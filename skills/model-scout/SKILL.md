---
name: model-scout
description: Search for new models or quantizations that would perform well on this cluster. Use when asked to check for better models, newer quantizations, or what's new on HuggingFace that fits the hardware.
---

## Cluster Constraints

- **Hardware:** 2× NVIDIA GB10 (SM 12.1), 121 GiB unified memory each
- **Runtime:** `nvcr.io/nvidia/vllm:26.04-py3` (vLLM 0.19, no Ray)
- **Memory budget per node at TP=2:** ~108.9 GiB (0.90 × 121 GiB)
- **Currently running:** `stepfun-ai/Step-3.5-Flash-FP8` (~97.5 GiB/node, ~11 GiB headroom)
- **Do not suggest:** `Qwen3.5-122B-A10B-FP8` (froze sparky), any model requiring Ray

## What to Look For

Search HuggingFace, vLLM release notes, and model leaderboards for:

1. **Better headroom:** Models with FP8 footprint well under 108.9 GiB/node — ideally
   under 80 GiB/node so KV cache and prefix caching have room to breathe.
   Disk size ≈ VRAM footprint for FP8/quantized models.

2. **Newer quantizations of current model:** Any new FP8, AWQ, or GPTQ releases
   of Step-3.5-Flash, or successor models from StepFun AI.

3. **Strong reasoning models with standard vLLM support:** Prioritize models that
   work with the stock NVIDIA vLLM image — no custom forks, no special patches.

4. **SM 12.1 compatibility:** Must work on GB10 Blackwell. Models requiring
   CUTLASS kernels need vLLM 26.04+. Flag any that are known to have issues.

5. **MoE vs dense tradeoff:** For MoE models, vLLM loads ALL experts into VRAM —
   use total parameter count for memory math, not active parameters per token.

## Report Format

For each candidate, report:
- Model name and HuggingFace link
- Format and disk size
- Estimated VRAM per node at TP=2
- Headroom remaining (~108.9 GiB budget)
- Any known vLLM compatibility issues
- Why it's better or worse than what we're running

Flag anything that needs investigation before committing to a download.
