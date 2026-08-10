# Qwen3-Coder-Next on 2× DGX Spark — Status Tracker

**Last updated:** 2026-07-02
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE
**Status:** 🔎 Candidate — best-fit agentic-coding model found in the 2026-07-02 discovery sweep
**Target quant:** NVFP4 — `RedHatAI/Qwen3-Coder-Next-NVFP4` (~48 GiB) or the GB10-native `ucbye/Qwen3-Coder-Next-NVFP4-GB10` (~46 GiB)

---

## Model Overview

- **Developer:** Qwen (Alibaba)
- **Purpose:** Purpose-built **agentic coding** model — the chat template *is* an
  agentic tool-call harness (`<tool_call><function=…><parameter=…>`), "You are Qwen,
  a helpful AI assistant that can interact with a computer to solve tasks."
- **Architecture:** `Qwen3NextForCausalLM` / `qwen3_next` — hybrid **Gated DeltaNet +
  attention** MoE (linear-attention hybrid; cheap long-context KV).
- **Total parameters:** ~79.7B (MoE, A3B-class active) — **text-only, not a VLM.**
- **HuggingFace (BF16):** https://huggingface.co/Qwen/Qwen3-Coder-Next
- **HuggingFace (FP8, official):** https://huggingface.co/Qwen/Qwen3-Coder-Next-FP8
- **HuggingFace (NVFP4, reputable):** https://huggingface.co/RedHatAI/Qwen3-Coder-Next-NVFP4
- **HuggingFace (NVFP4, GB10-native):** https://huggingface.co/ucbye/Qwen3-Coder-Next-NVFP4-GB10

> **Why this one now:** the repo's Step-3.7-Flash candidate is troubled on 26.06
> because it is a **vision-language** model and vLLM's VLM path for it is shaky.
> Qwen3-Coder-Next is **text-only**, sidestepping that class of problem entirely,
> and is far smaller.

---

## Quantization Formats & Footprint

| Format | Source | Disk | Fit |
|---|---|---|---|
| BF16 | Qwen/Qwen3-Coder-Next | ~160 GiB | ❌ too big at TP=2 fully-committed; per-node no |
| FP8 | Qwen/Qwen3-Coder-Next-FP8 | ~80 GiB | ✅ per-node single (80/121) or TP=2 (40/node) |
| **NVFP4** | **RedHatAI/…-NVFP4** / **ucbye/…-NVFP4-GB10** | **~46–48 GiB** | ✅✅ huge headroom every shape |

NVFP4 is the pick on Blackwell — native FP4 tensor cores, and it leaves the most room.

### Fit math (121 GiB usable/node)

| Shape | Weights/node | Free/node | Notes |
|---|---|---|---|
| **NVFP4, per-node single** (one instance each node, **no TP, no NCCL**) | ~46 | ~75 | per-node dual–style; simplest, most robust |
| **NVFP4, TP=2 shared** | ~23 | ~98 | enormous KV/prefix-cache room |
| FP8, per-node single | ~80 | ~41 | still comfortable |

---

## vLLM Compatibility & Risks

- **`qwen3_next` arch support:** requires a vLLM new enough to have the Gated-DeltaNet
  hybrid path. **Verify on our 26.06 container before committing** — but the existence of
  multiple **GB10-native NVFP4 quants** (`ucbye` ~28k downloads, `saricles`, `gdubicki`,
  all tagged `vllm`+`GB10`) is strong evidence dual-Spark users serve this exact model.
- **NVFP4 needs 26.06** (confirmed working 2026-07-02) — same container the
  Step-3.7 work is on.
- Ships its own vLLM tool-parser (`qwen3coder_tool_parser_vllm.py`) — wire the
  `--tool-call-parser` accordingly for agentic use.

## Why better / worse than what we run

- **Better:** text-only (no VLM breakage), purpose-built for agentic coding + tool use,
  tiny footprint → can run **per-node with no cross-node NCCL** (removes the whole class
  of GB10 multinode hang risk), or TP=2 with massive prefix-cache headroom.
- **Worse:** ~80B is smaller than MiniMax-M2.7 (~230B) / Step (~197B); for broad
  "general technical" reasoning the big MoEs may still edge it. Best treated as a
  **dedicated coding engine**, potentially alongside a big general model.
