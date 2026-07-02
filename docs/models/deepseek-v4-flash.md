# DeepSeek V4 Flash on 2× DGX Spark — Status Tracker

**Last updated:** 2026-05-23
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE

---

## Model Overview

- **Architecture:** 284B total parameters, **13B active per token** (Mixture-of-Experts)
- **Attention:** Compressed Sparse Attention (CSA) + Heavily Compressed Attention (HCA) hybrid — dramatically smaller KV cache than standard GQA
- **HuggingFace:** https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash

---

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Fit |
|---|---|---|---|---|
| BF16 | deepseek-ai/DeepSeek-V4-Flash | ~568 GiB | ~284 GiB | ❌ Does not fit |
| **FP4+FP8 mixed** | **deepseek-ai/DeepSeek-V4-Flash** | **~149 GiB** | **~73.85 GiB** | **⚠️ Fits — blocked on custom tooling** |

Only one quantized checkpoint is available; the mixed FP4+FP8 format is the official release.
FP4 covers routed experts (e2m1fn packed); FP8 E4M3 covers attention, norms, and router layers.

---

## FP4+FP8 Mixed — deepseek-ai/DeepSeek-V4-Flash

| | Per node |
|---|---|
| Model weights at TP=2 | ~73.85 GiB |
| KV cache at 200K context | ~2 GiB |
| **Free headroom** | **~50+ GiB** |

KV cache is extremely compact: V4 Flash compresses it **8.7× smaller** than V3. A 1M-token full cache needs only ~10 GiB across both nodes. At 200K tokens: ~2 GiB total.

**Concurrency tradeoffs at TP=2:**

| Context length | Max concurrent sequences |
|---|---|
| 200K | 2 |
| 65K | 16 |
| 32K | 36 |

---

## Performance (TP=2, dual DGX Spark, measured)

- Decode throughput: **~44 tok/s** (single stream)
- Concurrency=2 aggregate: ~45 tok/s
- TTFT short prompts: ~2s
- TTFT 32K context: ~53s
- TTFT 128K context: ~250s
- Speculative decoding (MTP, 2 tokens): ~68% acceptance rate
- Cold start (flashinfer autotuning): ~6 minutes

---

## Tooling Requirements (as of 2026-05-23)

### What's needed

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ⚠️ Custom fork required | Pin to commit `dda4668b59567416f86956cfe7bbc1eab371a61e` on [`jasl/vllm`](https://github.com/jasl/vllm) — only this commit has GB10 validation |
| **Docker image** | ⚠️ Custom image required | `eugr/spark-vllm-docker` (PR #219, unmerged community build) |
| **Ray** | ✅ Not needed | Uses `--distributed-executor-backend mp` (multiprocessing + direct NCCL over RoCE) — simpler than the Ray stack |
| **Deployment recipe** | Community | https://github.com/tonyd2wild/deepseek-v4-flash-dual-spark-recipe |

### Single-node variant (1× DGX Spark, hybrid quant)

A separate recipe exists using a hybrid 2-bit/FP8 quantization that fits in 128 GiB on a single node:
- ~85 GiB on-disk checkpoint, ~110 GiB resident during serving, ~18 GiB left for KV cache
- vLLM branch required: `kv-layout-dsv4-compressor-state` (3 KV cache patches not yet merged)
- Docker: `lmxxf/vllm-deepseek-v4-dgx-spark` (aarch64, SHA256 verified)

---

## Known Issues (as of 2026-05-23)

### 🔴 Hang after ~6 requests on SM 12.x (GB10)

- **Issue:** vLLM hangs after approximately 6 requests when using `cudagraph_mode=FULL_AND_PIECEWISE` with chunked prefill on SM 12.x hardware
- **GitHub:** https://github.com/vllm-project/vllm/issues/40969
- **Mitigation:** The `jasl/vllm` pinned commit works around this; mainline vLLM does not yet have the fix
- **Status:** Open / under active development as of 2026-05-23

---

## Key Links

| Resource | URL |
|---|---|
| NVIDIA forum: dual DGX Spark TP=2 recipe + numbers | https://forums.developer.nvidia.com/t/deepseek-v4-flash-official-fp8-running-across-2x-dgx-spark-tp-2-mtp-200k-ctx-recipe-numbers/370309 |
| NVIDIA forum: single DGX Spark hybrid quant recipe | https://forums.developer.nvidia.com/t/deepseekv4-flash-hybrid-quant-1x-dgx-spark-antirezs-optimized-128-gb-mlx-recipe-ported-to-vllm-for-gb10/369584 |
| vLLM blog: DeepSeek V4 long-context attention | https://vllm.ai/blog/deepseek-v4 |
| vLLM recipes page | https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4-Flash |
| LMSYS blog: DeepSeek V4 Day-0 (SGLang + Miles) | https://www.lmsys.org/blog/2026-04-25-deepseek-v4/ |
| VRAM requirements guide (all quants) | https://codersera.com/blog/deepseek-v4-vram-gpu-requirements-2026/amp/ |
| Dual-spark deployment recipe repo | https://github.com/tonyd2wild/deepseek-v4-flash-dual-spark-recipe |
| jasl/vllm fork (pinned commit needed) | https://github.com/jasl/vllm |
| GitHub issue #40969 (SM 12.x hang) | https://github.com/vllm-project/vllm/issues/40969 |
| HuggingFace model card | https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash |

---

## What to Watch For

To know when this is ready to deploy with standard tooling:

1. **Issue #40969 closed** — the SM 12.x hang fix merged to mainline vLLM
2. **`jasl/vllm` patches upstreamed** — GB10 validation lands in a released vLLM version
3. **NVIDIA vLLM container updated** — `nvcr.io/nvidia/vllm` image ships with V4 Flash support for GB10 (watch for a tag newer than `26.03.post1-py3`)
4. **`eugr/spark-vllm-docker` PR #219 merged** — or an equivalent official aarch64+GB10 image available

---

## Current Plan

Holding on V4 Flash until mainline tooling catches up. In the meantime:

- **snoopy:** `MiniMax-M2.7-AWQ-4bit` — full node dedicated, 128K context, works with current NVIDIA vLLM image
- **sparky:** `Qwen3-30B-A3B-Instruct-2507-FP8` — already installed, shares resources with Hermes + dev workloads

Revisit this file when any of the "What to Watch For" items above change.
