# Upgrade: `nvcr.io/nvidia/vllm` → `26.06-py3` (NVFP4 enablement)

**Status:** 🟡 In progress — blocked on upstream issue closure
**Current pin:** `nvcr.io/nvidia/vllm:26.04-py3` (vLLM 0.19.0, NCCL 2.29.7) — stable
**Target:** `26.06-py3`+ (vLLM 0.22.1, NCCL 2.30.5) — required for NVFP4
**Last updated:** 2026-07-02

> NVIDIA NGC images are calendar-versioned `YY.MM`, so `26.06-py3` = the **June 2026**
> build of `nvcr.io/nvidia/vllm` (Python 3). It is *not* a vLLM version — 26.04 ships
> vLLM 0.19; 26.06 ships 0.22.1.

This is a **living tracker**, not a decision record. The upgrade is underway and
paused on specific upstream bugs; when the completion criteria below are met we
re-attempt it. (It is deliberately *not* an ADR — nothing is settled to make
immutable; we are waiting, not deciding against.)

---

## Why migrate at all

NVFP4 quantizations (Step-3.7-Flash-NVFP4, MiniMax NVFP4, …) need the b12x SM12.1
FP4 kernels merged to vLLM main on 2026-05-20 (PR #40082), first shipped in a
container ≥ 26.05/26.06. **26.04 predates them.** NVFP4 roughly halves the per-node
weight footprint vs FP8, which unlocks big-shared-with-headroom profiles on this
2× GB10 cluster (see `docs/models/step-3.7-flash.md`). So the target container is a
prerequisite for the whole NVFP4 line of work.

## What happened (2026-07-02)

1. Bumped the pin to `26.06-py3`, pulled on both nodes (digests matched), deployed
   `minimax` at TP=2.
2. **Both nodes hard-hung during multinode bring-up** — SSH-unresponsive, required
   a hard reset of both machines. (Same failure *class* as the earlier
   "Qwen3.5-122B froze sparky" lockup.)
3. Root-caused to a **NCCL 2.30.4+ NVLS regression on GB10** (see blockers). The
   version boundary matches our own logs exactly: 26.04's NCCL 2.29.7 works;
   26.06's NCCL 2.30.5 hangs.
4. Rolled back to 26.04 (stable; `minimax` running).
5. Implemented **fail-safe boot (ADR-0011)** so a *future* failed retry lands both
   nodes empty-and-reachable instead of re-hanging on every boot. This is what
   makes re-attempting 26.06 safe rather than reckless.
6. Staged the **NVLS killswitch** (`NCCL_NVLS_ENABLE=0`, see
   `roles/common/files/nccl-env.conf`) — harmless on 26.04, likely necessary on 26.06.

## Blocking upstream issues

Status as of **2026-07-02** — all open and un-triaged unless noted. Re-check these
when re-assessing.

| Issue | Scope | State | Triaged? | Workaround we control |
|---|---|---|---|---|
| [NVIDIA/nccl#2167](https://github.com/NVIDIA/nccl/issues/2167) | **Our 26.06 hang** — NCCL 2.30.4+ NVLS load-time hang on GB10 (no NVLink). 2.29.7/2.30.3 work; 2.30.4+ hang | Open | No label, no NVIDIA reply | **`NCCL_NVLS_ENABLE=0`** (staged) |
| [vllm#41725](https://github.com/vllm-project/vllm/issues/41725) | Inference-time TP=2 CUDA deadlock, 2× Spark + MiniMax, 35–55 min in | Open | No label, no maintainer reply | none confirmed — PP=2 sidesteps (latency cost) |
| [vllm#40969](https://github.com/vllm-project/vllm/issues/40969) | GB10 cudagraph inference hang (DeepSeek-V4, `FULL_AND_PIECEWISE`) | Open | Label `DSv4` only | `cudagraph_mode: PIECEWISE` / `--enforce-eager` |
| [vllm#33041](https://github.com/vllm-project/vllm/issues/33041) | Older TP=2-on-Blackwell **init** hang (NCCL 2.27.7) | Closed | Labeled `bug` | resolution unclear |

These are a *family* of distinct-but-related GB10-multinode hangs (load-time,
init-time, inference-time, cudagraph-time), each with its own trigger. Do not
assume fixing one fixes the others.

## Mitigations we control (independent of upstream)

| Lever | Addresses | Cost on this hardware | Where |
|---|---|---|---|
| `NCCL_NVLS_ENABLE=0` | nccl#2167 NVLS hang | **zero** (NVLS needs NVLink we don't have) | `roles/common/files/nccl-env.conf` — **staged** |
| `cudagraph_mode: PIECEWISE` / `--enforce-eager` | #40969 cudagraph hang | some throughput | per-profile `head_extra_args` (if needed) |
| PP=2 instead of TP=2 | #41725 inference deadlock | single-stream latency (pipeline bubbles) | profile `pipeline_parallel_size` |

## Completion criteria (re-attempt when ANY holds)

- **nccl#2167** is fixed/closed, **or** we validate `NCCL_NVLS_ENABLE=0` fully
  resolves our 2-node TP=2 bring-up on 26.06 (solo-first test plan below).
- A container ≥ 26.x ships **NCCL ≥ 2.30.6** (or otherwise reverts the NVLS
  regression).
- **vllm#41725** (inference-time TP=2 deadlock) closes with a fix or a confirmed
  stable workaround — this one is the real gate for *sustained* serving, since it
  strikes after 35–55 min even when bring-up succeeds.

## Retry plan (behind the fail-safe net)

When a criterion is met:

1. Re-pin the target container; `docker pull` on both nodes; **verify digests match**.
2. Confirm `NCCL_NVLS_ENABLE=0` is present in the deployed NCCL config.
3. **Test solo first** — a single-node profile (no cross-node NCCL) to isolate the
   FP4 kernels from the multinode path. If that alone hangs, the problem isn't NCCL.
4. Then TP=2 bring-up. Fail-safe boot (ADR-0011) means a hang → hard reset →
   empty+reachable, not a re-hang loop.
5. **Soak test**: run a multi-hour load to catch the #41725 inference-time deadlock
   (35–55 min). A clean bring-up is necessary but not sufficient.
6. Only promote to a serving profile after a clean soak.

## Re-assessment log

- **2026-07-02** — Migration opened. 26.06 bump hard-hung both nodes at TP=2
  bring-up; root-caused to NCCL 2.30.4+ NVLS regression (nccl#2167). Rolled back to
  26.04. Fail-safe boot (ADR-0011) and NVLS killswitch staged. All upstream issues
  open/un-triaged.

## References

- ADR-0011 — fail-safe boot (the net that makes retrying safe)
- `docs/models/step-3.7-flash.md` — NVFP4 target model + memory analysis
- `ansible/group_vars/all.yml` — the `vllm_image` pin (with the hang note)
- `ansible/roles/common/files/nccl-env.conf` — the NVLS killswitch
