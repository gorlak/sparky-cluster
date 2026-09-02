# Profiles — the allowlist, and what each entry serves

A *profile* is a YAML file under `ansible/profiles/<name>.yml` that fully describes
one serving configuration: which model(s), on which node(s), at what TP and `gmu`,
with what context length.

**The profiles directory *is* the allowlist** (ADR-0018). A profile file means "keep
these weights, install these engines" — `./sparky.sh deploy` (no argument) converges
the whole fleet to it, and `./sparky.sh activate <name>` then picks which one serves.
Adding a profile and *running* it are separate acts at separate privilege levels: the
first is a password-gated deploy, the second needs no root at all.

**Naming — the name is COPIED, never composed.** A profile's name is the upstream repo's
model name, **lowercased, verbatim**: `nvidia/Mistral-Medium-3.5-128B-NVFP4` →
`mistral-medium-3.5-128b-nvfp4`. Most names carry a quant because the *vendor* put it in
the repo name, not because we append one — so when a vendor ships a quant as its base repo
(`mistralai/Mistral-Medium-3.5-128B` is genuinely FP8) the profile is
`mistral-medium-3.5-128b`, and `-fp8` would be a name that matches nothing on the Hub.

> This used to read "profile names are the `<model>-<version>-<quant>` triple", which
> describes *building* a name and produced exactly that mistake on 2026-08-11. The rule is
> enforced by `topology.name_matches_repo` and `tests/test_topology.py`; if this prose and
> that function ever disagree again, **the function is right**.

The only suffixes we may invent are the closed set `topology.VARIANT_SUFFIXES`, and the
test for membership is **a second way of serving the SAME weights** — something a repo name
cannot express. Two kinds qualify: **topology** (`-single`, TP=1 instead of TP=2) and
**optimization** (`-eagle`, `-mtp3`, speculative decoding on against a bare-name twin with
it off). The optimization pair exists to be A/B'd (ADR-0014) — editing one profile in place
destroys the control. A **bare name is the TP=2
big-shared shape**, which since 2026-08-10 is every serving profile. A `-single`
suffix marks the snoopy-only TP=1 shape; none remain live, and the retired ones are
in [`../docs/models/retired/`](../docs/models/retired/). (`-dual` — one
independent engine per node — was retired earlier: two endpoints of one model buy
nothing without a round-robin in front.) TP=2 is no longer reserved for models too
big for one node; it measured faster for models that fit, too. `empty` is the special "nothing serving" profile.

This doc is the **catalog** of profiles that exist today. Companion docs:

- [`profile-tuning.md`](profile-tuning.md) — *why* the `gmu` and `context_length`
  values below were picked, with the per-model memory math and the GB10
  unified-memory accounting quirk.
- [`serving-topology.md`](serving-topology.md) — the `serving_topology` schema
  and how the `vllm` engine kind projects into the various roles.

## Catalog

Measured 2026-08-10 on the ADR-0016 HTTP-native harness. **`ctx` is what one request may
use (`context_length`); `KV` is what the cache actually holds** — the gap is the headroom
we are choosing not to offer, and on this fleet it is enormous.

