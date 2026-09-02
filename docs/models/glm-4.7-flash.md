# GLM-4.7-Flash on 2× DGX Spark — Fact Sheet

**Last updated:** 2026-08-30
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each (121 GiB usable), ConnectX-7 200 Gbit RoCE
**Installed quant:** **bf16 — live** (profile `glm-4.7-flash`, TP=2)
**Target quant:** NVFP4 (community `GadflyII`) — the A/B follow-up, not yet done

> Onboarded 2026-08-30 (ADR-0029). Promoted from a candidate the day it served: it needed **no
> container work** — the running `26.07-xgrammar-fix` image already had everything.

## Model Overview

- **Developer:** Z.ai / Zhipu (`zai-org`), MIT license.
- **Architecture:** `Glm4MoeLiteForCausalLM` (`glm4_moe_lite`) — MoE, 47 layers, hidden 2048,
  **64 routed experts, 4 active + 1 shared**, `moe_intermediate_size` 1536.
- **Params:** **~30B total / ~A3B active** — the fast large-total/small-active shape, same class as
  `qwen3.6-35b-a3b`. **Text-only** (vision is sourced on its own track, ADR-0029).
- **Context:** **202,752 native** (config `max_position_embeddings`, no YaRN); "1M" would need a
  `rope_scaling` block we have not added.
- **Features:** tool calling (`glm47`) + reasoning (`glm45`), both verified live.
- **HF:** [`zai-org/GLM-4.7-Flash`](https://huggingface.co/zai-org/GLM-4.7-Flash).

## Quantization Formats & Footprint

| Format | Source | Disk | Per node at TP=2 | Status |
|---|---|---|---|---|
| **BF16** | `zai-org/GLM-4.7-Flash` (official) | 63.7 GB | **~29.7 GiB** | **live** — safe first bring-up |
| NVFP4 | `GadflyII/GLM-4.7-Flash-NVFP4` (community) | 20.5 GB | ~9.5 GiB | staged, **not yet A/B'd** — uncalibrated |
| MXFP4 | `GadflyII/GLM-4.7-Flash-MXFP4` (community) | 20.9 GB | ~9.7 GiB | less proven on sm_121 |
| FP8 | — (not published) | — | — | none exists |

## Serving — as deployed

- **Container:** `dgx-spark/vllm:26.07-xgrammar-fix` (the fleet default) — vLLM 0.24.0 registers the
  arch, transformers **5.6.1** recognises the config, `glm47`/`glm45` parsers ship in 0.24.0. No
  derived image was needed; the fallback tracker (`docs/upgrades/container-glm-4.7-flash-tf5.md`)
  went unused.
- **`memory_fraction: 0.70`** (ADR-0028) — held ~18–24 GiB host free under load.
- **`context_length: 202,752`** (native). KV at 0.70 holds **~1,016,000 tokens (51 GiB)** → ~5×
  concurrency at the full window.
- **Flags:** `--enable-chunked-prefill --enable-auto-tool-choice --tool-call-parser glm47
  --reasoning-parser glm45`.

## Onboarding record (2026-08-30)

First activation attended and clean. Smoke: **ready · tool-shape 200 · quality pass · vision n/a**.
Functional: reasoning correct (trains→7 PM, sheep→9), tool call parsed (`get_weather({"location":"Paris"})`),
decode ~39 tok/s at concurrency 1 (bf16). No container work — the ADR-0029 "off-the-shelf isn't a
gate" bet paid off immediately.

## What to Watch For

- **Heavy reasoner.** It emits long reasoning into the `reasoning` field (not `reasoning_content` —
  vLLM 0.24.0 naming) and can spend a whole tight budget thinking before it answers: an 800-token
  request returned empty content; 3000 answered correctly. **Give requests a generous `max_tokens`
  (~4k–8k).** This is a client/Open WebUI default, not a profile flag (model-agnostic client).
- **The NVFP4 is community, uncalibrated.** A/B it against bf16 (smoke + evals) before trusting it;
  only then consider it for the live slot. Batch `--kv-cache-dtype fp8` with that switch if wanted.
- **Longer context** (YaRN to ~1M) is a deliberate `rope_scaling` step, memory-bounded by KV.

## Key Links

- Profile: `ansible/profiles/glm-4.7-flash.yml` · Container tracker (fallback, unused): [`../upgrades/container-glm-4.7-flash-tf5.md`](../upgrades/container-glm-4.7-flash-tf5.md)
- [ADR-0029](../adr/0029-model-sourcing-strategy.md) — the per-capability / build-our-own strategy this onboarding proved
- Sweep it came from: [`../../scouting-reports/2026-08-17-qwen38-granite-glm.md`](../../scouting-reports/2026-08-17-qwen38-granite-glm.md)
- [GB10 forum guide](https://forums.developer.nvidia.com/t/glm-4-7-flash-on-pgx-dgx-vllm-guide/358874) · [vLLM #34098](https://github.com/vllm-project/vllm/issues/34098)
