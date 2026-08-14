> ⚰️ **RETIRED 2026-08-12** — dense: 3.44 tok/s, 84% of its bandwidth ceiling.
> The **verdict** and its *reconsider-when* live in [`../tombstones.md`](../tombstones.md), which owns them; nothing here restates one.
>
> This sheet is kept for the **engineering**: the memory math, the footprints, the
> quant findings, the workarounds. Its status and "next steps" sections are frozen
> in place and are **history, not a plan** — read them as what was believed then.

# Mistral-Medium-3.5-128B on 2× DGX Spark — Status Tracker

**Last updated:** 2026-08-08
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE
**Installed quant:** **NVIDIA ModelOpt `MIXED_PRECISION`** — staged as `Mistral-Medium-3.5-128B-NVFP4`, **89 GiB measured** (~44.5 GiB/node at TP=2)
**Profile:** [`mistral-medium-3.5-128b-nvfp4`](../../profiles.md) — TP=2, gmu 0.75, `max_model_len` 131072, container 26.07

> **The staged checkpoint is not what this sheet originally targeted.** It is NVIDIA's
> ModelOpt quant of `mistralai/Mistral-Medium-3.5-128B` (nvidia-open-model-license,
> modelopt 0.37.0) — *not* the community `zdy1995love` NVFP4 repo analyzed below, and not
> the official FP8. Three things follow, each measured from the staged files rather than
> assumed:
>
> | | what the sheet assumed | what is actually staged |
> |---|---|---|
> | quant | plain NVFP4, or official FP8 | **`quant_algo: MIXED_PRECISION`** — 367 layers FP8 + 249 NVFP4, per-layer map in `quantized_layers` |
> | footprint | ~64 GiB (NVFP4) / ~128 GiB (FP8) | **89 GiB** — between the two, as mixed precision implies |
> | tokenizer | Mistral-native, needs `--tokenizer-mode mistral` | **both** formats present (`tokenizer.json` + `tekken.json`/`params.json`); we take the HF path, vLLM's default |
>
> It also declares **`kv_cache_quant_algo: FP8`**, which puts it in DEF-0007's territory
> (FP8 KV × prefix caching → multi-turn corruption, never re-tested since vLLM 0.19).
> `--enable-prefix-caching` is deliberately omitted from the profile for that reason.
>
> Config facts for the memory math: 88 layers, 8 KV heads (GQA), head_dim 128,
> hidden 12288, vocab 131072, `max_position_embeddings` 262144 via **YaRN factor 64 from
> an original 4096**. `text_config.model_type` is `ministral3`, matching vLLM's
> Ministral-3-Reasoning recipe — hence `--reasoning-parser mistral` (`[THINK]`) and
> `--tool-call-parser mistral` (`[TOOL_CALLS]`), both confirmed in the chat template.

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

---

## The retired profile configuration

Kept because reviving this model costs a deploy and the config is the expensive part to
reconstruct — the parser names, the flag combinations and the memory math were each learned
the hard way. Nothing parses it; `ansible/profiles/*.yml` is the allowlist and this is not
there.

### `mistral-medium-3.5-128b (official FP8)`

