# Container upgrade tracker — a derived image to serve GLM-4.7-Flash (`26.07-glm-tf5`)

> **Living, gated tracker — not an ADR.** It tracks building a derived image so the cluster can
> serve `glm4_moe_lite`. The *decision* to build our own containers is [ADR-0029](../adr/0029-model-sourcing-strategy.md);
> the *model* is [`glm-4.7-flash`](../models/glm-4.7-flash.md).

**Status:** ✅ **CONFIRMED not needed — GLM-4.7-Flash is live on the existing image** (onboarded
2026-08-30, `docs/models/glm-4.7-flash.md`). This tracker is retained only as the recipe if a
future GLM variant ever outruns the running transformers.
**Current:** `dgx-spark/vllm:26.07-xgrammar-fix` — vLLM 0.24.0, **and it already runs transformers 5**
(vLLM 0.24.0 removed transformers v4, so the image's own build assert pins `transformers` to `5.x`).
**Target (fallback only):** `dgx-spark/vllm:26.07-glm-tf5` — 26.07 + a **newer** transformers 5.x + the
xgrammar patch, pinned to the GLM profile only. Built **only if** the running image's transformers 5.x
turns out too old to recognise the `glm4_moe_lite` config.
**Last updated:** 2026-08-30

## Why

GLM-4.7-Flash is `Glm4MoeLiteForCausalLM` (`glm4_moe_lite`), and that architecture **requires
transformers ≥5.0** ([vLLM #34098](https://github.com/vllm-project/vllm/issues/34098)) — not in
any stock `nvcr.io/nvidia/vllm` tag we run. Under ADR-0029 this is a buildable-by-us element, not a
reject: the model is a 30B-A3B MoE (incumbent-class speed) and a [GB10 forum user has served
it](https://forums.developer.nvidia.com/t/glm-4-7-flash-on-pgx-dgx-vllm-guide/358874) via a custom
image. This is the first derived image built for **capability** rather than a bug fix (xgrammar
was a fix); it is the template ADR-0029 anticipates.

## What changes

| | current 26.07 | target `26.07-glm-tf5` |
|---|---|---|
| vLLM | 0.24.0 | 0.24.0 (unchanged **if** it registers the arch — see open items) |
| transformers | 4.x (stock) | **≥5.0** |
| xgrammar patch (DEF-0010) | present | carried forward (GLM tool-calls via `glm47`) |
| scope | every profile | **GLM-4.7-Flash profile only** |

## Implications for the cluster

- **Isolated by design.** Only the GLM profile's `vllm_image` points here; a transformers-5.0
  regression cannot reach `qwen3.6`, `minimax`, etc. This is exactly the per-profile-image safety
  the fleet already relies on (README).
- **Memory is a non-issue.** bf16 is ~30 GiB/node at TP=2, ~90 GiB headroom — well clear of the
  ADR-0028 floor. The container change is about *loading* the arch, not fitting it.

## Dependencies

1. **Weights staged ✅** — `GadflyII/GLM-4.7-Flash-NVFP4` (20 GB, 4 shards) + `zai-org/GLM-4.7-Flash`
   (bf16, 59 GB, 48 shards), both in `/opt/cluster/model-cache/` with valid `config.json`
   (2026-08-17). Next `deploy` mirrors them to `/opt/vllm/models` on both nodes.
2. **vLLM arch support — UNRESOLVED, and it is the pivotal question.**
   [PR #31386](https://github.com/vllm-project/vllm/pull/31386) added `Glm4MoeLiteForCausalLM`
   (merged 2026-01-19). The timeline says 0.24.0 (26.07, July) should include it — but the
   v0.24.0 changelog names only the GLM-4.7 *parser*, not the *arch* PR. Release notes don't
   enumerate every model, so this is weak evidence, not a verdict. **Settle it with the probe
   (open item A) before building** — it decides transformers-only vs transformers+vLLM.

## Workarounds (WARs) register

| WAR | fixes | upstream | cost | applied in | remove when | status |
|---|---|---|---|---|---|---|
| transformers ≥5.0 | `glm4_moe_lite` unrecognised | [#34098](https://github.com/vllm-project/vllm/issues/34098) | one derived-image layer; possible vLLM-wheel bump | `26.07-glm-tf5` | a stock `nvcr` tag ships transformers ≥5.0 with 0.24.0+ | 🔵 planned |
| xgrammar pin | tool-calling below vLLM's min | DEF-0010 | carried from `26.07-xgrammar-fix` | `26.07-glm-tf5` | NVIDIA ships xgrammar ≥ vLLM's floor | ✅ known-good |

## Open items

- **A. Does 0.24.0 register the arch? — ✅ RESOLVED (2026-08-27).** Probed the deployed image:
  `Glm4MoeLiteForCausalLM in ModelRegistry.get_supported_archs()` → **true**.
- **B. Does the image tolerate transformers 5? — ✅ RESOLVED.** It doesn't merely tolerate it, it
  *requires* it: vLLM 0.24.0 removed transformers v4, and the image's own build assert
  (`vllm-26.07-xgrammar-fix/Dockerfile`) pins `transformers` to `5.x`. So both pieces the arch needs
  are already in the running image.
- **C. Is the image's transformers 5.x new enough for the `glm4_moe_lite` *config*? — the one
  residual check.** vLLM's registry (A) and transformers' config map are separate; the arch landed in
  transformers 5.0.0 (vLLM #34098), and the image has *some* 5.x. Confirm before the first activation:

  ```bash
  # transformers version in the running image + whether it maps the glm4_moe_lite config
  sudo docker run --rm dgx-spark/vllm:26.07-xgrammar-fix python3 -c "from importlib.metadata import version; print('transformers', version('transformers')); from transformers.models.auto.configuration_auto import CONFIG_MAPPING_NAMES; print('glm4_moe_lite recognised:', 'glm4_moe_lite' in CONFIG_MAPPING_NAMES)"
  ```

  **`recognised: True`** → no build at all; unblock the profile, deploy, activate. **`False`** → build
  the fallback image above (bump transformers 5.x; the [`eugr/spark-vllm-docker`](https://github.com/eugr/spark-vllm-docker)
  `--pre-tf` recipe is the proven reference), keeping vLLM 0.24.0 as-is since the arch is already there.

## Completion criteria

1. `26.07-glm-tf5` builds on **both** nodes (the `images` role, ADR-0013).
2. GLM-4.7-Flash **loads** under it (bf16 first) and `sparky smoke` passes — readiness, the
   tool-call shape with `--tool-call-parser glm47 --reasoning-parser glm45`, and text-sanity.
3. A soak runs clean with the ADR-0028 memory harvest recording the high-water mark.

## Retry / deploy plan

Build behind the fail-safe, one model, attended: bf16 → smoke → soak → evals/coding, then A/B the
community NVFP4 against bf16 before trusting the uncalibrated quant. TP=2 native (no Ray) — the
forum's single-node/Ray config and its `RAY_memory_usage_threshold` workaround are **not** ours.

## Re-assessment log

- **2026-08-17** — created from the 2026-08-17 sweep. Open items A/B identified as the go/no-go
  for a transformers-only vs transformers+vLLM build.
- **2026-08-17** — both quants **staged** (NVFP4 20 GB, bf16 59 GB). Web check on item A was
  inconclusive: v0.24.0 changelog lists the GLM-4.7 parser but not the `glm4_moe_lite` arch PR —
  suggestive but not decisive (notes aren't exhaustive). The `ModelRegistry` probe is the only
  reliable check, and it is privileged (`sudo docker`), so it waits for a human at the console.
- **2026-08-27 — the premise mostly dissolved.** Probe returned **true** (item A). And reading the
  `xgrammar-fix` Dockerfile showed the image *already runs transformers 5* (item B) — vLLM 0.24.0
  removed v4, so it had to. With the `glm47`/`glm45` parsers also in 0.24.0, **all three requirements
  are already in the running image**, so a new container is probably unnecessary. Status → 🟢; this
  tracker is now the fallback if residual check C fails. The GLM profile (`glm-4.7-flash`, bf16,
  `blocked: true`) was written pointing at the *existing* image on that basis.
- **2026-08-30 — CLOSED, no build.** Residual check C returned `transformers 5.6.1, glm4_moe_lite
  recognised: True`. GLM-4.7-Flash activated cleanly on the existing image — smoke passed, tool-calling
  and reasoning verified, memory safe at gmu 0.70. The fallback image was never built. Model promoted
  to a live fact sheet (`docs/models/glm-4.7-flash.md`); this tracker is now a recipe on the shelf.

## References

- [`docs/models/glm-4.7-flash.md`](../models/glm-4.7-flash.md) — fit, speed, parser flags
- [ADR-0029](../adr/0029-model-sourcing-strategy.md) — build-our-own containers is policy
- [ADR-0013](../adr/0013-container-image-sourcing.md) — the `images` role builds/pulls per node
- [GB10 forum guide](https://forums.developer.nvidia.com/t/glm-4-7-flash-on-pgx-dgx-vllm-guide/358874) · [vLLM #34098](https://github.com/vllm-project/vllm/issues/34098) · [PR #31386](https://github.com/vllm-project/vllm/pull/31386)
