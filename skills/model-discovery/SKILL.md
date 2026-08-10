---
name: model-discovery
description: Find new models or quantizations that fit this cluster — search HuggingFace (via the `hf` CLI) AND scour the dual-DGX-Spark community (NVIDIA forums, vLLM issues) for what actually runs on GB10. Use when asked to check for better models, newer quantizations, or what's new that fits.
---

## Priority tiers — what this cluster is FOR

Two GB10 nodes were bought and wired with 200 Gbit RoCE **specifically to run one model
across both, dedicated** — maximum intelligence and speed, both machines fully committed.
Rank every sweep by these tiers:

- **Tier 1 — the point.** The **smartest model that fits TP=2 fully-committed across both
  nodes and still runs reasonably fast** — multimodal too, when the container supports its
  vision path. This flagship slot is the primary job. "Fully committed" means filling the
  ~215 GiB two-node budget (~108 GiB/node) with the best weights — a candidate that only
  fits by leaving a node or half the RAM idle is *under-using the hardware*, not winning.
- **Tier 2 — secondary / experimental.** Single-node (`-single`, on snoopy by design) and
  dev-headroom profiles exist only to spare a node for experimentation. **"Frees a node" is a cost here,
  not a feature.**

So a sweep leads with the Tier-1 question: *is the current flagship still the smartest
fast-enough model that fills both nodes?* Everything else is secondary. When Tier-1
candidates are close on intelligence+speed, **vendor/region diversity is a tiebreaker** —
the suite is currently all-Chinese, so a strong European option (Mistral especially) or
other diversity is a plus, all else near-equal.

## Cluster Constraints

- **Hardware:** 2× NVIDIA GB10 (SM 12.1), 121 GiB unified memory each
- **Runtime:** `nvcr.io/nvidia/vllm:26.04-py3` (vLLM 0.19, no Ray)
- **Memory budgeting is per-profile, not universal** — see
  [`docs/profile-tuning.md`](../../docs/profile-tuning.md). Rough rules of thumb
  for what fits *at all*:
  - Big-shared TP=2 at the upper end (`gmu` 0.90, fully-committed): each shard
    must fit ~108 GiB.
  - Big-shared TP=2 with dev headroom (`gmu` ~0.75): each shard ≤ ~85 GiB.
  - Per-node single-engine (no TP): weights ≤ ~50–70 GiB depending on
    desired headroom.
  - **The 2-node ceiling is ~215 GiB total weights** (TP=2 fully-committed). As of
    2026-07 the open *frontier* has outgrown it: GLM-5.2 (~350–450 GiB NVFP4),
    Qwen3.5-397B / MiniMax-M3 (~234 GiB), Kimi-K2.7 all need 4–8 GPUs. Don't burn a
    sweep re-confirming they don't fit — the productive band for this cluster is the
    tier below (~50–160 GiB: Nemotron-Super-120B, Mistral-Medium-128B, the 30–75B
    NVFP4 line). Note frontier models only to record *why* they're out of range.
- **Current loadout varies** — check `/opt/cluster/current-topology.json`,
  the panel at `/admin`, or [`docs/profiles.md`](../../docs/profiles.md) for the
  active profile.
- **Do not suggest:** `Qwen3.5-122B-A10B-FP8` (froze sparky), any model requiring Ray

## How to search — the `hf` CLI

`hf` is HuggingFace's CLI (the `gh` of the Hub). Prefer it over web scraping —
it's structured, scriptable, and can size a repo *without downloading it*.

- **Discover:** `hf models ls --search "<query>" --author <org> --sort last_modified --limit 20`
  - Filter to what fits: **`--num-parameters 'min:100B,max:250B'`** (maps to our
    TP=2 window), plus `--filter fp8` / `--filter modelopt` (NVFP4) / `--filter awq`.
  - Machine-readable: add `--json` (or `--format agent`).
  - **NVFP4 lives under the `nvidia/` org.** For the proven GB10 path (modelopt FP4),
    the calibrated quant is usually published by NVIDIA — for its *own* models
    (Nemotron) *and* third-party (Mistral-Medium, Qwen3-VL, Gemma, GLM). Sweep it
    directly: `hf models ls --author nvidia --filter modelopt --sort last_modified`.
    A community NVFP4 is often RTN/uncalibrated — prefer the `nvidia/` calibrated one
    when it exists (checked 2026-07: it increasingly does).