```yaml
---
#    WHY: **dense**. GB10 decode is memory-bandwidth-bound — every token re-reads every
#    active weight — and a dense model's active set IS its whole weight set. Measured
#    2026-08-11 on the FP8 build: **3.44 tok/s single-stream, 84% of its theoretical
#    ceiling**. There is no flag, quant or container that fixes this; the NVFP4 sibling
#    only reaches ~5.7 tok/s by the same arithmetic. The replacement is
#    `mistral-small-4-119b-2603-nvfp4` — same vendor, 119B total but ~6B ACTIVE.
#
#    Verdict is owned by docs/models/tombstones.md; this file keeps the CONFIG and what
#    was learned building it, which was a great deal: the Mistral flag TRIO
#    (`--tokenizer-mode` + `--config-format` + `--load-format mistral`, all three or the
#    halves disagree), the dual-layout repo, and the DEF-0012 diagnosis. Re-verify
#    parsers, architecture and quant algo before reviving.
#    See docs/models/retired/README.md.
#
# Mistral-Medium-3.5-128B, OFFICIAL FP8 — TP=2 across both nodes, container 26.07.
#
# WHY THIS AND NOT THE NVFP4 SIBLING. `mistral-medium-3.5-128b-nvfp4` (NVIDIA's quant of
# this same model) has never served — DEF-0012 — and the 2026-08-11 sweep found the reason
# is structural, not incidental: that repo ships ONLY the HF weight layout
# (`model.safetensors.index.json`, **no `consolidated.safetensors.index.json`**), so the
# proven Mistral recipe's `--load-format mistral` has no native index to read and may be
# unrunnable there no matter what we set. THIS repo — Mistral's own — ships **both**
# layouts, so the native path is actually available.
#
# It is also the better model on this cluster's own terms: official rather than a
# third-party quant, and **dense 128B**, which is the Tier-1 shape. `Mistral-Small-4` is
# sparse and lands at ~33 GiB/node, leaving most of the 121 GiB pool idle —
# [[model-discovery]] is explicit that fitting by leaving hardware idle is under-using it.
#
# SIZING — READ THIS BEFORE TRUSTING ANY BYTE COUNT. The repo holds the SAME weights twice:
# 124.4 GiB of native `consolidated-*` plus 124.4 GiB of HF `model-*`, 248.9 GiB on the
# Hub for a 124.4 GiB model. Summing `*.safetensors` reports double, and on 2026-08-11 that
# arithmetic wrongly ruled this model out of a sweep as "too big". `sparky download` now
# takes one layout (`--layout native|hf|both`, default native). Size from ONE layout, and
# read precision from `.safetensors.parameters`, never from a repo's tags:
#   {"F8_E4M3": 121_802_588_160, "BF16": 5_901_622_016}   ← genuinely FP8, dtype-verified
#
# ⚠️ THE VISION RISK, AND IT IS THE ONE THING TO WATCH ON FIRST ACTIVATION.
# The two config halves declare quantization DIFFERENTLY, and only one carries exclusions:
#   config.json  (HF)     → quant_method fp8, and
#                           modules_to_not_convert: [model.vision_tower,
#                                                    model.multi_modal_projector, lm_head]
#   params.json  (native) → quantization: {qformat_weight: fp8_e4m3, qscheme_act: TENSOR}
#                           — NO exclusion list at all
# We are taking the native path, so the exclusion list is the half we do not read. That is
# **exactly the shape of Step-3.7's failure the same day**: text quality passed while the
# vision probe returned degenerate garbage, because a vision tower quantized without
# calibration produces numerically meaningless embeddings feeding a healthy LM.
#
# The mitigating evidence: 5.9B params are stored **BF16 on disk** — almost certainly the
# vision tower, projector and lm_head — so the tensors are already unquantized whichever
# layout you load. The open question is only whether vLLM's native-config path tries to
# apply FP8 to them anyway. **The smoke gate's vision probe answers it in one activation.**
#   * vision passes  → nothing to do.
#   * vision garbage → re-stage the HF layout (`sparky download <repo> --layout hf`) and
#     drop `--config-format`/`--load-format`, keeping the HF config that HAS the exclusion
#     list. That costs 124 GiB, which is why this note exists rather than a guess.
#
# MEMORY MATH (gmu 0.75 — big-shared with dev headroom):
#   weights 124.4 GiB total → ~62.2 GiB/node at TP=2
#   gmu 0.75 × 121 = 90.75 GiB/node budget − 62.2 weights − ~1 graphs → ~27.5 GiB KV/rank
#   KV/token = 2 × 88 layers × 8 kv_heads × 128 head_dim = 352 KiB (bf16); at TP=2 the 8 KV
#     heads split 4/4, so ~176 KiB per rank
#   → ~27.5 GiB KV holds ~164k tokens/rank. At max_model_len 131072 that is ~1.25 concurrent
#     full-length sequences — thin, and the first thing to tune. `gmu` has room to rise
#     (0.85 → ~40 GiB KV/rank) once the load is proven; raise gmu before cutting context.
#
# CONTEXT: trained max 262144 via YaRN from an original 4096. 131072 matches the NVFP4
# sibling's reasoning — half the extended ceiling, already generous, and it keeps a single
# sequence from consuming the whole KV budget on the most extrapolated part of the curve.
#
# NO `--quantization` FLAG. The checkpoint self-declares on both halves; overriding a
# self-declaring checkpoint double-quantizes into garbage, the standing footgun here.

# ONE NAME. profile == engine == served_as == the lowercased canonical HF model name,
# copied verbatim. NOT `-fp8`: Mistral ships FP8 as the BASE repo, so the quant is not in
# the upstream name and appending one would invent a name matching nothing on the Hub. The
# NVFP4 sibling is distinguishable because NVIDIA put NVFP4 in ITS repo name.
profile_name: mistral-medium-3.5-128b
hf_repo: mistralai/Mistral-Medium-3.5-128B
archetypes: [big-shared, vision]

# Staged and verified 2026-08-11, so the park it was written with is lifted: **21/21
# native-layout files present and size-exact against the Hub**, 124.5 GiB, no
# `.incomplete`, `consolidated.safetensors.index.json` present, and the HF `model-*` half
# correctly absent — `sparky download` took one layout and saved 124.4 GiB.
# (It was parked while the fetch was in flight because a `deploy` MOVES the inbox copy and
# mirrors it, so deploying mid-transfer would have replicated a truncated checkpoint.)

vllm_image: dgx-spark/vllm:26.07-xgrammar-fix  # 26.07 + xgrammar WAR (DEF-0010)

serving_topology:
  - name: mistral-medium-3.5-128b
    kind: vllm
    model: Mistral-Medium-3.5-128B  # dir under /opt/vllm/models
    served_as: mistral-medium-3.5-128b
    gpu_memory_utilization: "0.75"
    max_model_len: 131072
    head_extra_args:
      - --enable-chunked-prefill
      # THE TRIO, on both ranks. Two of three is what left DEF-0012 with a native config
      # and an HF-named weight loader (`KeyError: language_model.embed_tokens.weight`).
      # Every rank builds its own VllmConfig, so a head-only flag desynchronises the pair.
      - --tokenizer-mode mistral
      - --config-format mistral
      - --load-format mistral
      - --reasoning-parser mistral
      - --enable-auto-tool-choice
      - --tool-call-parser mistral
    worker_extra_args:
      - --enable-chunked-prefill
      - --tokenizer-mode mistral
      - --config-format mistral
      - --load-format mistral
```

