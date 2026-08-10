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
| A model we keep but can't currently serve | the profile, with `blocked: true` | Parked, not rejected — weights and engine files are kept so re-testing costs no download. `step-3.7-flash-nvfp4` is parked on an upstream VL bug, and we fully intend to serve it. |
| A model that works on one container but not another | [`defects.md`](../defects.md), as a `DEF-NNNN` row | The model is fine; a version combination isn't. MiniMax-AWQ is pinned to 26.04 by DEF-0004 — still deployed, still serving. |
| A *setting* we tried and ruled out | the relevant upgrade tracker's "Ruled out" section | `--enforce-eager` didn't fix the Marlin hang. That's an experiment result, not a model. |
| A model superseded by a newer one we now run | nothing — git history covers it | Being replaced isn't a verdict. Only record it if the older one would otherwise look tempting again. |
| A model we'd keep, but whose weights we **evicted** for disk | here, marked *evicted, not condemned* | Parking assumes we hold the weights. Once they're gone a discovery sweep will propose re-downloading — which is the exact waste this register prevents — so the eviction needs a row even though the model is not rejected. |

**Park it with `blocked: true`; tombstone it only when the answer is "not on this
hardware, not this model."**

## The register

| Model | Verdict | Why — **owned here** | Reconsider when |
|---|---|---|---|
| **`Qwen3.5-122B-A10B-FP8`** | ⛔ **Never deploy** — hazardous | **Hard-froze sparky during weight load** (the machine, not the engine: unresponsive, recovered only by power cycle). Never root-caused, so we cannot say which layer failed or predict it elsewhere. It is the incident that motivated the fail-safe boot gate ([ADR-0009](../adr/0009-fail-safe-boot.md)). Treated as hazardous rather than merely broken: the cost of being wrong is a physical trip to the machine. | Someone root-causes the lockup upstream, **or** it is retried deliberately — single-node load first, fail-safe boot verified, a human at the keyboard. Never as an incidental part of a sweep. |
| **`MiniMax-M3`** (incl. `nvidia/MiniMax-M3-NVFP4`) | ⛔ **Does not fit** — hardware-bounded | ~428B total (A23B). The smallest quant that exists is NVFP4 at ~250 GiB → **~125 GiB per node at TP=2, against 121 GiB usable.** It misses by a margin no `gpu_memory_utilization` setting can close, because the shortfall is in *weights*, not KV cache. M3 needs TP=4 minimum. Model facts: [`minimax-m3.md`](minimax-m3.md). | The cluster grows past two nodes (TP=4 puts it at ~62 GiB/node, comfortable), **or** a quant lands at ≤ ~240 GiB total. Neither is close today — a sub-NVFP4 quant of a 428B MoE is not a thing that exists. |

