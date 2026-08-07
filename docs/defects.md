# Defect register

**Living index — not a decision record.** One row per **open** defect the cluster is
carrying, each with a **clears-when** condition so an update knows what to re-test.
This is the roll-up: the detailed analysis lives in the linked home (an upgrade
tracker's WAR register, an ADR, a model fact sheet). Rows **link, they don't
duplicate** — edit the home for detail, this table for status.

**Scope.** Defects whose follow-up is tied to an **update** — an upstream fix landing,
or a container/model/vLLM version bump we can re-test against. Operational gotchas with
a *permanent* fix (missing `--cgroupns=host`, `VLLM_HOST_IP` per node) are not defects
in this sense — they live in the README "Troubleshooting" section. Optimization-class
re-tests (disabled perf knobs) are governed by [ADR-0014](adr/0014-optimization-register.md);
the two that are corruption-bugs are indexed here as well (DEF-0007).

**How to use it during an update.** [`updating.md`](updating.md) sends you here: filter
to the component you're bumping, and for every row whose *Clears when* the update
satisfies, **re-test one at a time** (pulling several WARs at once hides which was still
load-bearing). Update the row's status with the result; when a defect is truly gone,
delete the row (git history keeps it) and drop its WAR from the home.

**Status:** 🔴 open (no mitigation, blocking) · 🟡 WAR'd (worked around, watching
upstream) · 🔵 watch (re-test on the next relevant bump — may already be fixed).

