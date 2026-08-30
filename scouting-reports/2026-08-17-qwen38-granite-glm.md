# Scouting report — 2026-08-17 · Qwen3.8, IBM Granite 4.2, GLM (Flash + frontier)

**Trigger.** "Is Qwen3.8 out, can we update?" — then "investigate the new IBM Granite models
and new GLM (maybe Flash?)." A three-family sweep of the fast-generalist / general track.

**Method.** `hf` CLI (author listings + `config.json` for active-param truth + `--tree -h`
sizing), the decode-arithmetic gate ([`model-discovery`](../skills/model-discovery/SKILL.md)),
the NVIDIA DGX-Spark forums for GB10 corroboration. Registers (tombstones, candidates) were
clean for all three on entry.

**Headline.** Two of the three "new small models" are **dense**, and dense is the write-off on
this bandwidth-bound hardware. The one real find is **GLM-4.7-Flash** — a 30B-A3B MoE, the right
shape — now written up as a candidate. This sweep also produced a strategy shift
([ADR-0029](../docs/adr/0029-model-sourcing-strategy.md)): rank per capability, and treat
building our own container as a gate we can clear rather than a wall.

## Qwen3.8 — out, wrong shape for the fast slot

The Qwen3.8 line is only two shapes: `Qwen/Qwen3.8-27B` (arch `qwen3_5` = **dense**, vision) and
`Qwen/Qwen3.8-2.4T-A95B` (frontier MoE — 2.4T total, out of the ~215 GiB budget). **There is no
Qwen3.8 in our model's shape** — Qwen replaced the small slot with a dense 27B, not a small-active
MoE, and **no nvidia-calibrated NVFP4 exists** (only community GGUF).

Dense 27B reads all 27B/token → decode ceiling ~20 tok/s (FP8) / ~40 (NVFP4) → **~10–34 measured**
vs the incumbent `qwen3.6-35b-a3b`'s **100.2**. A 3–5× regression on the axis the slot exists for.

**Not adopted; not a tombstone.** We want the *line*, not this shape. **Revisit when:** Qwen ships
a 3.8-generation **small-active MoE** (`Qwen3.8-…-A…B`), or nvidia publishes a **3.8 NVFP4**.

> **UPDATE 2026-08-30 — the re-trigger fired.** `Qwen/Qwen3.8-Flash-Next` landed: a **125B-A6B vision
> MoE** (arch `qwen4_exp`), exactly the small-active shape we said to watch for. Fits at NVFP4
> (~62 GiB/node) and runs on 2× DGX Spark — but via **SGLang**, not vLLM (GB10 arm64 doesn't register
> the arch). Full write-up + the SGLang-vs-vLLM decision:
> [`docs/models/candidates/qwen3.8-flash-next.md`](../docs/models/candidates/qwen3.8-flash-next.md).

## IBM Granite 4.2 — dense line, text-only

`granite-4.2-30b` is `GraniteForCausalLM`, **dense** (no expert fields; `intermediate_size 32768`).
The 4.2 flagship line (3b/8b/**30b**) is all dense — no large-total small-active MoE in it. IBM
ships its own NVFP4 (`granite-4.2-30b-nvfp4`, ~16.4 GiB), which is nice, but it does not fix
dense-is-slow: ceiling ~36 → **~18–31 tok/s**, and at 16 GiB it *under*-fills a 215 GiB box while
being **text-only**.

The only draw is **US/IBM vendor diversity** (the suite is all-Chinese) — but diversity is a
tiebreaker for models *close* on speed, and this isn't close. **Not adopted. Revisit when:** a
Granite **MoE** (the `granitemoehybrid` line) lands in our size band.

## GLM — frontier out, "Flash" is the find

- **Frontier (GLM-5.x) is out.** `nvidia/GLM-5.2-NVFP4` is **433 GiB** — nearly 2× the ~215 GiB
  two-node ceiling, and `glm_moe_dsa` with large active is slow regardless. Confirms the earlier
  standing note; don't re-check.
- **GLM-4.7-Flash → [candidate sheet](../docs/models/glm-4.7-flash.md).** `Glm4MoeLite`,
  **30B-A3B**, the same fast shape as the incumbent. bf16 fits at ~30 GiB/node (~45–77 tok/s),
  community NVFP4 at ~9.5 GiB/node (incumbent-class speed). vLLM supports the arch; a **GB10 forum
  guide has served it**. The catch — needs **transformers ≥5.0**, i.e. a derived image — is exactly
  the kind of blocker ADR-0029 now treats as buildable-by-us rather than disqualifying. **We are
  grabbing it.**

## Method notes (fold-back for `model-discovery`)

- **`config.json` is the shape oracle.** `curl …/raw/main/config.json` → presence/absence of
  `n_routed_experts` / `num_experts_per_tok` settles dense-vs-MoE in one look, faster than reading
  a card. Two "new 27–30B models" this sweep were dense, invisible from the name alone.
- **The `nvidia/` modelopt sweep is the calibrated-NVFP4 filter.** Its *absence* for a model
  (Qwen3.8, GLM-4.7-Flash) is a real signal: the proven FP4 path isn't published, so it's bf16 /
  community-quant / self-quant — a cost to price in, not a detail.