- **Inspect:** `hf models info <repo>` (metadata + tags) · `hf models card <repo>` (model card).
- **Size a quant BEFORE downloading:** `hf models ls <repo> --tree -h`, then sum the
  `*.safetensors` sizes. Disk ≈ VRAM footprint for quantized weights.
  - **`-h` reports SI GB (÷10⁹); the memory math is in GiB (÷2³⁰).** Convert:
    `GiB = GB / 1.0737` (~7% smaller). Calibrated 2026-07 against the measured
    incumbent (Step-3.5-FP8: `hf` ~207 GB → 193 GiB actual). Skip this and every
    fit call runs ~7% optimistic.

Examples:
- Successors/quants of what we run: `hf models ls --author stepfun-ai --sort last_modified`
- Fit-window sweep: `hf models ls --search flash --num-parameters 'min:100B,max:200B' --filter fp8`

## Beyond the Hub — GB10 community intel

HuggingFace tells you a quant *exists*; it does **not** tell you whether it runs on a
*dual* DGX Spark. GB10 (sm_121, no inter-node NVLink, RoCE) is niche, and its failure
modes are hardware-specific — the 26.06 NVLS hang is the poster child (a shipped
container that HF and vLLM release notes said nothing about). The people running this
exact setup congregate in a few places; check them before recommending or downloading
anything non-trivial:

- **NVIDIA Developer Forums — the "DGX Spark / GB10" category** (`forums.developer.nvidia.com`).
  Primary source: dual-Spark users posting real results — which quants serve, which
  container/driver combos work, NCCL/CUDA hangs and their workarounds, TP-vs-PP findings.
  Search `site:forums.developer.nvidia.com DGX Spark <model or topic>`.
- **vLLM GitHub issues**, filtered to this hardware: `GB10` / `sm_121` / `DGX Spark` /
  `TP=2` (hangs, cudagraph, NVFP4). Note each issue's state and whether a workaround
  (env var, `--enforce-eager`, PP) is *confirmed*.
- **Community configs**: `eugr/spark-vllm-docker`, `mark-ramsey-ri/vllm-dgx-spark`,
  `Sggin1/DGX-SPARK` — working docker/serve setups and benchmarks for dual Spark.

Use `WebSearch` to find threads, then `WebFetch` the promising ones for the specifics
that matter (exact NCCL/container versions, flags, whether it needed a hard reset).
**Fold anything load-bearing into the relevant `docs/upgrades/` tracker or
`docs/models/` fact sheet** so it isn't re-discovered later. A forum report of "it hangs
on GB10" for a container/quant we don't run yet is a **blocker to record**, not a detail
to skip.

**Screen candidates against [`docs/models/tombstones.md`](../../docs/models/tombstones.md)
before reporting them.** That register owns the verdicts on models already rejected; a
sweep that re-surfaces one has cost time twice. If a tombstoned model reappears as a
genuinely good idea, the answer is to check its *Reconsider when* — not to re-do the
analysis. And when a sweep rejects a model for a reason about the **model itself** (does
not fit on this hardware; hangs the node), add a row there rather than burying it in a
scouting report.

## Polling etiquette (be a good web citizen)

These sources are other people's servers — NVIDIA's community forum (a Discourse
instance with real rate limits) most of all. `WebSearch`/`WebFetch` and the official
APIs (`hf`, `gh`) already handle the basics (proper user-agent, one request per fetch,
`WebFetch` caches each URL ~15 min). Keep it light on top of that:

