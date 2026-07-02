# Mistral-Medium-3.5-128B on 2× DGX Spark — Status Tracker

**Last updated:** 2026-07-02
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE
**Installed quant:** None — not downloaded
**Target quant:** FP8 official checkpoint (fits at TP=2; see analysis)

---

## Model Overview

> ⚠️ **Dense model, not MoE.** This is the first dense model analyzed for this cluster.
> All other current/candidate models (Step-3.x-Flash, MiniMax-M2.7/M3, DeepSeek-V4-Flash)
> are MoE. Dense vs. MoE changes the KV cache characteristics significantly — see below.

- **Developer:** Mistral AI
- **Architecture:** Dense Transformer (not MoE) — all 128B parameters active per token
- **Context window:** 256K tokens
- **Multimodal:** Text + image input (vision encoder)
- **Reasoning:** Yes — `--reasoning-parser mistral`
- **Tool calling:** Yes — `--tool-call-parser mistral`
- **Speculative decoding:** EAGLE (`mistralai/Mistral-Medium-3.5-128B-EAGLE` — separate draft model)
- **Tokenizer:** Mistral-native — requires `--tokenizer-mode mistral`
- **HuggingFace (official):** https://huggingface.co/mistralai/Mistral-Medium-3.5-128B
- **HuggingFace (EAGLE draft):** https://huggingface.co/mistralai/Mistral-Medium-3.5-128B-EAGLE
- **HuggingFace (NVFP4, community):** https://huggingface.co/zdy1995love/Mistral-Medium-3.5-128B-NVFP4

---

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Fit |
|---|---|---|---|---|
| BF16 | — | ~267 GiB | ~134 GiB | ❌ Exceeds 121 GiB limit |
| **FP8** | **mistralai/Mistral-Medium-3.5-128B** | **~134 GiB** | **~67 GiB** | **✅ Fits with headroom** |
| NVFP4 | zdy1995love/Mistral-Medium-3.5-128B-NVFP4 | ~75 GiB | ~38 GiB | ✅ Fits — quality risk (RTN, uncalibrated) |
| GGUF | unsloth, bartowski | varies | — | ⚠️ GGUF serving not in use |

> **On the official checkpoint precision:** HuggingFace lists the official repo as "BF16" but the
> checkpoint files total ~134 GiB for 128B parameters — the size of an FP8 checkpoint (128B × 1
> byte ≈ 128 GiB + embeddings), not BF16 (128B × 2 bytes ≈ 256 GiB). The model card also tags
> `F8_E4M3`. The most likely explanation: the official checkpoint is FP8, and the "BF16" label
> reflects activation dtype or is a HuggingFace metadata error. Verify from `config.json` on
> download. The 267 GiB total repo size is both formats (model-* shards + consolidated-* shards)
> of the same weights.

---

## FP8 — mistralai/Mistral-Medium-3.5-128B

### Memory fit (TP=2)

Architecture details not publicly documented; the following are estimated from checkpoint
size and Mistral's design patterns for 128B-class models. **Verify from config.json on download.**

| Item | Estimated |
|---|---|
| num_hidden_layers | ~80 |
| hidden_size | ~8192 |
| num_attention_heads | ~64 |
| num_key_value_heads | ~8 (GQA) |
| head_dim | ~128 |
| intermediate_size | ~57344 |

| | Per node |
|---|---|
| Weights at TP=2 (FP8, estimated) | ~67 GiB |
| CUDA graphs (estimate) | ~2 GiB |
| At gmu=0.75: KV available | **~22 GiB** |
| At gmu=0.75: outside headroom | **~30 GiB** |
| At gmu=0.90: KV available | **~40 GiB** |

### Dense model KV cache — the key trade-off

KV per token (estimated): **~320 KiB** (80 layers × 2 × 8 KV heads × 128 head_dim × 2 bytes)

Compare to MoE models on this cluster:
- MiniMax-M2.7-AWQ: ~124 KiB/token (62 layers, 8 KV heads, head_dim 64)
- Step-3.5-Flash-FP8: similar or smaller (fewer effective KV layers)

Dense models carry **2.5× more KV per token** than similar-weight MoE models because the
head_dim is larger (128 vs 64) — a consequence of the larger hidden_size required to hold
128B parameters in a dense architecture.

**Practical concurrency at gmu=0.75 (~22 GiB KV):**

| Context length | KV per sequence | Concurrent sessions |
|---|---|---|
| 8K | ~2.5 GiB | ~8 |
| 32K | ~10 GiB | ~2 |
| 128K | ~40 GiB | ❌ Doesn't fit |
| 128K | ~40 GiB | ✅ 1 session at gmu=0.90 |
| 256K | ~80 GiB | ❌ Doesn't fit at any reasonable gmu |

For typical coding/reasoning tasks at 8–32K context, 2–8 concurrent sessions at gmu=0.75.
For long-context use (128K+), raise gmu to 0.90 and expect 1 concurrent session.

### Tooling status

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ✅ Officially recommended | Mistral AI lists vLLM as primary inference framework |
| **Docker image** | ⚠️ Verify in 26.04 | Model released May 2026; confirm 26.04 supports the architecture |
| **`--tokenizer-mode mistral`** | 🔴 Required | Mistral-native tokenizer, not HuggingFace format |
| **`--tool-call-parser mistral`** | ✅ Supported | Standard Mistral tool-call format in vLLM |
| **`--reasoning-parser mistral`** | ✅ Supported | Reasoning mode |
| **`--quantization` flag** | ⚠️ TBD | If checkpoint is FP8, auto-detected; if BF16, not needed. Verify on load. |
| **EAGLE speculative decoding** | ⚠️ TBD | Separate draft model; vLLM EAGLE support needed |
| **`--kv-cache-dtype fp8`** | ⚠️ Cluster-wide caveat | Not enabled pending multi-turn corruption investigation |

