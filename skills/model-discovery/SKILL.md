---
name: model-discovery
description: Find new models or quantizations that fit this cluster — search HuggingFace (via the `hf` CLI) AND scour the dual-DGX-Spark community (NVIDIA forums, vLLM issues) for what actually runs on GB10. Use when asked to check for better models, newer quantizations, or what's new that fits.
---

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
- **Inspect:** `hf models info <repo>` (metadata + tags) · `hf models card <repo>` (model card).
- **Size a quant BEFORE downloading:** `hf models ls <repo> --tree -h`, then sum the
  `*.safetensors` sizes. Disk ≈ VRAM footprint for quantized weights.

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

Using the searches above (and vLLM release notes / leaderboards for context), look for:

1. **Better headroom:** Models with FP8 footprint well under 108.9 GiB/node — ideally
   under 80 GiB/node so KV cache and prefix caching have room to breathe.
   Disk size ≈ VRAM footprint for FP8/quantized models.

2. **Newer quantizations of current model:** Any new FP8, AWQ, or GPTQ releases
   of Step-3.5-Flash, or successor models from StepFun AI.

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