| **`DeepSeek-V4-Flash`** (`deepseek-ai/DeepSeek-V4-Flash-DSpark`) | ⚠️ **Evicted — wrong image, not wrong hardware** | Loads and shards fine, then dies at weight load with `Unknown SF transformation` (`layout.hpp`). We first read that as "GB10 has no FP8 GEMM" and treated the model as hardware-bounded. **That was wrong, and upstream says so:** [vllm#41063](https://github.com/vllm-project/vllm/issues/41063) reports FP8 ops working on SM 12.x and identifies the **FP4** path as the blocker, while [DeepGEMM#372](https://github.com/deepseek-ai/DeepGEMM/issues/372) names our exact failure — no SM120/121 branch for NVFP4 **expert scales**. V4-Flash is FP8 dense + **mxfp4 MoE**, so its FP4 experts are the part with no kernels. Decisive evidence that this is an image problem: at least three projects serve it on **2× DGX Spark** today — [tonyd2wild](https://github.com/tonyd2wild/deepseek-v4-flash-dgx-spark), [hazyumps](https://github.com/hazyumps/deepseek-v4-flash-gb10) (~1.7k tok/s prefill, 40–60 tok/s decode), [elsung](https://github.com/elsung/dgx-spark-deepseek-v4-flash) — every one of them on a **custom vLLM built for `sm_121`** (`TORCH_CUDA_ARCH_LIST=12.1a`), not NVIDIA's container. 156 GiB evicted because we cannot serve it *today*, not because it cannot be served. Config kept in [`profiles/retired/`](../../ansible/profiles/retired/deepseek-v4-flash-dspark.yml). | **Three concrete routes, cheapest first.** (1) A container bump lands the FP4 kernels — re-probe on every new image. (2) Build a derived image for `sm_121` the way the community recipes do; we already run one derived image (the xgrammar WAR), so the mechanism exists (ADR-0013). (3) Use the **native FP8** checkpoint `deepseek-ai/DeepSeek-V4-Flash` that the working recipes use, **not** `nvidia/DeepSeek-V4-Flash-NVFP4` — evaluated 2026-08-10 and rejected, since an NVFP4 build leans harder on the one path that is missing. |

| **`Step-3.5-Flash-FP8`** (`stepfun-ai/Step-3.5-Flash-FP8`) | ⚠️ **Evicted — outclassed on every axis** | Served this cluster reliably for months; retired 2026-08-10 on measurement, not preference. It is the only model the fleet scoreboard flagged **dominated** (beaten on accuracy *and* throughput *and* node occupancy): **32,768** usable context — one quarter of the next worst and 1/8th of the best — **19.0 tok/s** single-stream and **51.5 ms** TPOT (slowest measured), and 54.3% on the MMLU-Pro subset, itself a *floor* (59 of 140 items truncated, 58 returning empty). It cost **195 GiB**, the largest checkpoint we held, and was the last profile on the 26.04 container; retiring it makes the fleet single-container. It also carried [DEF-0007](../defects.md) — FP8 KV cache and prefix caching both disabled for multi-turn stability, so it ran without two optimisations everything else gets. | A Step release lands that is competitive on **context** (our binding constraint) — the 32k ceiling was the disqualifier, not the speed. [`step-3.7-flash-nvfp4`](../../ansible/profiles/step-3.7-flash-nvfp4.yml) is still parked on DEF-0006 and is the natural re-entry point for this vendor: its weights are kept, so re-testing costs no download. |

| **`Qwen3-VL-32B-Instruct-NVFP4`** | ⚠️ **Evicted — its niche closed** | Never served: vLLM 0.24.0 refuses the checkpoint, whose real quant is `NVFP4_AWQ` ([DEF-0013](../defects.md) — re-probed 2026-08-10, `modelopt_algos` is still `[FP8, NVFP4]`). It was parked rather than deleted because re-testing was free. What changed is not the defect but the *reason to want it*: it existed as the cheapest route to vision — 21 GiB, TP=1, sparky left free — and on 2026-08-10 the single-node shape lost on every measured axis while [`qwen3-vl-235b-a22b-instruct-nvfp4`](../../ansible/profiles/qwen3-vl-235b-a22b-instruct-nvfp4.yml) serves vision **and** tools at **75.0%**. Unblocking it would not make us run it. Config kept at [`profiles/retired/`](../../ansible/profiles/retired/qwen3-vl-32b-instruct-nvfp4-single.yml). | We want a **small** VL model again — i.e. a workload that needs vision on one node while the other does something else. That is a fleet-shape question, not a model question; if it returns, re-check DEF-0013 first with `./sparky.sh probe quant`. |

## Adding one

Add a row, and **move** the verdict here from wherever it currently sits — leaving a
link behind, never a copy. Keep *Reconsider when* concrete and falsifiable, in the same
spirit as a defect's *clears-when*: a tombstone with no revisit condition is a dead end
rather than a decision, and this cluster's standing priority is to keep current.

If a tombstone's condition is met and the model proves out, **delete the row** — git
history keeps the reasoning.
