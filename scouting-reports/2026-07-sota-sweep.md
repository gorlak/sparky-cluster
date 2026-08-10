# SOTA sourcing sweep — 2× DGX Spark (GB10) — 2026-07-26

**Assessment only.** No profile changed, nothing deployed. A fleet-wide review of whether
the models we serve are being superseded, run *before* investing in ADR-0014 optimizations
so we don't tune models on their way out. Method: [[model-discovery]] + [[model-evaluation]]
(HF `hf` CLI sizing + NVIDIA DGX Spark forum / vLLM-issue cross-referencing). Fit window:
2-node TP=2 tops ~215 GiB total weights; single-node NVFP4 ≤ ~50–70 GiB.

> **⚠️ Flagship conclusion revised (2026-07-26, Tier-1 re-eval).** This sweep used a
> *headroom* lens and said "hold Step-3.5" for the big-shared slot. A follow-up under the
> corrected **Tier-1 doctrine** (both nodes fully dedicated, intelligence×speed first — see
> skills/model-discovery "Priority tiers") found the opposite: **Step-3.5-Flash is the
> *least* intelligent model that fits** (AA Intelligence Index 26). The flagship picks are
> **MiniMax-M2.7-AWQ** (AII 38, deployable on stock 26.04 today) and, with a patched
> container, **DeepSeek-V4-Flash** (AII 40, fast). The secondary-tier "hold" calls below
> still stand. Framed as continuous-eval candidates, not a one-time switch.

## Strategic verdict — does this change the ADR-0014 decision?

**Yes: it turns it green.** The headline finding is that **none of our three serving slots
is being superseded within this hardware.** The open *frontier* (GLM-5.2, Qwen3.5-397B,
MiniMax-M3, Kimi-K2.7/K3) has grown past 2× GB10 and now needs 4–8 GPUs; the incumbents
remain the best *fitting* models in their slots. So the mitigations we carry are **not**
protecting soon-obsolete models — the ADR-0014 work optimizes models that are staying, and
is therefore worth doing.

Two decision-relevant specifics:

