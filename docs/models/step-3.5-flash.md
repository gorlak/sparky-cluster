# Step-3.5-Flash on 2× DGX Spark — Status Tracker

**Last updated:** 2026-05-24
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE
**Installed quant:** FP8 — `stepfun-ai/Step-3.5-Flash-FP8` (~195 GiB, ~97.5 GiB/node at TP=2)

---

## Model Overview

- **Developer:** StepFun AI
- **Architecture:** Sparse Mixture-of-Experts (MoE), 45-layer Transformer
- **Total parameters:** 196.81B (196B backbone + 0.81B head)
- **Active parameters per token:** ~11B
- **MoE config:** 288 routed experts per layer + 1 shared expert; Top-8 selected per token
- **Context window:** 256K tokens (3:1 Sliding Window Attention)
- **Speculative decoding:** MTP-3 (3-way Multi-Token Prediction) — not yet fully supported in vLLM
- **Vocabulary:** 128,896 tokens
- **HuggingFace:** https://huggingface.co/stepfun-ai/Step-3.5-Flash
- **GitHub:** https://github.com/stepfun-ai/Step-3.5-Flash
- **Paper:** https://arxiv.org/pdf/2602.10604
- **Official vLLM recipe:** https://docs.vllm.ai/projects/recipes/en/latest/StepFun/Step-3.5-Flash.html

---

## Quantization Formats & Footprint

| Format | Disk | VRAM (total) | Per node at TP=2 |
|---|---|---|---|
| BF16 (full precision) | ~400 GiB | ~400 GiB | ~200 GiB — does not fit |
| **FP8** | **~195 GiB** | **~195 GiB** | **~97.5 GiB ✅ (tight)** |
| INT4 GGUF (Q4_K_S) | ~111.5 GiB | ~120 GiB | ~60 GiB ✅ |

**FP8 is the target format.** Pre-deployment estimates of ~50 GiB/node were
wrong — see correction below.

---

## FP8 — stepfun-ai/Step-3.5-Flash-FP8

> ⚠️ **Pre-deployment estimates were significantly off.** The model card and
> early references quoted ~100 GiB total / ~50 GiB per node. The actual
> on-disk and in-VRAM footprint is ~195 GiB total / ~97.5 GiB per node.
>
> **Why:** This is a sparse MoE model, but vLLM loads all experts into VRAM
> at startup regardless of which are active per token. 196B parameters × 1
> byte (FP8) ≈ 196 GiB. The ~50 GiB figure assumed only active experts were
> loaded, which is not how vLLM handles MoE.

| | Per node |
|---|---|
| Model weights | ~97.5 GiB |
| GPU memory budget (0.90 × 121 GiB) | ~108.9 GiB |
| KV cache + CUDA graphs headroom | **~11 GiB** |
| Headroom for other workloads | **effectively zero** |

`--gpu-memory-utilization 0.90` is required. At 0.70 (the old default
recommendation), KV cache allocation goes negative and vLLM refuses to start.

---

## Tooling Requirements (as of 2026-05-24)

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ✅ Standard release | Official recipe on vllm.ai; 26.04 image has SM12.1 CUTLASS fix |
| **Docker image** | ✅ Standard NVIDIA image | `nvcr.io/nvidia/vllm:26.04-py3` |
| **Ray** | ❌ Not needed / not available | vLLM 0.19 dropped Ray; native multinode via `--nnodes/--node-rank/--master-addr` |
| **SM12.1 workarounds** | ✅ Fixed in 26.04 | PR #38126 merged; CUTLASS kernels now work on SM12.1 |

### SM12.1 workarounds (fallback only — should not be needed with 26.04)

```bash
VLLM_DISABLED_KERNELS=cutlass_moe_mm,cutlass_scaled_mm
VLLM_MOE_KERNEL_BACKEND=triton
```

---

## Performance

**Official benchmarks (4×H200, TP=4, FP16):**
- Output throughput: 811.94 tok/s
- TTFT (mean): 422.62 ms
- ITL (inter-token latency): 11.91 ms

**Single DGX Spark, community measured:**
- ~20–24 tok/s at 44 ms latency (short context 2K–8K)

**Dual DGX Spark TP=2 (estimated):**
- ~40–48 tok/s decode

**Note:** MTP-3 speculative decoding (~3× throughput boost) is not yet fully supported in vLLM. When it lands, throughput should improve substantially.

---

## Known Issues (as of 2026-05-23)

### ✅ SM12.1 CUTLASS kernels — FIXED in 26.04

- PR #38126 added SM12.1 variants to all NVFP4/CUTLASS guards
- Previously caused ~40 traps during warmup with 26.03
- **Tracking:** https://github.com/vllm-project/vllm/issues/31128

### ⚠️ V1 engine "sink setting not supported"

- Affects some attention backends on GB10; status in 26.04 unknown
- **Tracking:** https://github.com/vllm-project/vllm/issues/28589

### ⚠️ Aggressive memory allocation defaults in 26.04

