# MiniMax-M2.7 on 2× DGX Spark — Status Tracker

**Last updated:** 2026-07-02
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE
**Status:** ✅ Deployed and serving (`minimax-m2.7-awq` profile)
**Installed quant:** AWQ 4-bit compressed-tensors — `cyankiwi/MiniMax-M2.7-AWQ-4bit` (122 GiB measured, ~61 GiB/node at TP=2)

---

## Model Overview

- **Developer:** MiniMax AI
- **Architecture:** Sparse Mixture-of-Experts (MoE), 62-layer Transformer
- **Total parameters:** ~230B
- **Active parameters per token:** ~10B (256 local experts, 8 per token)
- **Attention:** 48 heads, 8 KV heads (GQA), head dim 64
- **Hidden size:** 3072
- **Context window:** 196,608 tokens (trained); deployed at 131,072 (~128K)
- **HuggingFace (BF16):** https://huggingface.co/MiniMaxAI/MiniMax-M2.7
- **HuggingFace (AWQ, installed):** https://huggingface.co/cyankiwi/MiniMax-M2.7-AWQ-4bit
- **HuggingFace (NVFP4):** https://huggingface.co/nvidia/MiniMax-M2.7-NVFP4

---

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Fit |
|---|---|---|---|---|
| BF16 | MiniMaxAI/MiniMax-M2.7 | ~460 GiB | ~230 GiB | ❌ Does not fit |
| **AWQ 4-bit** | **cyankiwi/MiniMax-M2.7-AWQ-4bit** | **122 GiB** | **~61 GiB** | **✅ Installed** |
| NVFP4 | nvidia/MiniMax-M2.7-NVFP4 | ~115 GiB est. | ~58 GiB est. | ✅ Likely fits — not yet analyzed |

---

## AWQ 4-bit — cyankiwi/MiniMax-M2.7-AWQ-4bit (Installed)

This is a **community quantization** by `cyankiwi`, not an official MiniMaxAI release.
The official HuggingFace repo offers BF16 only; this is the only viable AWQ quant
available at time of download.

> ⚠️ The HuggingFace listing for `cyankiwi/MiniMax-M2.7-AWQ-4bit` shows "37B params"
> in its metadata — this is incorrect. The model is the full 230B MiniMax-M2.7.

**Quantization method:** `compressed-tensors` (pack-quantized int4), detected automatically
by vLLM from `config.json`. Do **not** pass `--quantization awq` or `--quantization
awq_marlin` — vLLM auto-detects `compressed-tensors` and adding an explicit flag causes
double-quantization.

---

### Memory (Measured)

All numbers from the installed model at `/opt/vllm/models/MiniMax-M2.7-AWQ-4bit`.

| | Value |
|---|---|
| **Disk size (measured)** | **122 GiB** |
| Weights per node at TP=2 | **~60.93 GiB** |
| CUDA graphs per node (measured) | **~0.65 GiB** |
| KV per token | **~124 KiB** (62 layers × 2 × 8 KV heads × 64 dim × bf16) |

### KV budget at gmu=0.75 (current deployment)

| | Per node |
|---|---|
| vLLM budget (0.75 × 121 GiB) | 90.75 GiB |
| Weights + CUDA graphs | 61.58 GiB |
| **KV available** | **~29 GiB** |
| **Outside headroom** | **~30 GiB** |

At `max_model_len=131072`: KV per full-context sequence ≈ 131K × 124 KiB ≈ **~16 GiB**.
That gives room for **1–2 concurrent 128K sessions** — comfortable for single/few-user use.

### Alternative gmu settings

| gmu | vLLM budget | KV available | Outside headroom | Use case |
|---|---|---|---|---|
| 0.65 | 78.65 GiB | ~17 GiB | ~42 GiB | Max dev headroom; 1 concurrent 128K session |
| **0.75 (current)** | **90.75 GiB** | **~29 GiB** | **~30 GiB** | **1–2 concurrent 128K; comfortable dev margin** |
| 0.85 | 102.85 GiB | ~41 GiB | ~18 GiB | 2–3 concurrent 128K; less dev room |
| 0.90 | 108.9 GiB | ~47 GiB | ~12 GiB | Max KV; effectively no dev headroom |

