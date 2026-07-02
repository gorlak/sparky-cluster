# MiniMax-M3 on 2× DGX Spark — Status Tracker

**Last updated:** 2026-07-02
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each
**Status:** ❌ Does not fit — weights exceed per-node memory at TP=2
**Available quant:** NVFP4 — `nvidia/MiniMax-M3-NVFP4` (~250 GiB, ~125 GiB/node at TP=2 — exceeds 121 GiB limit)

---

## Model Overview

- **Developer:** MiniMax AI
- **Architecture:** Sparse Mixture-of-Experts (MoE), multimodal
- **Total parameters:** ~428B
- **Active parameters per token:** ~23B (A23B)
- **Context window:** 1,000,000 tokens (native 1M context)
- **Attention:** MiniMax Sparse Attention (MSA) — 9× prefill / 15× decode speedup vs M2.7 at 1M context
- **Multimodal:** Native text + image + video from inception (deeper fusion than M2.7's added ViT)
- **HuggingFace (BF16):** https://huggingface.co/MiniMaxAI/MiniMax-M3
- **HuggingFace (NVFP4):** https://huggingface.co/nvidia/MiniMax-M3-NVFP4
- **Technical report:** arXiv:2606.13392

---

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Fit |
|---|---|---|---|---|
| BF16 | MiniMaxAI/MiniMax-M3 | ~856 GiB | ~428 GiB | ❌ Does not fit |
| **NVFP4** | **nvidia/MiniMax-M3-NVFP4** | **~250 GiB** | **~125 GiB** | **❌ Does not fit (exceeds 121 GiB)** |

Neither format fits on this 2-node cluster at TP=2.

---

## NVFP4 — nvidia/MiniMax-M3-NVFP4

| | Value |
|---|---|
| NVFP4 checkpoint disk size | **~250 GiB** (88 shards) |
| Per node at TP=2 | **~125 GiB** |
| Available per node (GB10) | **121 GiB** |
| **Shortfall** | **~4 GiB before KV or CUDA graphs** |

At TP=2 — the maximum this cluster supports (2 nodes × 1 GPU each) — the weights alone
exceed the 121 GiB usable memory per node by ~4 GiB. There is no gmu setting that fixes
this; `gpu_memory_utilization` controls KV cache allocation, not the weight footprint.

**M3 requires at minimum 4 GPUs (TP=4) to distribute the weight shards below 121 GiB per node.**
The official NVIDIA recipe uses `--tensor-parallel-size 8` (targeting 8× B200/H200 nodes).

This cluster cannot run MiniMax-M3 in any configuration.

---

## Comparison: M2.7 vs M3

| | MiniMax-M2.7-AWQ (installed) | MiniMax-M3-NVFP4 |
|---|---|---|
| Total params | ~230B | ~428B |
| Active params/token | ~10B | ~23B |
| Context window | 196K (deployed at 128K) | 1M |
| Disk (installed quant) | 122 GiB | ~250 GiB |
| Per node at TP=2 | ~61 GiB | ~125 GiB |
| Fits on this cluster? | ✅ Yes (gmu=0.75) | ❌ No |
| Attention mechanism | Standard | MiniMax Sparse Attention (MSA) |
| Multimodal | Add-on ViT | Native from training |
| Tool-call parser | ⚠️ Unverified | `minimax_m3` |
| Reasoning parser | — | `minimax_m3` |
| NVFP4 available? | ✅ (not installed) | ✅ (official NVIDIA) |

M3 is approximately 2× M2.7 in both total and active parameter count. The 1M context and
MSA are significant capability upgrades but the hardware requirement doubles with them.

---

## What Would It Take

This is purely academic given the hardware, but for reference:

- **4 nodes at TP=4:** ~62.5 GiB/node — would fit with headroom. Not possible with 2 nodes.
- **A single GB200 NVL72 rack:** the actual intended target.
- **A future 4-node DGX Spark expansion:** if snoopy and sparky were joined by two more
  GB10 nodes, TP=4 would fit M3 comfortably.

---

## What to Watch For

1. **Official MiniMaxAI FP8 quant** — if released, would be ~428 GiB / ~214 GiB per node at TP=2. Still doesn't fit.
2. **Smaller distilled M3 variants** — MiniMax or community may release smaller MoE configurations. Worth checking if an A10B or A15B active-parameter variant appears.
3. **Cluster expansion** — M3 is the natural candidate if a 3rd or 4th GB10 node is ever added.

---

## Key Links

| Resource | URL |
|---|---|
| HuggingFace (BF16) | https://huggingface.co/MiniMaxAI/MiniMax-M3 |
| HuggingFace (NVFP4, NVIDIA) | https://huggingface.co/nvidia/MiniMax-M3-NVFP4 |
| Technical report | https://arxiv.org/abs/2606.13392 |