- 26.04 defaults to near-100% GPU memory utilization, which can cause OOM on unified-memory systems
- Use `--gpu-memory-utilization 0.90` — **not 0.70**; at 0.70 the model doesn't fit (KV cache allocation goes negative)

### ⚠️ Do not pass `--quantization fp8` for FP8 checkpoints

- `Step-3.5-Flash-FP8` declares `quantization_config: {quant_method: fp8}` in `config.json`
- vLLM auto-detects this; adding `--quantization fp8` causes double-quantization
- Symptom: garbage multilingual output on every inference

### ⚠️ FP8 KV cache + prefix caching interaction (vLLM 0.19, under investigation)

- `--kv-cache-dtype fp8` and `--enable-prefix-caching` together cause multi-turn
  conversation corruption: Nth inference produces nonstop garbage thinking tokens
- Currently both are disabled in production config
- Re-enable one at a time to narrow down which is the culprit

### ⚠️ MTP-3 not fully supported in vLLM

- Pending upstream; throughput will improve when it lands

---

## Current vllm serve flags (production, 2026-05-24)

```
vllm serve /models/Step-3.5-Flash-FP8 \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name step-3.5-flash \
    --tensor-parallel-size 2 \
    --distributed-executor-backend mp \
    --nnodes 2 \
    --node-rank 0 \
    --master-addr 10.0.200.12 \
    --trust-remote-code \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.90 \
    --enable-chunked-prefill \
    --enable-auto-tool-choice \
    --tool-call-parser step3p5 \
    --reasoning-parser step3p5
```

Flags intentionally omitted (vs. earlier plans):
- `--quantization fp8` — causes double-quantization on FP8 checkpoints; never use
- `--kv-cache-dtype fp8` — disabled pending multi-turn corruption investigation
- `--enable-prefix-caching` — disabled pending same investigation
- `--distributed-executor-backend ray` — Ray removed from vLLM 0.19

`--max-model-len 32768` is conservative given the tight memory budget (~11 GiB
headroom). May be raisable once KV cache flags are sorted out.

---

## Comparison: Step-3.5-Flash vs DeepSeek V4 Flash

| | Step-3.5-Flash | DeepSeek V4 Flash |
|---|---|---|
| Total params | 196B | 284B |
| Active params/token | ~11B | ~13B |
| Context | 256K | 200K |
| FP8 per node at TP=2 | ~97.5 GiB | ~73.85 GiB |
| Headroom per node | ~11 GiB | ~35 GiB |
| Standard vLLM? | ✅ Yes | ❌ Requires custom fork |
| Custom Docker? | ❌ No | ❌ Yes |
| SM12.1 fixed? | ✅ Fixed in 26.04 | 🔴 Hang bug still open |
| MTP support in vLLM? | ⚠️ Pending | ⚠️ Pending |
| **Ready to try?** | **✅ Yes** | **❌ Not yet** |

---

## What to Watch For

1. **MTP-3 support in vLLM** — large throughput uplift when it lands
2. **V1 engine fix for GB10** — https://github.com/vllm-project/vllm/issues/28589
3. **NVIDIA vLLM 26.05+** — check release notes before future container updates

---

## Key Links

| Resource | URL |
|---|---|
| HuggingFace model | https://huggingface.co/stepfun-ai/Step-3.5-Flash |
| GitHub repo | https://github.com/stepfun-ai/Step-3.5-Flash |
| ArXiv paper | https://arxiv.org/pdf/2602.10604 |
| Official vLLM recipe | https://docs.vllm.ai/projects/recipes/en/latest/StepFun/Step-3.5-Flash.html |
| vLLM recipe (GitHub) | https://github.com/vllm-project/recipes/blob/main/StepFun/Step-3.5-Flash.md |
| NVIDIA forum: single Spark thread | https://forums.developer.nvidia.com/t/running-step-3-5-flash-on-single-spark/359457 |
| NVIDIA vLLM 26.04 release notes | https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/rel-26-04.html |
| NVIDIA vLLM 26.03 release notes | https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/rel-26-03.html |
| vLLM PR #38126 (SM12.1 CUTLASS fix) | https://github.com/vllm-project/vllm/pull/38126 |
| vLLM issue #31128 (SM12.1 support) | https://github.com/vllm-project/vllm/issues/31128 |
| vLLM issue #28589 (V1 engine GB10) | https://github.com/vllm-project/vllm/issues/28589 |
| bkrabach/dgx-spark-cluster | https://github.com/bkrabach/dgx-spark-cluster |
| eelbaz/dgx-spark-vllm-setup | https://github.com/eelbaz/dgx-spark-vllm-setup |
| Medium: vLLM on DGX Spark SM121 guide | https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8 |
| Medium: Flash Attention on SM121 | https://medium.com/@rakshith.d26/flash-attention-on-sm-121-solving-pytorch-compatibility-on-blackwell-gb10-a83d9ff3cf9b |
| NVIDIA NIM model card | https://build.nvidia.com/stepfun-ai/step-3-5-flash/modelcard |
