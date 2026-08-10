# Profiles — the allowlist, and what each entry serves

A *profile* is a YAML file under `ansible/profiles/<name>.yml` that fully describes
one serving configuration: which model(s), on which node(s), at what TP and `gmu`,
with what context length.

**The profiles directory *is* the allowlist** (ADR-0018). A profile file means "keep
these weights, install these engines" — `./sparky.sh deploy` (no argument) converges
the whole fleet to it, and `./sparky.sh activate <name>` then picks which one serves.
Adding a profile and *running* it are separate acts at separate privilege levels: the
first is a password-gated deploy, the second needs no root at all.

**Naming.** Profile names are the `<model>-<version>-<quant>` triple
(e.g. `qwen3-coder-next-nvfp4`, `minimax-m2.7-nvfp4`). A **bare name is the TP=2
big-shared shape**, which since 2026-08-10 is every serving profile. A `-single`
suffix marks the snoopy-only TP=1 shape; none remain live, and the retired ones are
in [`../ansible/profiles/retired/`](../ansible/profiles/retired/). (`-dual` — one
independent engine per node — was retired earlier: two endpoints of one model buy
nothing without a round-robin in front.) TP=2 is no longer reserved for models too
big for one node; it measured faster for models that fit, too. `empty` is the special "nothing serving" profile.

This doc is the **catalog** of profiles that exist today. Companion docs:

- [`profile-tuning.md`](profile-tuning.md) — *why* the `gmu` and `max_model_len`
  values below were picked, with the per-model memory math and the GB10
  unified-memory accounting quirk.
- [`serving-topology.md`](serving-topology.md) — the `serving_topology` schema
  and how each engine kind (`vllm`, `ollama`) projects into the various roles.

## Catalog

Measured 2026-08-10 on the ADR-0016 HTTP-native harness. **`ctx` is what one request may
use (`max_model_len`); `KV` is what the cache actually holds** — the gap is the headroom
we are choosing not to offer, and on this fleet it is enormous.

| Profile | Model | decode | ctx / KV | notes |
|---|---|---|---|---|
| [`qwen3-vl-235b-a22b-instruct-nvfp4`](#qwen3-vl-235b-a22b-instruct-nvfp4) | Qwen3-VL-235B-A22B | 23.8 tok/s | 131k / 534k | **75.0%** MMLU-Pro subset — the accuracy leader. Vision + tools verified |
| [`nvidia-nemotron-3-super-120b-a12b-nvfp4`](#nvidia-nemotron-3-super-120b-a12b-nvfp4) | Nemotron-3-Super-120B-A12B | *unmeasured* | 262k / ~31M est | new 2026-08-10; Puzzle's uncompressed upstream |
| [`nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4`](#nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4) | Nemotron-3-Puzzle-75B-A9B | 32.0 tok/s | 131k / **35.2M** | the long-context model — hybrid Mamba, 8 of 88 layers attention |
| [`qwen3.6-35b-a3b-nvfp4`](#qwen36-35b-nvfp4) | Qwen3.6-35B-A3B | **100.2 tok/s** | 262k / 16.3M | the fast generalist; TPOT 9.6 ms |
| [`qwen3-coder-next-nvfp4`](#qwen3-coder-next-nvfp4) | Qwen3-Coder-Next | 54.0 tok/s | 262k / 5.98M | coding; also the DEF-0003 exercise (no spec-decode masking it) |
| [`minimax-m2.7-nvfp4`](#minimax-m27-nvfp4) | MiniMax-M2.7 | 24.9 tok/s | 131k / 449k | best raw throughput (148.9 tok/s @16); reasons past the eval cap on 32% of items |
| [`mistral-medium-3.5-128b-nvfp4`](#mistral-medium-35-nvfp4) | Mistral-Medium-3.5-128B | *unmeasured* | 131k / — | the European option; `MIXED_PRECISION`, `--tokenizer-mode mistral` |
| [`step-3.7-flash-nvfp4`](#step-37-nvfp4) | Step-3.7-Flash-NVFP4 | — | — | ⛔ **PARKED** — DEF-0006, re-probed on 26.07 and still missing |
| [`empty`](#empty) | — | — | — | nothing serving; the fail-safe target |

**Every profile is TP=2 across both nodes.** That is not a coincidence and not a policy —
it is what the measurement said. Three paired TP=1/TP=2 profiles were benched back to
back on 2026-08-10 and TP=2 won on decode (1.34–1.59×), throughput (+41–50%) **and** KV
capacity, on a dense-MoE model, a second dense-MoE model, and a hybrid-Mamba model. The
`-single` twins were retired the same day; their configs live in
[`ansible/profiles/retired/`](../ansible/profiles/retired/). See
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
  Never pass `--quantization`. Same shape as `mistral-medium-3.5-128b-nvfp4`.

### nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4
- **Model:** `NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-NVFP4` (~50 GiB); 26.07.
- **Measured:** **35.2M KV tokens** — the largest cache in the fleet by 2×, against a
  131,072 ceiling. That is 268 full-length conversations, or 0.4% of the cache in use.
- **Why so large:** of 88 layers only 8 are attention; the rest are Mamba and MLP, and a
  Mamba layer's state is fixed rather than growing per token. Context is nearly free here.
- **Workflow:** long documents, whole-codebase reading. Raise `max_model_len` before
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
- **Open:** DEF-0012 — `--limit-mm-per-prompt` does not skip multimodal profiling.

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
`gpu_memory_utilization` and `max_model_len`.

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
