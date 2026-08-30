# Qwen3.8-Flash-Next on 2× DGX Spark — Candidate Fact Sheet

**Last updated:** 2026-08-30
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each (121 GiB usable), ConnectX-7 200 Gbit RoCE
**Installed quant:** none — weights not held
**Target quant:** NVFP4 (community), ~62 GiB/node at TP2 — or FP8 (official), ~87 GiB/node

> **Blocked on:** **no vLLM path on GB10.** vLLM has *day-0* support on x86/AMD, but on **SM121
> (GB10) the arm64 image does not register `Qwen4ExpForConditionalGeneration`**, and even patched,
> the QSA sparse-decode resolver falls back to an FA4 CUTE path that **fails in warmup**. The only
> proven DGX-Spark path today is **SGLang**.
> **Clears when — either of:** (a) a vLLM image we can run registers `qwen4_exp` **and** the SM121
> QSA path works (upstream — plausible, since x86 is day-0 — or a derived-image patch of ours); or
> (b) we deliberately adopt **SGLang as a second engine kind** (an ADR-scale decision, below).

**Track:** general **and** multimodal at once (ADR-0029) — a 125B-A6B *vision* MoE. If served, it is
a candidate to supersede **both** `qwen3.6-35b-a3b` (general) **and** `qwen3-vl-235b` (vision), and
it would narrow `glm-4.7-flash` to the coding track. That breadth is why it is worth the paperwork.

## Model Overview

- **Developer:** Qwen Team, Alibaba (`Qwen/Qwen3.8-Flash-Next`), license: other (Qwen).
- **Architecture:** `Qwen4ExpForConditionalGeneration` (`qwen4_exp`) — hybrid **Gated DeltaNet + Qwen
  Sparse Attention (QSA)**, 48 layers, **512 experts (10 routed + 1 shared)**, `moe_intermediate_size`
  640, `hidden_size` 2560, `num_key_value_heads` 2, `full_attention_interval` 4.
- **Params:** **125B total / A6B active** — the ideal large-total/small-active shape, *and* multimodal.
  Two extras attached: a **51B PLE / n-gram embedding table** (separate, **offloadable to host**), and
  a built-in **4B MTP head** (multi-token prediction → speculative decode).
- **Context:** **262,144 native, up to ~1M via YaRN.**
- **Multimodal:** image, video, text (`image-text-to-text`).
- **Sampling:** thinking `temp 1.0 / top_p 0.95 / top_k 20`; instruct `temp 0.7 / top_p 0.80 / top_k 20`.
- **HF:** [`Qwen/Qwen3.8-Flash-Next`](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) · [`-FP8`](https://huggingface.co/Qwen/Qwen3.8-Flash-Next-FP8).

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Fit |
|---|---|---|---|---|
| BF16 | `Qwen/Qwen3.8-Flash-Next` | 360.9 GB | ~168 GiB | ❌ Overflows 121 GiB/node |
| **FP8** | **`Qwen/…-FP8`** (official) | **186.5 GB** | **~87 GiB** | ✅ Fits, fully-committed, best quality |
| **NVFP4** | **`lovedheart/…-NVFP4-FP8`** (community) | **123.5 GiB** | **~62 GiB** | ✅ Fits with room |
| uint3 | `HamboneLabs-AI/…-uint3-g64` (community, **GB10-tagged**) | 51.5 GiB | ~26 GiB | ✅ Sub-4-bit, huge room |

**Note the 51B n-gram table offloads.** With `--ple-offload-embedding` it sits in **host RAM (~13
GB/rank)**, not VRAM — "a row lookup, not a matmul," so decode is unaffected. That is what makes the
above per-node numbers comfortable rather than tight.

## Speed — the arithmetic gate ✅

A6B active → fast, and MTP roughly triples it. **Measured on our exact hardware** (tonyd2wild, 2×
DGX Spark, TP2, NVFP4, SGLang): **peak ~69.7 tok/s (code), ~50 typical, ~20 without MTP**, TTFT
~0.2 s warmed, at 262K context. Comfortably conversational and in the fast-flagship class.

## TP=2 divisibility ✅

512 experts, 2 KV heads, 48 layers — even across the board; the standard TP=2 split holds.

## GB10 corroboration & the serving blocker

- **vLLM:** [day-0 on x86/AMD](https://x.com/vllm_project/status/2092600887873286157), but **not GB10**:
  the arm64 image doesn't register the arch, and QSA fails on SM121 in warmup
  ([support issue](https://github.com/jundot/omlx/issues/3170), [vLLM recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next)).
- **SGLang — proven on 2× DGX Spark TP2:**
  [**tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark**](https://github.com/tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark).
  Fix is a **one-line Python guard** (extend `is_sm100_supported()` to accept `is_sm120_supported()`
  after a head-shape probe); patched image `radixark/sglang-qwen38flashnext:sm121-qsa`. Worker-first
  TP2 launch, `--ple-offload-embedding`, KV pool pinned to 600K, `--max-mamba-cache-size 97`, temp ≤0.7
  (a rare multimodal-rope assert under cuda-graphs at temp 1.0), and a mandatory `drop_caches` pre-launch.

## The decision this sheet exists to preserve — SGLang vs vLLM

This is **not** a normal candidate: the blocker is the *serving engine*, not a quant or a kernel.

- **SGLang now** — works today on our hardware. But it is a **second serving engine** in a vLLM-only
  cluster. Softener: our profile schema already carries `kind: vllm`, so `kind: sglang` is the
  anticipated seam — still real work (a `sglang@.service` template, its image, reconciler handling,
  health/smoke against its endpoint), but not a rewrite. **Adopting it is an ADR-scale decision.**
- **vLLM later** — stays on our stack. The arm64 arch-registration gap is bigger than SGLang's
  one-line guard, so this likely means waiting for upstream (day-0 on x86 makes it plausible) or a
  heavier derived-image/source patch. Cheaper if it lands; uncertain when.

**Recommendation when we return:** first check whether vLLM's arm64 `qwen4_exp` registration has
landed or is patchable-by-us (ADR-0029) — that keeps this on our stack. Only if we want the model
badly enough *and* the vLLM path stalls does SGLang earn its ADR.

## Lifecycle

Weights are **not staged** (deliberately — nothing serves it here yet). On promotion — when a serving
path is real and we stage weights — this moves to `docs/models/qwen3.8-flash-next.md` and the serving
blocker becomes a `DEF-NNNN` row. Until then it lives here so a sweep does not re-derive it.

## References

- [`docs/adr/0029-model-sourcing-strategy.md`](../../adr/0029-model-sourcing-strategy.md) — per-capability ranking; build-our-own containers (and, by extension, the SGLang question)
- [`scouting-reports/2026-08-17-qwen38-granite-glm.md`](../../../scouting-reports/2026-08-17-qwen38-granite-glm.md) — the sweep whose "revisit when Qwen ships a 3.8 small-active MoE" re-trigger this fired
- [tonyd2wild DGX-Spark SGLang deployment](https://github.com/tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark) · [vLLM day-0](https://x.com/vllm_project/status/2092600887873286157) · [arm64 support issue](https://github.com/jundot/omlx/issues/3170)
