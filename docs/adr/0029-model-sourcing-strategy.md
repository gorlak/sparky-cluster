# ADR-0029: Model sourcing strategy — rank per capability, and build our own containers

**Date:** 2026-08-17
**Status:** Accepted

## Context

Two assumptions were baked into how this cluster sources models, and a single sweep
(Qwen3.8 / Granite-4.2 / GLM-4.7-Flash, 2026-08-17) surfaced both as too narrow.

1. **One flagship ranking.** [`model-discovery`](../../skills/model-discovery/SKILL.md) ranked
   candidates against a single question — *the smartest model that fills both nodes, decodes
   fast, and does vision too.* But the fleet has never actually been one model: it holds
   `qwen3-vl-235b` for vision, `qwen3-coder-next` for code, `qwen3.6-35b-a3b` for fast general
   work. Folding "vision too" into the one flagship penalised a strong text model for lacking a
   capability we source *separately* — GLM-4.7-Flash (30B-A3B, an ideal fast MoE) scored down
   for being text-only, against a slot where vision was never the axis.

2. **Off-the-shelf containers as a gate.** The skills said *"prioritise models that work with
   the stock NVIDIA image — no custom forks, no special patches,"* which reads a required
   image change as disqualifying. Yet we already run a **derived** image
   (`dgx-spark/vllm:26.07-xgrammar-fix`, DEF-0010) because NVIDIA shipped xgrammar below vLLM's
   own minimum. Building the container is not the exception; it is a capability we already have.
   GLM-4.7-Flash needs transformers ≥5.0 for its `glm4_moe_lite` arch — under the old rule a
   reject, under reality a Tuesday.

## Decision

### 1. Rank models per capability, not one flagship

Sourcing asks the ranking question **once per capability track**, not once overall:

- **General / reasoning** — the smartest fast MoE that fills the box. The default track.
- **Multimodal / vision** — ranked on its own. Vision is a **nice-to-have** for the other
  tracks, never a requirement, *because it has its own ranking.*
- **Coding** — ranked on its own, on coding-specific evidence (ADR-0024's harness), not folded
  into general intelligence.

The hardware constraint is unchanged: **one engine serves at a time** (one front port
fleet-wide, ADR-0018), so the fleet is a *set* of best-in-track profiles and `activate` picks
which track is live. What changes is the sourcing lens — "best general", "best vision", "best
coder" are three questions with three answers, and a candidate is judged against the track it
is *for*. The Tier-1 "fill both nodes with a large-total, small-active MoE" arithmetic
(`model-discovery`) still governs *within* a track; it is the ranking dimension that splits.

### 2. Off-the-shelf containers are a convenience, not a gate

A model that needs an image we don't run today is **not** disqualified. The order of preference
is unchanged — a stock `nvcr.io/nvidia/vllm` tag is the lowest-effort path and still the
default — but "stock only" stops being a gate. The real gate is narrower and more honest:

> **Can we build the container elements the model needs, ourselves?** A transformers bump, a
> parser patch, a reasoning/tool grammar, a kernel — if the missing piece is something we can
> add to a derived image, the model is in range.

Two conditions keep this from becoming a time sink:

- **Front-load the investigation.** Before building, establish *what* is missing and *some*
  confidence it will work — a config that names the arch, a vLLM model file that supports it, a
  GB10 community report that ran it. Confidence, not certainty; the build is how certainty is
  earned.
- **The build is worth the tokens.** Owning our containers puts the cluster nearer the bleeding
  edge — newer-model support arrives when we build it, not when NVIDIA reships — and the
  expertise compounds. That is a feature of this project, not overhead to minimise.

## Consequences

- **A text-only model is no longer penalised in the general/coding tracks.** GLM-4.7-Flash is
  now a live candidate (`docs/models/glm-4.7-flash.md`) judged on general/coding
  merit, with vision sourced elsewhere.
- **"Needs a newer transformers / a patch" becomes a `Clears when`, not a rejection.** Such
  models are candidates gated on a build we choose to do, not tombstones.
- **The derived-image path is first-class.** `dgx-spark/vllm:26.07-xgrammar-fix` stops being an
  apologised-for one-off and becomes the template: a container upgrade tracker
  (`docs/upgrades/`) captures each build, its WAR register carries what we added and why, and
  the removal condition is "stock caught up."
- **Discipline still applies.** Per-capability ranking is not "run everything" — each track
  holds *one* best answer, and the fleet is still small. Build-our-own is not "build blindly" —
  it is gated on front-loaded confidence and a decode-arithmetic pass that a bad fit fails
  regardless of container.

## Alternatives considered

**Keep the single flagship ranking.** It never matched the fleet, which was already
multi-track; the "vision too" clause just mis-scored good text models. Formalising what we do
beats a rule we route around.

**Keep "stock containers only."** Safer, and genuinely lower-effort — but it caps the cluster
at whatever NVIDIA last shipped, on hardware bought to run the frontier. We already broke the
rule for xgrammar; the honest move is to make the exception the policy, with a real gate
(buildable-by-us) in place of a blanket ban.

## References

- [ADR-0018](0018-provision-select-split.md) — one engine live at a time; the fleet is a set, activation picks one
- [ADR-0024](0024-coding-measurement.md) — the coding-specific measurement the coding track ranks on
- [`skills/model-discovery`](../../skills/model-discovery/SKILL.md) — the priority tiers, now per-capability, and the container-policy line
- [`docs/models/glm-4.7-flash.md`](../models/glm-4.7-flash.md) — the candidate this shift unblocks
- `dgx-spark/vllm:26.07-xgrammar-fix` (DEF-0010) — the derived image this makes the template rather than the exception
