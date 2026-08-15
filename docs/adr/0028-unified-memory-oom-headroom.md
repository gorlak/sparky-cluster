# ADR-0028: Unified-memory OOM is unrecoverable — headroom is a hard floor, not a tuning knob

**Date:** 2026-08-15
**Status:** Accepted

## Context

On 2026-08-15 a measurement run took **both nodes down at once** with an unclean hard-reset.
The trigger was ordinary — the `soak` regiment, sustained concurrent load against
`qwen3.6-35b-a3b-nvfp4` at `gpu_memory_utilization: 0.80`. The failure was not: instead of
the OOM killer surgically reaping the offending process and the box carrying on, the whole
system livelocked and reset.

This ADR records why that happens on GB10, and the standing rule that follows — because the
naive mental model ("Linux will kill the hog and recover") is wrong here in a way that turns
a tuning mistake into a fleet outage.

The fail-safe held: the `.running` markers survived on both nodes, so
[ADR-0009](0009-fail-safe-boot.md)'s boot gates skipped auto-start and the fleet came back
**empty and reachable**. That is the only reason this was a recoverable incident and not a
manual rescue. This ADR is about not reaching that state in the first place.

## Investigation

### The margin, and where it went

At `gmu 0.80`, vLLM reserves `0.80 × 121 ≈ 97 GiB` of the 121 GiB unified pool and holds it
flat for the engine's lifetime. Measured host-visible headroom at idle with the engine
loaded: **6 GiB.** On GB10 the GPU allocation is charged to the *same* pool the OS lives in,
so that 6 GiB is all the OS, the frontends, the harness and page cache have to share.

`soak` grows a working set over its run — a lengthening multiturn conversation in the
harness, plus the API server's host-side buffers under long-context load. Bounded, but
larger than 6 GiB. It consumed the margin and all 16 GiB of swap (`Free swap = 0kB`).

### The OOM killer never actually killed anything

The premise that the box dropped SSH *because* the killer murdered a process is wrong. It
was **invoked** at 06:02:34 (`MainThread invoked oom-killer`) and never completed. There is
**no `Killed process` line** in the log; the last thing sparky ever wrote is the OOM report's
task-table header. It died mid-report, before selecting a victim.

The kernel's own timestamps prove a **direct-reclaim livelock**:

```
06:02:34  invoked oom-killer
06:02:42  Call trace:          (+8s)
06:02:54  oom_kill_process     (+12s)
06:04:24  [task dump begins]   (+90s)   — then nothing
```

Kernel OOM lines normally print microseconds apart. Taking ~90 seconds to reach the task
dump, and never finishing it, means the kernel could not schedule itself to completion.

### Why the kill could not have helped anyway

Three things compounded, and the first is GB10-specific:

1. **Most of the pressure is unreclaimable.** vLLM's ~97 GiB is GPU-pinned. The OOM killer
   can only reap userspace *anonymous* pages; it cannot touch pinned device memory. Even a
   successful kill of the offending Python would have freed a few GiB of host RSS against
   ~97 GiB it is not allowed to reclaim.
2. **Swap was already exhausted** — zero reclaim headroom.
3. With RAM and swap gone and the bulk unreclaimable, **every allocation stalls in direct
   reclaim that frees nothing.** `sshd` cannot `fork`/allocate to service a login → SSH
   drops. The kernel cannot allocate to finish its own OOM report → the 90 s crawl. The box
   is alive but no process — including the OOM reaper — makes forward progress.

### Why both nodes, when only one ran out of memory

**snoopy recorded zero OOM-killer invocations.** It never ran short. It was in **NCCL
lockstep** with sparky's TP=2 shard; when sparky stopped answering the collective, snoopy's
engine hung on it. Both reset together at ~06:14 — a wedged head drags its TP=2 worker down,
regardless of the worker's own memory state. **Tensor parallelism turns a single-node OOM
into a fleet event.**

### Why there was no clean `ENOMEM` to catch

`vm.overcommit_memory = 0` (the default). Under overcommit, `malloc`/`mmap` return success
without reserving physical pages; memory becomes real only when *touched*. So by the time
the pool was exhausted, every allocation call had long since been answered "yes" — there was
no pending request to fail. The actual shortfall lands at **page-fault time**, servicing a
plain store instruction that has no return value and no errno: the write completes or the
process dies. There is no layer at which the program could catch it and unwind. And the
tipping allocation here was `gfp_mask=0xcc0` — **`GFP_KERNEL`**, the kernel's own request on
the process's behalf, which cannot be reflected to userspace as an error at all.

## Decision

**Host headroom on unified memory is a hard floor, established by measurement, not a knob to
maximise.** Three parts:

### 1. Restore headroom now (done)