1. **Reasoning slot → do the MTP-3 work (ADR-0014's top item), now.** The only model that
   out-reasons Qwen3.6-35B-A3B while fitting single-node NVFP4 — Nemotron-Puzzle-75B — is
   **~3× slower** and weak at tool-calling, so it can't replace the incumbent. The
   highest-leverage move stays "make the fast incumbent fast": MTP-3's ~28–30 → ~97 tok/s.
   And it's now **de-risked** — Puzzle-75B proves native MTP runs reliably on our *exact*
   stack (stock 26.06 / vLLM 0.22.1 / modelopt-NVFP4), and the older FlashInfer+MTP
   illegal-memory crash ([vllm#37754](https://github.com/vllm-project/vllm/issues/37754)) is
   fixed in current builds.
2. **New capability available: a vision slot is newly viable** — the one genuine *upgrade*
   the sweep surfaced (a capability expansion, not a replacement). Low-risk via the dense
   Qwen3-VL generation. See the VL row.

## Prioritized table

| Slot | Incumbent | Best candidate | Delta | Blockers | Recommendation |
|---|---|---|---|---|---|
| **General big-shared** (TP=2) | Step-3.5-Flash-FP8 (193 GiB) | Nemotron-3-Super-120B-A12B-NVFP4 (75 GiB) | Fresher + huge KV headroom, but **smaller** (120B-A12B vs 197B) → not a capability win | 26.06 (DEF-0002 on TP=2; dodge via TP=1) | **HOLD** incumbent; optional eval of Nemotron-Super on TP=1 |
| **Coding** (single-node NVFP4) | Qwen3-Coder-Next (46 GiB) | *(none stronger that fits)* | Still July-2026 efficiency SOTA (70.6% SWE-bench @ 3B active) | — | **HOLD**; watch Kimi-K3 (weights 2026-07-27) |
| **Reasoning-generalist** (single-node NVFP4) | Qwen3.6-35B-A3B (22 GiB) | Nemotron-Puzzle-75B-A9B-NVFP4 (50 GiB) | Stronger reasoning but **~3× slower** + weak tools → complement, not drop-in | custom Mamba-hybrid; tool-shape risk | **HOLD** incumbent → **do MTP-3 A/B** (ADR-0014); stage Puzzle-75B as reasoning-heavy eval |
| **Vision/multimodal** (NEW) | *(none)* | Qwen3-VL-32B-Instruct-NVFP4 (20 GiB, dense) | New capability; processor first-class in stock vLLM 0.22.1 | one-deploy smoke test (NVFP4 checkpoint unproven on stock image) | **STAND UP** as a `-single`/`-dual` slot (lowest-risk VL entry) |

## Per-slot detail

### General big-shared chat — HOLD Step-3.5-FP8
Frontier has outgrown the 2-node NVFP4 window (~215 GiB ceiling; Step-3.5 already sits near
it at 193 GiB). What's new *and* fitting is one tier down:
- **Nemotron-3-Super-120B-A12B-NVFP4** (`nvidia/…`, ~75 GiB) — top worth-eval: fresh reasoning-
  generalist on the proven modelopt/GB10 path, enormous KV headroom, fits **both** TP=2 and
  TP=1 single-node. Caveat: smaller than the incumbents → a freshness/headroom play, not a
  clear capability upgrade. Run TP=1 to sidestep DEF-0002.
- **Mistral-Medium-3.5-128B-NVFP4** (`nvidia/…`, ~89 GiB) — now has an **official calibrated**
  NVFP4 (was blocked on community-RTN-only); dense → ~2.5× KV/token of a same-weight MoE.
- **DeepSeek-V4-Flash-DSpark** (~156 GiB) — matured into a fast DGX-Spark release (~60–67 tok/s
  with spec-decode) but still rides **community sm_121-patched vLLM images, not the stock
  container** → blocked-until-stock under our "no forks" rule.
- Direct successor **Step-3.7-Flash-NVFP4** fits (60/node) but stays **blocked on DEF-0006**.

### Coding — HOLD Qwen3-Coder-Next
No official Qwen coder newer than Qwen3-Coder-Next (Jan 2026) — "Qwen3.6-Coder" hits are all
community merges/REAP-prunes. Everything genuinely stronger on agentic coding (GLM-5.2,
incoming Kimi-K3) is 5–15× too large for a GB10 node even at NVFP4; everything that fits
(Devstral-Small-2-24B FP8, Devstral-2-123B, GLM-4.7-Flash, DeepSeek-V4-Flash) is
comparable-to-weaker on agentic benchmarks or carries a quant/arch risk. Only worth-eval:
**Devstral-Small-2-24B** (FP8, ~24 GiB) as a lightweight *alternate* engine — not a replacement.

### Reasoning-generalist — HOLD incumbent, DO MTP-3
Covered in the strategic verdict. Secondary: **Gemma-4-31B-NVFP4** (`nvidia/…`, ~30 GiB) is a
clean dense generalist that fits with huge headroom but brings **no MTP throughput edge**.
Puzzle-75B is the only reasoning-*upgrade* that fits, and its cost (3× slower, weak tools)
is exactly why the fast incumbent + MTP-3 is the better play.

### Vision/multimodal — STAND UP a slot (dense Qwen3-VL)
The make-or-break mirrors DEF-0006 — *which multimodal processor the stock image ships*:
- **Qwen3-VL (2025 gen, `qwen3_vl`)** — processor upstream in vLLM since 0.11.0 → first-class
  in stock 0.22.1. **Viable.**
- **Qwen3.6-VL (2026 gen, `qwen3_5_vl_moe`)** — needs a patched processor
  ([vllm#49638](https://github.com/vllm-project/vllm/issues/49638)) → **blocked, same class as
  DEF-0006.** The natural VL sibling of our reasoning incumbent; track against stock-image updates.

Recommended entry: **Qwen3-VL-32B-Instruct-NVFP4** (dense, ~20 GiB, single-node) — dense NVFP4
rides the proven b12x sm_121 path and sidesteps *both* the GB10 NVFP4-MoE-vision FlashInfer
crash and the DEF-0004 Marlin-MoE hang that sink the MoE VL options. Flagship
**Qwen3-VL-235B-A22B-NVFP4** (official `nvidia/`, ~65 GiB/shard TP=2) is the quality ceiling but
inherits MoE-vision + DEF-0002 risk → verify-first. **Verification gap:** the community NVFP4
32B checkpoint was validated on *patched* containers; the arch is stock-supported but this
checkpoint booting a vision forward on stock 26.06 is a one-deploy smoke test — do it before
committing the profile.

## Recommended next actions (in order)

1. **Reopen ADR-0014 with MTP-3 on `qwen3.6-35b-a3b-nvfp4` first** — highest value, now de-risked;
   A/B single-stream TPS behind the fail-safe net, tool-shape check via the smoke gate.
2. **Then FP8-KV / prefix-caching re-test on 26.06** (DEF-0007) — the "may already be fixed"
   free win, on models confirmed here to be staying.
3. **Stand up a VL slot** — eval `Qwen3-VL-32B-Instruct-NVFP4` single-node (one-deploy smoke).
   A capability expansion, independent of ADR-0014.
4. **Fact-sheet / tracker follow-ups** (docs, when acted on): new sheets for Nemotron-Super-120B,
   Nemotron-Puzzle-75B, Qwen3-VL-32B; refresh existing `minimax-m3`, `deepseek-v4-flash`,
   `mistral-medium-3.5` (each has a material update). New blockers to track if we pursue VL:
   the `qwen3_5_vl_moe` processor gap (vllm#49638) and the NVFP4-MoE-vision FlashInfer crash on GB10.

## Sources
NVIDIA DGX Spark developer forums (Puzzle-75B, Super-120B MTP illegal-memory, DeepSeek-V4-DSpark
2× Spark, Qwen3.6-VL/NVFP4 hang threads), docs.nvidia.com Nemotron-Super Spark deployment guide,
vllm.ai DGX Spark blog, vLLM supported-models matrix + issues (#37754, #49638), and `hf` CLI tree
sizing. Full per-archetype findings are in this session's four research passes.