### Serve flags (draft — not yet tested)

```
vllm serve /models/Mistral-Medium-3.5-128B \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name mistral-medium-3.5 \
    --tensor-parallel-size 2 \
    --distributed-executor-backend mp \
    --nnodes 2 \
    --node-rank 0 \
    --master-addr 10.0.200.12 \
    --tokenizer-mode mistral \
    --trust-remote-code \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.75 \
    --enable-chunked-prefill \
    --enable-auto-tool-choice \
    --tool-call-parser mistral \
    --reasoning-parser mistral
```

`max-model-len 32768` is conservative. Raise to 131072 for 128K context at gmu=0.90 (1
concurrent session). Do not set `max-model-len` higher than what vLLM confirms fits at
startup (`estimated maximum model length` in logs).

---

## NVFP4 — zdy1995love/Mistral-Medium-3.5-128B-NVFP4

### Memory fit (TP=2)

| | Per node |
|---|---|
| Total NVFP4 checkpoint | ~75 GiB (63.81 GiB LM weights + vision encoder) |
| Weights at TP=2 | **~38 GiB** |
| CUDA graphs (estimate) | ~2 GiB |
| At gmu=0.75: KV available | **~51 GiB** |
| At gmu=0.75: outside headroom | **~30 GiB** |

KV headroom is much more generous than FP8 — all the long-context scenarios that are
marginal at FP8 become comfortable here.

### Quality caveat

This is a **community RTN (Round-To-Nearest) quantization with no calibration data**,
created by dequantizing the FP8 checkpoint to BF16 then converting to NVFP4A16 via
llm-compressor. RTN without calibration data is the lowest-quality quantization method —
it does not optimize the quantization grid to minimize output error. For a dense reasoning
model, this may degrade quality meaningfully compared to the official FP8 checkpoint.

For production use, prefer the official FP8 checkpoint unless NVFP4 headroom is required
for a specific workload.

### NVFP4 tooling status

Same blockers as all NVFP4 models on this cluster:
- Container 26.05+ for native b12x SM121 FP4 kernels (PR #40082, merged 2026-05-20)
- `--kv-cache-dtype fp8` required (cluster-wide multi-turn corruption investigation)
- `--quantization modelopt`
- `--tokenizer-mode mistral` still required

---

## Dense vs. MoE: Profile Fit Comparison

| | Mistral-Medium-3.5-FP8 | MiniMax-M2.7-AWQ | Step-3.5-Flash-FP8 |
|---|---|---|---|
| Architecture | Dense | MoE | MoE |
| Params (total/active) | 128B / 128B | 230B / 10B | 197B / 11B |
| Per node at TP=2 | ~67 GiB | ~61 GiB | ~97.5 GiB |
| KV per token (est.) | ~320 KiB | ~124 KiB | — |
| Concurrent at 32K (gmu=0.75) | ~2 | ~10+ | ~5 |
| Profile archetype | Big-shared | Big-shared | Fully-committed |
| Vision | ✅ | ❌ | ❌ |
| Reasoning parser | ✅ mistral | ❌ | ✅ step3p5 |
| Tool-call parser | ✅ mistral | ⚠️ unverified | ✅ step3p5 |

Dense and MoE with similar per-node weight footprints differ primarily in KV cache
efficiency: MoE models use fewer/smaller attention layers relative to their parameter count,
giving them dramatically better concurrency at long context. Mistral-Medium-3.5 at 32K
context has ~2 concurrent sessions vs MiniMax-M2.7's ~10+ at the same gmu. At short
context (8K), dense is more competitive (~8 concurrent).

---

## What to Watch For

1. **Verify checkpoint precision** — download and check `config.json` / tensor dtypes to
   confirm FP8 vs BF16 and get exact architecture values (layers, heads, head_dim).
2. **vLLM 26.04 compatibility** — confirm model architecture is supported; check if
   26.04 knows the `mistral_medium` model type (may need 26.05+ or a newer image).
3. **EAGLE speculative decoding** — if vLLM's EAGLE support is functional in the current
   image, the separate draft model (`mistralai/Mistral-Medium-3.5-128B-EAGLE`) should
   provide meaningful throughput gains for single-stream use.
4. **Official FP8 or calibrated quantization** — if Mistral releases an official
   FP4/NVFP4 with calibration data, it would be preferable to the RTN community quant.

---

## Key Links

| Resource | URL |
|---|---|
| HuggingFace (official) | https://huggingface.co/mistralai/Mistral-Medium-3.5-128B |
| HuggingFace (EAGLE draft) | https://huggingface.co/mistralai/Mistral-Medium-3.5-128B-EAGLE |
| HuggingFace (NVFP4, community) | https://huggingface.co/zdy1995love/Mistral-Medium-3.5-128B-NVFP4 |
| GGUF (unsloth) | https://huggingface.co/unsloth/Mistral-Medium-3.5-128B-GGUF |
| GGUF (bartowski) | https://huggingface.co/bartowski/mistralai_Mistral-Medium-3.5-128B-GGUF |