`qwen3.6-35b-a3b-nvfp4` dropped from `gmu 0.80` to `0.70`. That hands ~12 GiB back to the
host — measured idle headroom **6 GiB → ~20 GiB** with the engine loaded. The full run that
crashed at 0.80 then completed clean at 0.70: all five regiments, `soak` at 1,098 requests
with 0 failures, host memory **flat throughout**. KV capacity fell from 16.3M to 13.8M
tokens (~15%), still dozens of full-length 262k contexts — the trade is real and small.

The point is not the number. It is that **a configuration must leave enough host headroom to
survive its own measurement load**, and 6 GiB does not on this hardware.

### 2. Headroom is measured, and the danger is a cliff not a slope

The reason 0.80 was dangerous rather than merely tight is that the failure mode is
**discontinuous**. Below some margin the OOM killer can still reap a process and the box
survives; below the point where reclaimable memory is smaller than the transient spike, the
system livelocks and only a reset recovers it. There is no graceful degradation across that
line. So headroom is not "spend it down to the last safe byte" — it is a floor with a margin
for the largest transient a workload produces, and the largest transient is a *measured*
quantity, not a reasoned one. This is why [`docs/measurement.md`](../measurement.md) treats
`gpu_memory_utilization` as a swept axis with an outcome, and why
[`docs/profile-tuning.md`](../profile-tuning.md)'s "gmu is a split" is now a safety property,
not only a performance one.

### 3. Strict overcommit is the failure-mode fix, and is deferred pending test

`vm.overcommit_memory = 2` (strict) would refuse allocations past `CommitLimit`, so `malloc`
returns NULL and Python raises a catchable `MemoryError` — converting *"both nodes livelock
and reset"* into *"the harness process dies cleanly, the node stays up."* That is a strictly
better failure mode for a serving box, and it is the direct answer to "why not deny the
request instead of killing."

It is **not adopted yet**, because strict overcommit changes allocation semantics
system-wide: it can break `fork()` of large processes and reject legitimate sparse or CUDA
allocations conservatively, and vLLM's startup does both. Flipping it blind risks trading a
rare livelock for frequent spurious failures. It is a **proposed follow-up**, gated on
testing vLLM engine startup and the harness under `overcommit_memory=2` on one node before
the fleet. Recorded here so the option is not rediscovered from scratch.

## Consequences

- **`gmu` now carries a safety floor, not just a performance target.** A profile that maxes
  it to buy KV or throughput is one transient away from an unrecoverable reset, and the
  reset takes the TP=2 peer with it.
- **The measurement campaign gained a real result on its first live run** — the crash is the
  datum. gmu 0.70 is the proven operating point for this profile; 0.80 is not.
- **TP=2 raises the stakes of any single-node OOM to a fleet event.** Headroom on a
  tensor-parallel profile protects both nodes, not one.
- **The fail-safe is doing exactly its job** ([ADR-0009](0009-fail-safe-boot.md)) — it is
  what made an unrecoverable livelock into a reboot-and-reactivate rather than a rescue. It
  is a backstop, not a licence to run at the edge.
- **A latent, better failure mode is on the table** (strict overcommit) but unproven here.

## Alternatives considered

**Trust the OOM killer to recover.** The investigation is the rejection: on unified memory
the killer cannot reclaim the dominant allocation, and the box livelocks before the kill even
completes. It is not a safety net on this hardware.

**Add swap so there is more to fall back on.** Swap *was* the fallback and it was fully
consumed; more swap enlarges the thrashing window without changing the outcome, and swapping
a serving engine's working set is its own latency disaster.

**Cap the harness process with a cgroup `MemoryMax`.** Plausible and worth revisiting — it
would make the *harness* the thing that dies rather than the node. But it does not help when
the pressure comes from the API server's own host buffers rather than the harness, and it
needs the same headroom analysis to set the cap. A narrower version of the strict-overcommit
idea; deferred with it.

**Lower `max_model_len` instead of `gmu`.** Wrong lever: `max_model_len` is a cap that
allocates nothing (ADR-0026); it does not change the reservation that left 6 GiB. `gmu` is
the split that does.

## References

- [ADR-0009](0009-fail-safe-boot.md) — the fail-safe that made this recoverable; the
  `.running` markers that survived are its signature
- [ADR-0016](0016-continuous-evaluation-outer-loop.md) — the outer loop; `soak` is the
  regiment that produced the load
- [ADR-0026](0026-long-context-measurement.md) — why `max_model_len` is not the lever here
- [`docs/measurement.md`](../measurement.md) — `gmu` as a measured axis with an outcome
- [`docs/profile-tuning.md`](../profile-tuning.md) — "gmu is a split," now also a safety floor
- `ansible/profiles/qwen3.6-35b-a3b-nvfp4.yml` — the 0.80 → 0.70 change and its inline note