| Profile | Model | decode | ctx / KV | notes |
|---|---|---|---|---|
| [`qwen3-vl-235b-a22b-instruct-nvfp4`](#qwen3-vl-235b-a22b-instruct-nvfp4) | Qwen3-VL-235B-A22B | 23.8 tok/s | 131k / 534k | **75.0%** MMLU-Pro subset — the accuracy leader. Vision + tools verified |
| [`nvidia-nemotron-3-super-120b-a12b-nvfp4`](#nvidia-nemotron-3-super-120b-a12b-nvfp4) | Nemotron-3-Super-120B-A12B | *decode unmeasured* | 262k / 23.5M (89.8× conc.) | new 2026-08-10; Puzzle's uncompressed upstream. Smoke-verified 2026-08-12 — KV measured, replacing the ~31M estimate |
| [`nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4`](#nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4) | Nemotron-3-Puzzle-75B-A9B | 32.0 tok/s | 131k / **28.2M** | the long-context model — hybrid Mamba, 8 of 88 layers attention |
| [`qwen3.6-35b-a3b-nvfp4`](#qwen36-35b-nvfp4) | Qwen3.6-35B-A3B | **100.2 tok/s** | 262k / 16.3M | the fast generalist; TPOT 9.6 ms. **Also a VL** — passes the vision gate, which the tables did not say until 2026-08-12 |
| [`qwen3-coder-next-nvfp4`](#qwen3-coder-next-nvfp4) | Qwen3-Coder-Next | 54.0 tok/s | 262k / 5.98M | coding; also the DEF-0003 exercise (no spec-decode masking it) |
| [`minimax-m2.7-nvfp4`](#minimax-m27-nvfp4) | MiniMax-M2.7 | 24.9 tok/s | 131k / 449k | best raw throughput (148.9 tok/s @16); reasons past the eval cap on 32% of items |
| [`mistral-small-4-119b-2603-nvfp4`](#mistral-small-4-119b-2603-nvfp4) | Mistral-Small-4-119B | **49.0 tok/s** | 262k / 2.35M (8.96× conc.) | the European option; 119B total but **~6.6B active** (128 experts, 4+1) + MLA — the shape this hardware wants. **Also a VL** — vision gate passed 2026-08-12 |
| [`empty`](#empty) | — | — | — | nothing serving; the fail-safe target |

**Every profile is TP=2 across both nodes.** That is not a coincidence and not a policy —
it is what the measurement said. Three paired TP=1/TP=2 profiles were benched back to
back on 2026-08-10 and TP=2 won on decode (1.34–1.59×), throughput (+41–50%) **and** KV
capacity, on a dense-MoE model, a second dense-MoE model, and a hybrid-Mamba model. The
`-single` twins were retired the same day; their configs live in
[`models/retired/`](models/retired/). See
[`profile-tuning.md`](profile-tuning.md), which used to assert the opposite.

The remaining cost of TP=2 is **fleet occupancy**: both nodes are committed, leaving
~24 GiB of dev headroom on sparky rather than the whole box. That is the only surviving
argument for a single-node profile, and no current model makes it.

### qwen3-vl-235b-a22b-instruct-nvfp4
- **Model:** `Qwen3-VL-235B-A22B-Instruct-NVFP4` (~127 GiB, ~63.5 GiB/shard); 26.07.
- **Serves as:** `qwen3-vl-235b` (plus the stable `sparky` alias) at `sparky:8000`.
- **Measured:** 75.0% on the committed MMLU-Pro subset with **zero** unparseable answers,
  reproduced to the decimal across two runs. 23.8 tok/s single-stream, 110.5 @16.
- **Tools:** `hermes`, and that name was *read from the chat template*, not guessed —
  `qwen3_xml` returned HTTP 200 with `{}` and garbage arguments, which a status-code check
  called a pass.
- **Vision works, and loses small detail SILENTLY.** Verified end to end through Caddy on
  the stable alias: a 12 MB / 3 MP upload is accepted and answered correctly when the
  subject is a reasonable fraction of the frame. Hold the subject at ~1% of the width and
  it returns HTTP 200 and a confident **wrong** answer rather than refusing — the encoder
  downscales, and detail below its effective resolution is gone before the model sees it.
  In practice: a small error message inside a full-screen screenshot may be misread, not
  flagged. **Crop to the region of interest.** There is no transport limit; the proxy
  passed 12 MB without complaint.
- **Workflow:** the default when you want the smartest answer and can wait for it.

### nvidia-nemotron-3-super-120b-a12b-nvfp4
- **Model:** `NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` (~74.8 GiB, ~37.4 GiB/shard); 26.07.
- **Status:** new 2026-08-10, **unmeasured** — treat the first activation as the test.
- **Why:** the uncompressed upstream of Puzzle (120B/A12B vs 75B/A9B) in the same NemotronH
  hybrid family, so it should inherit the enormous cache for ~25 GiB more disk.
- ⚠️ **`MIXED_PRECISION` despite the `-NVFP4` name**, and it declares an FP8 KV cache.
  Never pass `--quantization`. Same `MIXED_PRECISION`-behind-an-NVFP4-name shape as the
  retired `mistral-medium-3.5-128b-nvfp4`.

### nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4
- **Model:** `NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-NVFP4` (~50 GiB); 26.07.
- **Measured:** **28.2M KV tokens** (66.9 GiB, 215× concurrency) — the largest cache in
  the fleet by 2×, against a 131,072 ceiling. That is 215 full-length conversations, or
  0.5% of the cache in use.
  > Was documented as 35.2M until 2026-08-12. Re-measured twice that day — 28,130,067
  > during a busy activation sweep and 28,175,438 on a fully idle box, a 0.16% spread —
  > so host load was never the explanation. 35.2M would need ~83.5 GiB of KV, and
  > `memory_fraction: 0.80` yields 66.9 GiB after 23.11 GiB of weights.
- **Why so large:** of 88 layers only 8 are attention; the rest are Mamba and MLP, and a
  Mamba layer's state is fixed rather than growing per token. Context is nearly free here.
- **Workflow:** long documents, whole-codebase reading. Raise `context_length` before
  reaching for another model.

### qwen3.6-35b-a3b-nvfp4
- **Model:** `Qwen3.6-35B-A3B-NVFP4` (~22 GiB); 26.07. FP8 KV cache.
- **Measured:** the fastest thing we serve — **100.2 tok/s** single-stream, **9.6 ms** TPOT,
  540 tok/s at concurrency 16, with 16.3M KV tokens.
- **On MTP-3:** a `-mtp3-single` sibling ran spec-decode for a recorded 2.3× win. Re-measured
  on this harness it was the *slowest* of three shapes (35.3 tok/s) while forfeiting vision
  and constrained tool calling, and was retired — see [ADR-0014](adr/0014-optimization-register.md)'s
  second errata, including the caveat that its acceptance rate was never re-measured.

### qwen3-coder-next-nvfp4
- **Model:** `Qwen3-Coder-Next-NVFP4` (~45 GiB); 26.07. Tools via `qwen3_coder`.
- **Measured:** 54.0 tok/s, TPOT 18.0 ms, 5.98M KV.
- **Also the DEF-0003 exercise:** it runs no speculative decoding, so unlike the MTP sibling
  it does not accidentally mask the GB10 cudagraph hang. No hang observed on 26.07.

### minimax-m2.7-nvfp4
- **Model:** `MiniMax-M2.7-NVFP4` (~131 GiB, ~65.5 GiB/shard); 26.07. Soaked 64 min clean.
- **Measured:** best raw throughput at concurrency (148.9 tok/s @16) and the best
  prefix-cache TTFT. Accuracy reads 57.1% but is a **floor**, not a score: it ran past the
  4096-token cap on 32% of items, taking a median 821 s on those and still not concluding.
- **Workflow:** batch and concurrent work. Not interactive reasoning.

### mistral-medium-3.5-128b-nvfp4
- **Model:** `Mistral-Medium-3.5-128B-NVFP4` (~89 GiB); 26.07. **Unmeasured.**
- **Why it is here:** deliberate vendor diversity — every other model in the fleet is
  Chinese, and a European option is a tiebreaker worth keeping.
- ⚠️ Two traps, both paid for: the checkpoint is **`MIXED_PRECISION`**, not NVFP4 as the
  repo name says, and it needs **`--tokenizer-mode mistral`** on *both* ranks or it refuses
  to start with `must be an instance of MistralTokenizer`.
- ⚠️ **ON TRIAL — DEF-0012, unparked 2026-08-11 and never yet served.** The checkpoint
  ships BOTH HF and Mistral-native artifacts and they disagree, so HF's `PixtralProcessor`
  counts one image in the prompt text and zero in the ids. `--limit-mm-per-prompt` does
  **not** help — vLLM profiles multimodal regardless of the limit — and that WAR is gone.
  The candidate under test is **`--config-format mistral` on both ranks**, putting the
  config half on the native path the tokenizer half was already forced onto.
- **Read the startup log for the quant path.** `params.json` carries no
  `quantization_config`, so if `--config-format mistral` loses the MIXED_PRECISION layer
  map the model loads unquantized or refuses. If it fails, the verdict is about **sourcing
  this checkpoint**, not about the model: the fallback is an official Mistral-native FP8
  or a pure-HF NVFP4 build.


### mistral-small-4-119b-2603-nvfp4
- **Model:** `Mistral-Small-4-119B-2603-NVFP4` (65.9 GiB, ~33 GiB/node at TP=2); 26.07.
  **Never served.** Staged and verified 2026-08-11 — 23/23 files size-exact against the Hub.
- **Why it is here:** the European slot, sourced properly after DEF-0012. It is the only
  Mistral candidate whose *packaging* cannot reproduce that failure — **pure Mistral-native**
  (`params.json` + `tekken.json` + `consolidated.safetensors.index.json`, no HF artifacts at
  all), with the quant config **inside `params.json`**, so `--config-format mistral` cannot
  discard it the way it did on the NVFP4 sibling.
- **The flags travel as a trio** — `--tokenizer-mode` + `--config-format` + `--load-format`,
  all `mistral`, on both ranks. Two of three is what broke DEF-0012.
- ⚠️ **compressed-tensors MoE** (128 experts, 4 active + 1 shared) — [vllm#50925](https://github.com/vllm-project/vllm/issues/50925)
  says that combination falls back to **Marlin** on sm_121, which is DEF-0004's territory
  and a node-freeze rather than a hang. **Attend its first activation**, and do not reach
  for `VLLM_NVFP4_GEMM_BACKEND=marlin` to fix a load failure without reading DEF-0004.
- **MLA head_size is 320** (`kv_lora_rank 256 + qk_rope_head_dim 64`) — the exact value a
  GB10 forum report could not run on vLLM 0.17.2rc1, forcing `VLLM_MLA_DISABLE=1` and a 40k
  context cap. Whether 0.24.0 supports it is the most valuable thing the first activation
  will tell us: with MLA the KV is ~22.5 KiB/token, without it ~25× worse.


## Switching what serves

```sh
./sparky.sh activate <name>   # make it live — no root; waits, then runs the smoke gate
./sparky.sh activate          # what's live, and what's activatable
./sparky.sh activate empty    # stop serving
./sparky.sh fleet             # the allowlist: deployed / live / parked, and where the weights are
```

`activate` writes the requested profile to `/opt/cluster/desired-profile` (a
group-writable file — **no sudo**), then triggers the fixed reconciler through its
single-command sudoers entry. The reconciler:

- **re-validates** the request against the allowlist and the installed env files **on
  every node** — a worker never takes a profile the head invented;
- writes each node's desired markers (`/opt/vllm/active/<engine>`) as an
  all-or-nothing transaction, *then* drives systemd to match. The markers are the
  source of truth, so a run killed mid-flight is repaired by simply re-driving to them;
- **stops fleet-wide before starting anywhere**, then starts workers before the head —
  otherwise a new worker rank would attach to the outgoing head's rendezvous store;
- fails the whole fleet to `empty` if any node errors, and reports why.

Nothing else moves. Open WebUI, Prometheus and Caddy point at a fixed, model-agnostic
endpoint, so they need no reconfiguration when the model changes — which is exactly
what lets this operation be unprivileged.

For the **live** state: `/admin`, `./sparky.sh status`, or
`cat /opt/cluster/current-topology.json`. For what a deploy *installed*:
`./sparky.sh fleet` or `cat /opt/cluster/fleet.json`.

## Removing a profile

Delete its `.yml` and `./sparky.sh deploy`. The deploy reports the weights that are
now unreferenced and leaves them; `./sparky.sh deploy --evict` deletes them, per node.
It will never delete the model that is currently serving — if the live profile is the
one leaving the allowlist, the deploy drives the fleet to `empty` and waits for the
engine to stop first. To keep the weights but stop it being activatable, set
`blocked: true` instead. *Block to park it; delete the file to evict it* — the gestures and their consequences
are described in the README's allowlist section.

## Adding a new profile

The **procedure** is owned elsewhere and is not repeated here: [[model-bringup]] for the
sequence from staged weights to serving, [[model-evaluation]] for the fit checks and flag
decisions, [`updating.md`](updating.md) for the fan-out (every place that must move
together), and [`profile-tuning.md`](profile-tuning.md) for choosing
`memory_fraction` and `context_length`.

What this file adds is the **catalogue above** — what each profile is and why — and the
constraints below, which are properties of the fleet rather than steps in a procedure.

Two constraints the fleet enforces, worth knowing before you write the file:

- **Engine names are unique fleet-wide**, not just within a profile — an engine name
  is its systemd instance (`vllm@<name>.service`) *and* its env file path.
- **Every engine listens on port 8000.** At most one is live fleet-wide, which is what
  lets the stable endpoint be a static health-checked upstream list. If you ever want
  two models live at once, that needs its own port/hostname route — and a written
  decision first.
- A serve flag may contain spaces and double quotes but **not a single quote**: flags
  travel to systemd as one single-quoted value that is re-split on whitespace with no
  quote processing. Write JSON args unspaced and unquoted —
  `--speculative-config {"method":"mtp","num_speculative_tokens":3}`.

See [`serving-topology.md`](serving-topology.md) for the full schema (every
field an engine entry can take).