### `mistral-medium-3.5-128b-nvfp4 (NVIDIA)`

```yaml
---
#    WHY: **dense**. GB10 decode is memory-bandwidth-bound — every token re-reads every
#    active weight — and a dense model's active set IS its whole weight set. Measured
#    2026-08-11 on the FP8 build: **3.44 tok/s single-stream, 84% of its theoretical
#    ceiling**. There is no flag, quant or container that fixes this; the NVFP4 sibling
#    only reaches ~5.7 tok/s by the same arithmetic. The replacement is
#    `mistral-small-4-119b-2603-nvfp4` — same vendor, 119B total but ~6B ACTIVE.
#
#    Verdict is owned by docs/models/tombstones.md; this file keeps the CONFIG and what
#    was learned building it, which was a great deal: the Mistral flag TRIO
#    (`--tokenizer-mode` + `--config-format` + `--load-format mistral`, all three or the
#    halves disagree), the dual-layout repo, and the DEF-0012 diagnosis. Re-verify
#    parsers, architecture and quant algo before reviving.
#    See docs/models/retired/README.md.
#
#    HISTORY. The fact sheet (docs/models/retired/mistral-medium-3.5.md) was deleted with this
#    retirement — it planned a model this cluster will not run. Read it at
#    `git show fd4c6d8:docs/models/retired/mistral-medium-3.5.md`. Verdict: tombstones.md.
#
#
# Mistral-Medium-3.5 (128B) — TP=2 across both nodes, container 26.07.
#
# WHY THIS MODEL: the first non-Chinese model in the fleet. Every incumbent (Step,
# MiniMax, Qwen) comes from one ecosystem, and a European option is worth carrying on
# diversity grounds alone — but it also happens to be the most COMFORTABLE big-shared
# fit we have: ~44.5 GiB/node of weights against MiniMax's ~70, so it buys the widest
# KV budget and the widest dev headroom of any TP=2 profile.
#
# QUANT — read this before touching the flags. The HF repo is named "...-NVFP4" but the
# checkpoint is **not** plain NVFP4: `quant_algo: MIXED_PRECISION`, modelopt 0.37.0,
# with 367 layers at FP8 and 249 at NVFP4 (per-layer map in `quantized_layers`). vLLM
# has a ModelOpt MIXED_PRECISION config class that reads exactly that map, so it is
# auto-detected — which means, as always, **NO `--quantization` flag** (it would
# double-quantize into garbage; the standing footgun on this cluster).
#
# FP8 KV CACHE IS DECLARED BY THE CHECKPOINT: `kv_cache_quant_algo: FP8`. That is
# DEF-0007's territory (FP8 KV + prefix caching → multi-turn corruption), never re-tested
# since vLLM 0.19. So `--enable-prefix-caching` is deliberately ABSENT here: if the two
# really do interact, the safe half to omit is the one we can add later for free.
# Watch the startup log for whether vLLM honours the declared FP8 KV or falls back to
# bf16 — the KV numbers below differ 2× between those cases, and it decides how much
# concurrency this profile actually has.
#
# MEMORY MATH (gmu 0.75 — "big-shared with dev headroom", same archetype as MiniMax):
#   weights 89 GiB total → ~44.5 GiB/node at TP=2
#   gmu 0.75 × 121 = 90.75 GiB vLLM budget/node − 44.5 weights − ~1 graphs → ~45 GiB KV/rank
#   KV/token = 2 × 88 layers × 8 kv_heads × 128 head_dim = 352 KiB (bf16) / 176 KiB (FP8);
#     at TP=2 the 8 KV heads split 4/4, so per rank that is 176 KiB (bf16) / 88 KiB (FP8)
#   → ~45 GiB KV holds ~268k tokens (bf16) or ~536k (FP8) per rank
#   At max_model_len 131072 that is ~2 (bf16) or ~4 (FP8) concurrent full-length sequences.
#   Outside headroom: 121 − 90.75 ≈ ~30 GiB/node for system + dev.
#
# CONTEXT: trained max is 262144, but via YaRN from an original 4096 (factor 64). 131072
# is half the extended ceiling and already generous; going to 262144 would spend the
# whole KV budget on a single sequence for the most extrapolated part of the rope curve.
#
# REASONING + TOOLS: the chat template uses Mistral's `[THINK]…[/THINK]` and
# `[TOOL_CALLS]` / `[AVAILABLE_TOOLS]` / `[TOOL_RESULTS]` conventions, so both parsers
# are `mistral` (vLLM's Ministral-3-Reasoning recipe). `text_config.model_type` is
# literally `ministral3`. No speculative decoding here, so DEF-0011 (MTP breaks
# constrained decoding) does not apply — all four tool_choice shapes should work.
#
# TOKENIZER — `--tokenizer-mode mistral` IS REQUIRED, and this cost a bring-up to learn.
# The directory carries BOTH HF (`tokenizer.json`, `config.json`,
# `model.safetensors.index.json`) and Mistral-native (`params.json`, `tekken.json`)
# artifacts, so the HF path looked available. It is not: vLLM validates the tokenizer
# TYPE for this architecture and refuses at config time —
#   ValidationError: Value error, The tokenizer must be an instance of MistralTokenizer
# The presence of `tokenizer.json` says the file is there, not that vLLM will accept it.
# `--config-format`/`--load-format` are NOT needed: config parsing got as far as
# quantization detection before the tokenizer check fired, so only the tokenizer is
# Mistral-native in a way that matters.
#
# PROBED BEFORE ACTIVATION (ADR-0019): 26.07 reports `Mistral3ForConditionalGeneration`
# supported and lists `modelopt_mixed` among its quantization methods, and the first
# bring-up confirmed both at runtime — it detected the FP8, NVFP4 and W4A16_NVFP4 layer
# groups correctly before failing on the tokenizer alone. See
# docs/models/retired/mistral-medium-3.5.md.

# ONE NAME. profile == engine == served_as == the lowercased canonical HF model
# name, so the scoreboard, the systemd unit, the API and huggingface.co all agree.
# `hf_repo` is the exact upstream id for eyeball-matching against the Hub.
profile_name: mistral-medium-3.5-128b-nvfp4
hf_repo: nvidia/Mistral-Medium-3.5-128B-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [big-shared, mixed-precision, vision]
# ⚠️ ON TRIAL — DEF-0012. Unparked 2026-08-11 to run the register's next candidate
#    ATTENDED. The engine crash-looped at startup and never served, text included: the
#    checkpoint ships BOTH HF and Mistral-native artifacts and the two disagree, so HF's
#    PixtralProcessor sees one image in the prompt text and zero in the input ids.
#
#    THE CANDIDATE: `--config-format mistral` on BOTH ranks, so the CONFIG half takes the
#    native path the TOKENIZER half was already forced onto. vLLM's own Mistral recipe
#    prescribes the two together; the hybrid state is ours, not upstream's. It replaces
#    the `--limit-mm-per-prompt {"image":0}` WAR, which DEF-0012 records as NOT working —
#    vLLM runs multimodal profiling regardless of the declared limit.
#
#    TRIAL RESULT 2026-08-11 — HALF RIGHT, AND THE FIX IS ONE MORE FLAG.
#    The multimodal symptom is GONE: no more `Mismatch in 'image' token count`. Config and
#    tokenizer now agree. It then died further in, at weight load:
#      quantization=fp8, quantization_config=None          <- the MIXED_PRECISION map, lost
#      KeyError: 'language_model.embed_tokens.weight'      <- llama.py:479, load_weights
#    Both halves are ONE cause: `load_format` stayed `auto` while `config_format` went
#    native. vLLM built a Mistral-native config, then asked an HF-named weight loader to
#    satisfy it. **The mixed state was never resolved — it moved down one layer.**
#
#    SO THE FLAGS TRAVEL AS A TRIO, and we were running two of three. NVIDIA's own DGX
#    Spark forum shows the working GB10 recipe for a sibling model using all three
#    together: `--tokenizer-mode mistral --config-format mistral --load-format mistral`.
#    `--load-format mistral` is added below; this is the next attended trial.
#
#    ⚠️ RETRACTION — this checkpoint is probably NOT "mis-assembled". An earlier note here
#    said so and told you to reject it. That was wrong, and the evidence is two-fold:
#    (1) dual HF-plus-native artifacts is the **Mistral publishing convention** — every
#    Mistral-3 repo checked ships both `config.json` and `params.json`/`tekken.json`;
#    (2) `zdy1995love/Mistral-Medium-3.5-128B-NVFP4`, structurally identical to this repo,
#    **serves on a DGX Spark** (forum performance report, vLLM 0.20.2rc1, no tokenizer or
#    config-format errors). The fault is in our flags and/or 0.24.0, not in the bytes.
#
#    RESIDUAL RISK for the next trial: this repo has `model.safetensors.index.json` but no
#    `consolidated.safetensors.index.json`, so `--load-format mistral` may not find an
#    index it recognises. If it fails THAT way, the honest reading is "this repo cannot
#    take the fully-native path" — and the fallback is the reverse experiment: drop
#    `--config-format`/`--load-format` entirely and keep the HF config (which DOES carry
#    the MIXED_PRECISION map), leaving only the processor to solve.
#    See scouting-reports/2026-08-mistral-sourcing.md for the field of alternatives.
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix  # 26.07 + xgrammar WAR (DEF-0010)
serving_topology:
  - name: mistral-medium-3.5-128b-nvfp4
    kind: vllm
    model: Mistral-Medium-3.5-128B-NVFP4
    served_as: mistral-medium-3.5-128b-nvfp4
    gpu_memory_utilization: "0.75"
    max_model_len: 131072
    head_extra_args:
      - --enable-chunked-prefill
      - --tokenizer-mode mistral
      # DEF-0012 candidate — the config half of the native path. Belongs on BOTH ranks
      # for the same reason `--tokenizer-mode` does: every rank builds its own
      # VllmConfig, so a head-only flag makes the worker disagree with the head.
      - --config-format mistral
      # The THIRD of the trio, added 2026-08-11 after the trial died with
      # `KeyError: 'language_model.embed_tokens.weight'` — a native config asking an
      # HF-named loader for native weight names. Without this, `load_format` stays `auto`
      # and the config/loader halves disagree exactly as config/tokenizer used to.
      - --load-format mistral
      - --reasoning-parser mistral
      - --enable-auto-tool-choice
      - --tool-call-parser mistral
    worker_extra_args:
      - --enable-chunked-prefill
      - --tokenizer-mode mistral
      - --config-format mistral
      - --load-format mistral
```
