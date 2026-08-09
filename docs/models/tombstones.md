# Model tombstones — models this cluster has rejected

**Living register. This file OWNS these verdicts.** A model listed here has been
evaluated and ruled out; the reason and the condition to revisit it live *here*, not
scattered across fact sheets, the defect register, or the README. Those link in.

It exists so a discovery sweep costs nothing twice. The expensive failure mode is not
rejecting a model — it is rediscovering six months later, at the cost of a download, a
profile, a deploy and possibly a frozen node, that we already knew.

**Read this before proposing a model.** The [model-discovery](../../skills/model-discovery/SKILL.md)
and [model-evaluation](../../skills/model-evaluation/SKILL.md) skills both check it first.

## What belongs here — and what doesn't

A tombstone is a **model** we will not run, for a reason that is about *that model*.

| Not a tombstone | Where it lives instead | Why |
|---|---|---|
| A model we keep but can't currently serve | the profile, with `blocked: true` | Parked, not rejected — weights and engine files are kept so re-testing costs no download. `step-3.7-nvfp4` is parked on an upstream VL bug, and we fully intend to serve it. |
| A model that works on one container but not another | [`defects.md`](../defects.md), as a `DEF-NNNN` row | The model is fine; a version combination isn't. MiniMax-AWQ is pinned to 26.04 by DEF-0004 — still deployed, still serving. |
| A *setting* we tried and ruled out | the relevant upgrade tracker's "Ruled out" section | `--enforce-eager` didn't fix the Marlin hang. That's an experiment result, not a model. |
| A model superseded by a newer one we now run | nothing — git history covers it | Being replaced isn't a verdict. Only record it if the older one would otherwise look tempting again. |

**Park it with `blocked: true`; tombstone it only when the answer is "not on this
hardware, not this model."**

## The register

| Model | Verdict | Why — **owned here** | Reconsider when |
|---|---|---|---|
| **`Qwen3.5-122B-A10B-FP8`** | ⛔ **Never deploy** — hazardous | **Hard-froze sparky during weight load** (the machine, not the engine: unresponsive, recovered only by power cycle). Never root-caused, so we cannot say which layer failed or predict it elsewhere. It is the incident that motivated the fail-safe boot gate ([ADR-0009](../adr/0009-fail-safe-boot.md)). Treated as hazardous rather than merely broken: the cost of being wrong is a physical trip to the machine. | Someone root-causes the lockup upstream, **or** it is retried deliberately — single-node load first, fail-safe boot verified, a human at the keyboard. Never as an incidental part of a sweep. |
| **`MiniMax-M3`** (incl. `nvidia/MiniMax-M3-NVFP4`) | ⛔ **Does not fit** — hardware-bounded | ~428B total (A23B). The smallest quant that exists is NVFP4 at ~250 GiB → **~125 GiB per node at TP=2, against 121 GiB usable.** It misses by a margin no `gpu_memory_utilization` setting can close, because the shortfall is in *weights*, not KV cache. M3 needs TP=4 minimum. Model facts: [`minimax-m3.md`](minimax-m3.md). | The cluster grows past two nodes (TP=4 puts it at ~62 GiB/node, comfortable), **or** a quant lands at ≤ ~240 GiB total. Neither is close today — a sub-NVFP4 quant of a 428B MoE is not a thing that exists. |

## Adding one

Add a row, and **move** the verdict here from wherever it currently sits — leaving a
link behind, never a copy. Keep *Reconsider when* concrete and falsifiable, in the same
spirit as a defect's *clears-when*: a tombstone with no revisit condition is a dead end
rather than a decision, and this cluster's standing priority is to keep current.

If a tombstone's condition is met and the model proves out, **delete the row** — git
history keeps the reasoning.