**Profile archetype:** Big-shared with dev headroom. Both nodes are TP'd together but each
keeps ~30 GiB free for OS, dev work, and other containers.

---

## Tooling Status

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ✅ Working | compressed-tensors int4 auto-detected; no special flags |
| **Docker image** | ✅ 26.04-py3 | No issues on GB10 |
| **TP=2 multinode** | ✅ Deployed | Same native torch.distributed setup as other models |
| **`--trust-remote-code`** | ✅ Required | Checkpoint ships `modeling_minimax_m2.py` |
| **Tool-call parser** | ⚠️ Unverified | MiniMax parser name in vLLM 0.19 / 26.04 not yet confirmed; omitted from profile |
| **`--kv-cache-dtype fp8`** | ⚠️ Not tested | Same cluster-wide caveat; not enabled |
| **`--enable-prefix-caching`** | ⚠️ Not tested | Same cluster-wide caveat; not enabled |

---

## Production Serve Flags

From the active `minimax-m2.7-awq` profile. Worker node uses the same flags minus
`--master-addr` and with `--node-rank 1`.

```
vllm serve /models/MiniMax-M2.7-AWQ-4bit \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name minimax-m2 \
    --tensor-parallel-size 2 \
    --distributed-executor-backend mp \
    --nnodes 2 \
    --node-rank 0 \
    --master-addr 10.0.200.12 \
    --trust-remote-code \
    --max-model-len 131072 \
    --gpu-memory-utilization 0.75 \
    --enable-chunked-prefill
```

`max-model-len 131072` is conservative vs the trained 196,608. There is KV budget to push
toward 196K at this gmu (16 GiB per 128K session → ~29 GiB available ≈ ~1.8 concurrent at
full 196K). Not yet tested at the trained maximum.

---

## Context Window: Deployed vs. Trained

The model is trained to 196,608 tokens but deployed at 131,072. This is a deliberate
conservative choice:

- At 196K: ~24 GiB per session, room for 1 concurrent at gmu=0.75
- At 131K: ~16 GiB per session, room for 1–2 concurrent
- No RoPE extrapolation needed (131K is well within 196K trained range)

To extend to the full trained context, raise `max_model_len` to 196608 in the profile —
vLLM will refuse to start if KV budget doesn't cover it, which is the safety check.

---

## NVFP4 — nvidia/MiniMax-M2.7-NVFP4

Official NVIDIA release. Not yet analyzed or downloaded for this cluster.

Estimated footprint (~230B at FP4): ~115 GiB total / ~58 GiB per node at TP=2 — fits
comfortably with significant headroom, similar to how Step-3.7-Flash-NVFP4 fits vs its
FP8 counterpart. Expected blockers mirror Step-3.7-Flash-NVFP4:
- Container 26.05+ for native b12x SM121 FP4 kernels
- `--kv-cache-dtype fp8` required (shared cluster-wide investigation)
- `--quantization modelopt` flag

Worth analyzing and downloading after the NVFP4 investigation track (fp8 KV cache +
container bump) resolves for Step-3.7-Flash.

---

## What to Watch For

1. **Tool-call parser name** — identify the correct `--tool-call-parser` value for
   MiniMax-M2.7 in vLLM 0.19 / 26.04 and add to the profile. Check vLLM's parser
   registry or model card.
2. **Full context (196K) testing** — raise `max_model_len` to 196608, verify vLLM
   accepts it at gmu=0.75, and measure TTFT at full context.
3. **Official quantization** — MiniMaxAI may release an official FP8 or NVFP4 quant.
   Would replace the community AWQ quant.
4. **fp8 KV cache + prefix caching** — same cluster-wide investigation; enablement here
   after the Step-3.5-Flash investigation clears.

---

## Key Links

| Resource | URL |
|---|---|
| HuggingFace (BF16, official) | https://huggingface.co/MiniMaxAI/MiniMax-M2.7 |
| HuggingFace (AWQ, installed) | https://huggingface.co/cyankiwi/MiniMax-M2.7-AWQ-4bit |
| HuggingFace (NVFP4, NVIDIA) | https://huggingface.co/nvidia/MiniMax-M2.7-NVFP4 |
