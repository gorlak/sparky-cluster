# MiMo-V2.5 on 2× DGX Spark — Candidate Fact Sheet

**Last updated:** 2026-08-12
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each (121 GiB usable), ConnectX-7 200 Gbit RoCE
**Installed quant:** none — weights not held
**Target quant:** NVFP4 (community), ~85.5 GiB/node at TP=2

> **Blocked on:** no **released** vLLM serves the `mimo_v2` architecture.
> **Clears when:** `MiMoV2ForCausalLM` lands in a released vLLM **and** an `nvcr.io/nvidia/vllm` tag ships it for sm_121/aarch64.

This is a [candidate](README.md), not a fleet model and not a rejection: it passes every fit
and speed gate we apply, and the only thing in the way is a serving stack we do not have.

---

## Model Overview

- **Developer:** Xiaomi ([XiaomiMiMo](https://huggingface.co/XiaomiMiMo)) · MIT licence · released 2026-04-27
- **Architecture:** **MoE**, `MiMoV2ForCausalLM` / `model_type: mimo_v2`. 48 layers, hidden 4096, **256 routed experts, 8 active per token**, no shared experts. Built on the MiMo-V2-Flash backbone.
- **Parameters:** **~310B total / ~15B active per token**
- **Context:** **1,048,576 native** (`max_position_embeddings`) — 4× the fleet's current best
- **Omnimodal:** text + **image + video + audio** in one model. Dedicated **729M ViT** (hybrid window attention) and **261M audio encoder** initialised from MiMo-Audio.
- **Attention:** hybrid. Full-attention layers use 64 heads / **4 KV heads**, `head_dim` 192 (K) and 128 (V), `rope_theta` 10,000,000. Sliding-window layers use 64 heads / **8 KV heads** and a **128-token window**, with `add_swa_attention_sink_bias: True` (attention sinks). Model card gives the SWA:full ratio as **5:1**.
- **HuggingFace:** https://huggingface.co/XiaomiMiMo/MiMo-V2.5

`custom_code` / `trust_remote_code` — the architecture ships as remote code, which is the
root of the blocker below: vLLM needs a **registered implementation**, not merely loadable
Python. (The `step-3.7-flash` bring-up taught this distinction the expensive way — a native
module and a generic fallback are different models in practice.)

---

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Fit |
|---|---|---|---|---|
| BF16 | — (not published) | ~620 GB | ~289 GiB | ❌ Does not fit |
| FP8 (official) | `XiaomiMiMo/MiMo-V2.5` | **315.0 GB** | **146.7 GiB** | ❌ Misses 121 GiB by ~26 |
| **NVFP4** | **`mitomtuna/MiMo-V2.5-0703-NVFP4`** | **183.6 GB** | **85.5 GiB** | **⚠️ Fits — blocked on vLLM support** |
| NVFP4 | `lukealonso/MiMo-V2.5-NVFP4` | 183.8 GB | 85.6 GiB | ⚠️ Same |
| NVFP4 | `shadowlilac/MiMo-V2.5-NVFP4` | 188.2 GB | 87.6 GiB | ⚠️ Same |
| MXFP4 | `djdeniro/MiMo-V2.5-MXFP4` | 1.9 GB | — | ❌ Placeholder — 2 shards, not a checkpoint |
| GGUF | `unsloth` · `bartowski` · `AesSedai` | various | — | ❌ No serving stack here; llama.cpp splits **layers**, forfeiting the TP=2 bandwidth win |

The official checkpoint is FP8-native (`quantization_config.quant_method: fp8`,
`save_format: fp8`, `total_size` 315,031,102,208 B across 73,081 tensors), with every
`self_attn.o_proj` in `ignored_layers` — held at higher precision. Its index declares
`tp_size: 4`, the vendor's reference shape.

**No first-party or NVIDIA NVFP4 build exists.** The three above are community uploads with
0–1 likes each — normally a strong reason for suspicion, and the failure mode that produced
[DEF-0013](../../defects.md) (a "NVFP4" repo whose real quant was `NVFP4_AWQ`, which vLLM
still refuses). Two things argue they are sound anyway:

- **Three independent quantizers agree within 2%** (183.6 / 183.8 / 188.2 GB). A truncated
  or partial upload does not land on the same number as two strangers' uploads.
- **All three retain `audio_tokenizer/`**, so the multimodal towers survived quantization —
  the specific thing that goes missing when a quantizer treats an omnimodal model as an LLM.

Neither check is a substitute for loading the weights. Both are free, and they are what
[model-evaluation](../../../skills/model-evaluation/SKILL.md) asks for before a 184 GB
download.

---

## NVFP4 — `mitomtuna/MiMo-V2.5-0703-NVFP4`

### Memory fit

| | Per node |
|---|---|
| Usable unified memory | 121 GiB |
| Model weights at TP=2 | **85.5 GiB** |
| KV cache at the **full 1M** context | **~9.6 GiB** |
| **Free headroom** | **~26 GiB** |

The KV number is the surprise, and it is the hybrid attention doing the work. Only the
full-attention layers scale with context; the sliding-window layers are pinned to a
**128-token** window and cost ~13 MB/node in total.

At 5:1 SWA:full over 48 layers → 8 full-attention layers. Each holds 2 KV heads per rank at
TP=2, at `192 + 128` elements in FP16:

```
8 layers × 2 KV heads × 320 elem × 2 B = 10,240 B per token per node
1,048,576 tokens × 10,240 B ≈ 10.2 GB ≈ 9.6 GiB per node
```

**The 1M context is affordable, not aspirational** — with ~26 GiB still spare. That would make
this the long-context model of the fleet by a wide margin, against
`nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4`'s current 131k served.

> ⚠️ The 5:1 ratio is from the model card, **not** from `config.json` (which carries no
> `layer_types`). If the true ratio is richer in full-attention layers the KV cost scales
> linearly with it — at 3:1 (12 full layers) the 1M budget becomes ~14.3 GiB/node, still
> comfortable. Verify against the loaded model before trusting a `max_model_len`.

### Speed — the arithmetic gate

Decode on GB10 is memory-bandwidth-bound at **273 GB/s**. The NVFP4 build averages
`183.6 GB ÷ 310B ≈ 0.59 bytes/param`, so ~15B active weights cost:

```
15B × 0.59 B = 8.9 GB per token, ÷ 2 nodes = 4.44 GB per node
273 GB/s ÷ 4.44 GB = ~61 tok/s ceiling at TP=2
```

The forum result corroborates both the model and the rule: **38.8 tok/s at TP=3 with MTP**,
where per-node active bytes are 2.96 GB → a ~92 tok/s ceiling → **42% of ceiling**, inside
the 30–85% band this cluster measures. Extrapolating that fraction to TP=2 gives a realistic
**~26 tok/s**.

**So this is not a speed win.** ~26 tok/s sits with `qwen3-vl-235b` (23.8) and `minimax-m2.7`
(24.9), well behind `qwen3.6-35b-a3b` (100.2). The case for it is **capability and context** —
audio, video, 1M — not throughput, and it should never be proposed as a fast model.

### TP=2 divisibility ✅

Worth stating explicitly because the forum post's summary claims the opposite. Every
sharded dimension divides by 2:

| | value | ÷2 |
|---|---|---|
| `num_attention_heads` | 64 | 32 ✅ |
| `num_key_value_heads` | 4 | 2 ✅ |
| `swa_num_attention_heads` | 64 | 32 ✅ |
| `swa_num_key_value_heads` | 8 | 4 ✅ |
| `n_routed_experts` | 256 | 128 ✅ |
| `vision_config.num_heads` / `num_key_value_heads` | 32 / 8 | 16 / 4 ✅ |

64 and 4 are precisely what does **not** divide by **3**, so TP=3 is the awkward shape and
TP=2 the clean one. The 3-node deployment was almost certainly chosen for KV budget at 1M
context, not divisibility. **Our shape is better suited than the one that has been proven.**

### Tooling status

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ⛔ **No released version** | Stable vLLM does not support `mimo_v2`. This is the blocker. |
| **Pre-built image** | ❌ Wrong platform | `vllm/vllm-openai:mimov25-cu129` — **CUDA 12.9**, the generic `vllm-openai` lineage. Our cluster is CUDA 13.0 / sm_121 and runs NGC images because that lineage cannot serve GB10 at all. |
| **Community GB10 build** | ⚠️ Fork only | A fork of `eugr/spark-vllm-docker` (PR #251) — the same project that carries [`deepseek-v4-flash`](../deepseek-v4-flash.md)'s PR #219. |
| **Required patches** | ⚠️ Two, hand-written | A **FusedMoE zero-fill** for corrupted NVFP4 output, and an **`attention_sink_bias` padding fix** for the MTP draft layer — the latter follows directly from `add_swa_attention_sink_bias: True`. |
| **NGC container** | ⛔ Not shipped | 26.07-py3 is vLLM 0.24.0. No `mimo_v2`. |

Building our own patched image is possible — we already derive one
(`dgx-spark/vllm:26.07-xgrammar-fix`) — but that image patches **one** vendored dependency.
This would mean carrying a forked vLLM with two unmerged correctness patches for the model's
*core* MoE and speculative paths, on weights nobody first-party quantized. That is a
different order of debt, and the cluster's rule is that a workaround needs a removal
condition; here the removal condition *is* the clears-when.

### Draft serve flags (untested — for when it clears)

```
--tensor-parallel-size 2
--max-model-len 1048576
--gpu-memory-utilization 0.85
--limit-mm-per-prompt image=4,video=1,audio=4
--trust-remote-code
```

Plus the fleet's standard tool-calling pair once the parser name is known. **Set every one of
these on both ranks** — `--limit-mm-per-prompt` is in `BOTH_RANK_FLAGS` because a
model-configuring flag on one rank deadlocks TP=2 startup.

**Disable thinking.** The forum reports quality rising **88.9 → 97.3 and latency halving**
with thinking off — a large enough swing to be the first thing configured, and a direct echo
of the `step-3.7-flash` finding that an unconditionally-opened `<think>` block is where
output quality goes to die.

---

## Assessment

**The best-shaped candidate screened for this cluster to date.** It is the first model that
passes the fit gate, the active-parameter speed gate, TP=2 divisibility, *and* brings a
capability the fleet does not have at any speed.

| | |
|---|---|
| ✅ **Fits** with room — 85.5 GiB/node, ~26 GiB spare after a full 1M context |
| ✅ **1M context affordable**, not nominal — every fleet profile today serves far less than it holds |
| ✅ **Audio + video**, which no fleet model has. Would subsume the README's outstanding *voice mode (STT/TTS)* future-work item into the serving model instead of a second stack |
| ✅ **TP=2-clean**, and better matched to 2 nodes than the 3-node deployment that exists |
| ⚠️ **~26 tok/s** expected — mid-fleet, not a fast model |
| ⚠️ **Community quants only** — no first-party or NVIDIA NVFP4 |
| ⛔ **Unserveable today** — needs a forked vLLM with two correctness patches |

It does **not** improve vendor diversity: Xiaomi is a third Chinese vendor, and the European
slot is filled by [`mistral-small-4-119b-2603-nvfp4`](../../profiles.md). Its argument is
capability, not sourcing.

**Do not download 184 GB against a serving stack we do not have.** The weights are the cheap
part to re-acquire; the screen above is the expensive part, and it is now written down.

---

## What to Watch For

1. **`mimo_v2` in a released vLLM** — the gating item. Watch the vLLM release notes for `MiMoV2ForCausalLM` in the supported-models list.
2. **An NGC tag that ships it** — `nvcr.io/nvidia/vllm` newer than `26.07-py3`, carrying that vLLM, built for sm_121/aarch64. Tracked by [version-discovery](../../../skills/version-discovery/SKILL.md).
3. **A first-party or NVIDIA NVFP4 build** — would remove the community-quant risk entirely, and is the single change that most improves this candidate's odds.
4. **`eugr/spark-vllm-docker` PR #251 merged** — the same project gates `deepseek-v4-flash`; one merged image could clear two candidates.
5. **The 5:1 SWA:full ratio confirmed** — decides whether 1M is really affordable at TP=2.

---

## Key Links

| Resource | URL |
|---|---|
| HuggingFace model card | https://huggingface.co/XiaomiMiMo/MiMo-V2.5 |
| vLLM recipes page (states stable vLLM lacks support) | https://recipes.vllm.ai/XiaomiMiMo/MiMo-V2.5 |
| NVIDIA forum: MiMo V2.5 Omni on 3× DGX Spark, TP=3 + MTP + 1M ctx | https://forums.developer.nvidia.com/t/mimo-v2-5-omni-on-3x-dgx-spark-tp-3-mtp-1m-context-39-tok-s/373948 |
| SGLang cookbook entry | https://lmsysorg.mintlify.app/cookbook/autoregressive/Xiaomi/MiMo-V2.5 |
| NVFP4 candidate (primary) | https://huggingface.co/mitomtuna/MiMo-V2.5-0703-NVFP4 |
| NVFP4 candidate (second) | https://huggingface.co/lukealonso/MiMo-V2.5-NVFP4 |
| NVFP4 candidate (third) | https://huggingface.co/shadowlilac/MiMo-V2.5-NVFP4 |
| MiMo-Audio (the audio encoder's origin) | https://huggingface.co/XiaomiMiMo/MiMo-Audio-7B-Instruct |

---

## Re-assessment log

- **2026-08-12 — screened, filed as a candidate.** Arrived as a third-party tip. FP8 ruled out on fit (146.7 GiB/node); three community NVFP4 builds found and cross-checked; TP=2 divisibility verified against `config.json` against a contrary claim in the source forum post; 1M-context KV derived at ~9.6 GiB/node from `sliding_window: 128`; speed ceiling put at ~61 tok/s with ~26 tok/s expected. Blocker identified as absent released-vLLM support, corroborated by the vLLM recipes page and a GB10 deployment that required a two-patch fork. **Not downloaded.**