| ID | Symptom | Affects | Upstream | Workaround in place | Clears when — **re-test** | Status | Detail |
|---|---|---|---|---|---|---|---|
| **DEF-0001** | NCCL 2.30.4+ NVLS load-time hard hang on GB10 (no NVLink) at TP=2 bring-up | any global 26.06 / NCCL ≥ 2.30.4 | [nccl#2167](https://github.com/NVIDIA/nccl/issues/2167) | `NCCL_NVLS_ENABLE=0` (`roles/common/files/nccl-env.conf`) — cleared NCCL init 2026-07-02 | #2167 fixed **or** a container ships NCCL ≥ 2.30.6 with the regression reverted — **re-test** dual-node bring-up behind fail-safe | 🟡 WAR'd | [26.06 tracker](upgrades/container-nvidia-vllm-26.06-py3.md) |
| **DEF-0002** | TP=2 inference-time CUDA deadlock, 35–55 min into serving (2× Spark) | sustained 26.06 TP=2 serving | [vllm#41725](https://github.com/vllm-project/vllm/issues/41725) | none confirmed — PP=2 sidesteps (pipeline-bubble latency) | #41725 fixed/closed — **soak-test** 26.06 TP=2 for hours *under concurrency* (a 90-min light-load soak passed 2026-08-06; duration alone is not the gate) | 🔵 watch — 90 min clean 2026-08-06, unreproduced | [26.06 tracker](upgrades/container-nvidia-vllm-26.06-py3.md) |
| **DEF-0003** | GB10 cudagraph inference hang (`FULL_AND_PIECEWISE`) | 26.06 profiles once serving | [vllm#40969](https://github.com/vllm-project/vllm/issues/40969) | `cudagraph_mode: PIECEWISE` / `--enforce-eager` (per-profile) | #40969 fixed — **re-test** full cudagraphs, A/B throughput (ADR-0014) | 🟡 WAR'd | [26.06 tracker](upgrades/container-nvidia-vllm-26.06-py3.md) · [ADR-0014](adr/0014-optimization-register.md) |
| **DEF-0004** | compressed-tensors **WNA16 Marlin MoE weight-load hang** on sm_121 — clean 0.19→0.22.1 regression | AWQ/Marlin MoE (minimax, …) on 26.06 | related: [vllm#40357](https://github.com/vllm-project/vllm/issues/40357) (closest), [#43906](https://github.com/vllm-project/vllm/issues/43906), [#35303](https://github.com/vllm-project/vllm/issues/35303), [#41511](https://github.com/vllm-project/vllm/issues/41511); exact load-hang **unfiled** | keep AWQ/Marlin models on **26.04** (NVFP4 uses `modelopt`, not Marlin — orthogonal) | the Marlin-MoE sm_121 load path is fixed in the vLLM build a container ships — **re-test** the model solo on the new 26.x | 🟡 WAR'd | [26.06 tracker appendix](upgrades/container-nvidia-vllm-26.06-py3.md) |
| **DEF-0005** | fastapi 0.137 × `prometheus_fastapi_instrumentator` 8.0.0 → HTTP **500 on every `/v1/*`** (`_IncludedRouter` has no `.path`) | any stock 26.06-py3 image | [vllm#45596](https://github.com/vllm-project/vllm/issues/45596), [#45597](https://github.com/vllm-project/vllm/issues/45597), [fastapi#15791](https://github.com/fastapi/fastapi/discussions/15791) | derived image caps `fastapi<0.137` (`roles/images/files/vllm-26.06-fastapi-fix/`, built by the `images` role) | NVIDIA ships a 26.06+ image that caps `fastapi<0.137` or bundles a `_IncludedRouter`-aware instrumentator — then repoint `vllm_image` to stock, drop the derived entry | 🟡 WAR'd | [26.06 tracker](upgrades/container-nvidia-vllm-26.06-py3.md) · [ADR-0013](adr/0013-container-image-sourcing.md) |
| **DEF-0006** | Step-3.7 `Step3VLProcessor` crash-loops on startup — `AttributeError: … '_get_num_multimodal_tokens'` (VL-processor, orthogonal to NVFP4/container) | `step-3.7-nvfp4` profile (`blocked: true` — weights kept, not activatable) | **unfiled** — vLLM `Step3VLProcessor` / `transformers/multimodal.py` | **unblock path found (2026-07 sweep):** StepFun ships a prebuilt image `vllm/vllm-openai:stepfun37` + an official vLLM Step-3.7 recipe — buildable/pullable via the `images` role (ADR-0013) | deploy on a container carrying the fixed processor (StepFun `stepfun37` image, or a stock vLLM once the method lands) — **re-test** `step-3.7-nvfp4` | 🟡 WAR'd (container path exists) | [profile-step-3.7 tracker](upgrades/profile-step-3.7-flash.md) |
| **DEF-0007** | FP8 KV cache + prefix caching → multi-turn corruption (Nth-turn garbage / nonstop thinking) on vLLM 0.19 | `step-3.5-fp8` (and FP8/AWQ on 26.04) | none filed — suspected FP8-KV × prefix-cache interaction, vLLM 0.19 | both disabled on `step-3.5-fp8` | **re-test on 26.06 (vLLM 0.22.1)** — the bug's container is behind us, may already be fixed; enable one at a time, multi-turn test (ADR-0014 method) | 🔵 watch | [ADR-0014](adr/0014-optimization-register.md) · README "Known Shortcomings" |
| **DEF-0008** | `Qwen3.5-122B-A10B-FP8` froze sparky during load (hard hang; same failure *class* as DEF-0001) | that model — "do not use" | internal — no upstream (never root-caused) | do not deploy | root-caused, or retried behind fail-safe boot (ADR-0009) on a clean container — **re-test** a single-node load first | 🔴 open | README "Do not use" |

## Filing a new defect

When a deploy/bench/smoke turns up a defect worth carrying (an upstream bug we work
around, a model/version that breaks, a corruption class): add a row here with the next
`DEF-NNNN`, and put the detail in its proper home ([documentation skill](../skills/documentation/SKILL.md):
a WAR row in the relevant upgrade tracker, an ADR-0014 register row, or a model fact
sheet). Back-reference the ID from the code comment or profile that carries the WAR
(`# WAR for DEF-0005 …`) so the mitigation points at its tracking row. Keep the
*Clears when* concrete — that condition is what makes follow-up efficient.