- **Authenticate — it's the sanctioned path *and* the polite one.** HF and GitHub
  publish the very APIs we use (`hf`, `gh`) for programmatic access; a token gives you
  their higher rate limits (GitHub: 60→5,000 req/hr) and identifies our traffic instead
  of hammering anonymously. Run `hf auth login` / `gh auth login` yourself.
- **Prefer the API to scraping.** `hf models …` and `gh` over fetching HTML — they're
  rate-limited and cached server-side and don't load a whole rendered page. GitHub's
  acceptable-use permits the API but restricts HTML scraping; the API is the blessed route.
- **Honor `robots.txt`.** NVIDIA's forum permits general agents on public thread/category
  pages (`/t/`, `/c/`) but disallows `/admin`, `/auth`, `/session`, and `api_key` params —
  stay on public threads, never those. No `Crawl-delay` is set, but Discourse rate-limits
  server-side, so the back-off rule below is the real limit.
- **Target, then fetch a few.** `WebSearch` to find the 1–3 relevant threads, then
  `WebFetch` only those. Never crawl or paginate a forum category or an issue list.
- **Reuse, don't re-fetch.** `WebFetch` caches per URL (~15 min) — don't request the
  same page twice in a session.
- **Low cadence, not a loop.** This runs on an event (a release, a periodic review, an
  unblocked dependency) — never a cron that polls the forum continuously.
- **Back off on limits.** A `429` / robots block / auth wall means stop, not retry
  harder. Never bypass gated content.
- **The repo is the cache.** Record what you learn in `docs/upgrades/`, `docs/models/`,
  and `scouting-reports/` here — so the *next* sweep reads our notes instead of hitting
  NVIDIA's servers again for the same answer. Recording once is the kindest thing we do.

## What to Look For

**Model-first, quant-to-fit — we compare *models*, not quantizations.** For each candidate
model, pick the single **best quant that fits** our RAM at the target profile shape (the
highest quality that loads with acceptable headroom) and present *that* as the candidate. A
model's quants are not competing options to A/B — the quant is chosen to fit the hardware
(e.g. for a big-shared TP=2 slot, take NVFP4 over FP8 when NVFP4 fits with headroom and FP8 is
fully-committed). Profiles are named with the `<model>-<version>-<quant>` triple — the chosen
quant *is* in the name (`qwen3-coder-next-nvfp4`, `minimax-m2.7-nvfp4`), plus a `-single` topology
suffix for the single-node (snoopy) shape. Quant-to-fit still governs *which* quant you pick; the name
just records it.

Using the searches above (and vLLM release notes / leaderboards for context), look for:

1. **Better headroom:** Models whose best-fitting quant lands well under 108.9 GiB/node —
   ideally under 80 GiB/node so KV cache and prefix caching have room to breathe.
   Disk size ≈ VRAM footprint for quantized models.

2. **Newer generations of what we run:** successor models from the same family (Step-3.5 →
   3.7; MiniMax-M2.7 → M3; a newer Qwen), each taken at its best-fitting quant.

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
- Headroom against the relevant `gmu` budget (varies per profile shape — see [`docs/profile-tuning.md`](../../docs/profile-tuning.md))
- Any known vLLM compatibility issues
- Why it's better or worse than what we're running

Flag anything that needs investigation before committing to a download.

## Self-improvement — leave this skill sharper than you found it

Every sweep teaches two different things; file each in its home:

- **What you found** (models, quants, sizes, GB10 viability) → the findings: a dated
  `scouting-reports/<YYYY-MM>-*.md`, `docs/models/<model>.md` fact sheets, and
  `docs/upgrades/` trackers (see [[model-evaluation]]). Not here.
- **How discovery went** (the *method*) → **this skill.** In the same change set, fold
  back what you learned about *searching*: an `hf` query/filter that surfaced fits (or
  one that wasted time), a new community source that had the answer, a sizing gotcha, a
  calibration that saves the next sweep from re-checking dead ends. Add the query that
  worked; retire the one that didn't.

A sweep that taught you something about how to search but left this skill unchanged
threw that lesson away. Keep the edits concrete and small — this is a living instrument.
