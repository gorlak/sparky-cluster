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
(e.g. `step-3.5-fp8`, `minimax-m2.7-nvfp4`). A `-single` / `-dual` **topology
suffix** marks the per-node (TP=1) shapes — `-dual` = one independent engine on
*each* node, `-single` = snoopy only. The **suffixless** big-model profiles are
**TP=2 across both nodes** (one model sharded); TP=2 is used only where a model
is too big for one node. `empty` is the special "nothing serving" profile.

This doc is the **catalog** of profiles that exist today. Companion docs:

- [`profile-tuning.md`](profile-tuning.md) — *why* the `gmu` and `max_model_len`
  values below were picked, with the per-model memory math and the GB10
  unified-memory accounting quirk.
- [`serving-topology.md`](serving-topology.md) — the `serving_topology` schema
  and how each engine kind (`vllm`, `ollama`) projects into the various roles.

## Catalog

| Profile | Shape | gmu | `max_model_len` | Outside headroom (per node) | Workflow archetype |
|---|---|---|---|---|---|
| [`step-3.5-fp8`](#step-35-fp8) | TP=2 big-shared | 0.90 | 32768 | ~5 GiB | fully-committed |
| [`step-3.7-nvfp4`](#step-37-nvfp4) | TP=2 big-shared (**26.07**) | 0.75 | 32768 | ~30 GiB | ⛔ **PARKED** (`blocked: true`) — upstream vLLM VL bug |
| [`minimax-m2.7-nvfp4`](#minimax-m27-nvfp4) | TP=2 big-shared (**26.07**) | 0.80 | 131072 | ~24 GiB | big-shared with dev headroom |
| [`qwen3-coder-nvfp4-single`](#qwen3-coder-nvfp4-single) | snoopy TP=1 (**26.07**) | 0.55 | 262144 | sparky free + ~55 GiB on snoopy | coder, sparky-free for dev |
| [`qwen3.6-35b-nvfp4-mtp3-single`](#qwen36-35b-nvfp4-mtp3-single) | snoopy TP=1 (**26.07**, MTP-3) | 0.55 | 262144 | sparky free + ~55 GiB on snoopy | reasoning-generalist (2.3× single-stream) |
| [`empty`](#empty) | no engines | — | — | full hardware | bare cluster |

### step-3.5-fp8
- **Model:** `Step-3.5-Flash-FP8` (~195 GiB total, ~97.5 GiB per shard); container 26.04.
- **Serves as:** `step-3.5-flash` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Why this tuning:** weights nearly fill the per-node budget. At `gmu 0.90`
  KV available is ~11.7 GiB — `max_model_len 32768` is the
  empirically-confirmed value (sliding_window=512 may allow more; not measured).
- **Workflow:** fully committed. Use other machines for dev that day.

### step-3.7-nvfp4
- **Model:** `Step-3.7-Flash-NVFP4` (~129 GiB total, ~64.5 GiB per shard); **pins container 26.07**
  (per-profile override — NVFP4/modelopt needs it).
- **Serves as:** `step-3.7-flash` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Status:** ⛔ **PARKED** — `blocked: true`, so it stays out of the allowlist file the
  reconciler validates against: its weights and engine env files are installed by every
  deploy (re-testing costs no download), but `activate` refuses it on every node. Drop
  the `blocked:` line and deploy to re-test when the fix lands.
  **NVFP4 loaded + ran on 26.06 with no hang** (2026-07-02) — the hard part works
  and per-profile pinning is validated. The remaining blocker is an upstream vLLM bug, not
  NVFP4/the container: Step-3.7's `Step3VLProcessor` crash-loops on startup (missing
  `_get_num_multimodal_tokens`). **Unblock when** vLLM ships the fix. See
  [`docs/upgrades/container-nvidia-vllm-26.06-py3.md`](upgrades/container-nvidia-vllm-26.06-py3.md)
  and [`docs/models/step-3.7-flash.md`](models/step-3.7-flash.md).
- **Workflow:** big-shared with ~30 GiB/node headroom (NVFP4 roughly halves the FP8 footprint).

### minimax-m2.7-nvfp4
- **Model:** `MiniMax-M2.7-NVFP4` (~131 GiB total, ~70 GiB per shard; modelopt NVFP4 MoE);
  **pins container 26.07**.
- **Serves as:** `minimax-m2` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Retired its AWQ sibling (2026-08-08).** `minimax-m2.7-awq` served the same model as
  compressed-tensors int4 on container 26.04. It is gone: the Marlin MoE loader that
  quant needs is [DEF-0004](defects.md), which **froze a node outright** on 26.07, so AWQ
  could never leave 26.04 — while this profile serves the same model on the current
  container with hardware FP4 on Blackwell. Keeping a permanently-pinned duplicate of a
  model we already serve better cost 122 GiB per node and bought nothing.
- **Why this tuning:** NVFP4 weights are *bigger* than the retired AWQ (~70 vs ~61 GiB/node), so
  `gmu 0.80` → ~26 GiB KV/rank budgeted (**30.58 GiB measured**, 449,664 tokens) and
  ~24 GiB/node outside headroom. Soaked 64 min at concurrency 8, **1312/1312 HTTP 200**
  with flat latency (p50 21.8 s / max 23.4 s) — DEF-0002's deadlock window passed clean.
- **Workflow:** big-shared with ~24 GiB/node headroom.

### qwen3-coder-nvfp4-single
- **Model:** `Qwen3-Coder-Next-NVFP4` (80B-A3B linear-attention hybrid, ~45 GiB NVFP4;
  compressed-tensors — staged as the `RedHatAI/Qwen3-Coder-Next-NVFP4` build; the
  GB10-targeted `saricles/Qwen3-Coder-Next-NVFP4-GB10` is an alternative if needed);
  **pins container 26.07**. Coding-tuned agent.
- **Engine:** `qwen3-coder-snoopy` (`snoopy:8000`), TP=1. Single-node serving runs on
  snoopy by design — sparky stays free for the frontends + dev.
- **Why per-node, not TP=2:** weights fit one node with room to spare; with no NVLink between
  the Sparks, cross-node TP=2 for a 3B-active model is bandwidth/latency-bound and would only
  *cost* decode throughput. See the tuning doc for the full TP=2 cost analysis.
- **Why this tuning:** `gmu 0.55` → ~20 GiB KV (cheap here — hybrid attn), ~55 GiB/node free
  for dev/builds. **Fallback:** if compressed-tensors won't load, use the official
  `Qwen/Qwen3-Coder-Next-FP8` (80 GiB) and rename the family to `-fp8`.

### qwen3.6-35b-nvfp4-mtp3-single
- **Model:** `Qwen3.6-35B-A3B-NVFP4` (35B-A3B MoE, ~22 GiB NVFP4; **nvidia modelopt** —
  the proven-loads path); **pins container 26.07**. Reasoning-generalist (also VL —
  run **text-first**; **MTP-3 on** — 2.3× single-stream decode, ADR-0014; MTP corrupts
  image number-reads so it stays text-only). Arch `Qwen3_5MoeForConditionalGeneration`,
  registry-confirmed on 26.07's vLLM 0.24.0. **Constrained tool choice (`required`,
  named-function) 500s here** — MTP breaks structured output on 0.24.0 (DEF-0011);
  `auto`, which is what Open WebUI sends, is unaffected.
- **Engine:** `qwen3.6-35b-snoopy` (`snoopy:8000`), TP=1. Single-node serving on snoopy
  by design.
- **Why per-node, not TP=2:** same as the coder family — fits one node, cross-node TP=2 is a
  throughput loss for a 3B-active model.
- **Why this tuning:** `gmu 0.55` → ~43 GiB KV (ample; cheap hybrid-attn KV), ~55 GiB/node
  free. Drop to ~0.45 for more dev headroom — KV is already overkill.

### empty
- **What it serves:** nothing. Only the always-on services (Caddy, control panel,
  Open WebUI, Prometheus, Grafana, exporters) — the cluster stays observable and
  reachable while both GPUs are free.
- **What activating it does:** declares no engines, so the reconciler clears every
  desired marker fleet-wide and stops every engine. Nothing is *uninstalled* — weights,
  env files and enabled units all stay, so the next activation is just a start.
- **Also the fail-safe target.** Any uncertainty — an unreadable request, an unknown
  profile, a node that fails to reconcile — lands here rather than guessing a model
  onto the GPU. It is always activatable, even if the allowlist file is missing, so
  recovery never depends on a file being right.
- **Workflow:** working with cloud AI (Claude etc.), running the cluster as a
  build farm, or just freeing the hardware.

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
`blocked: true` instead. *Block to park it; delete the file to evict it.*

## Adding a new profile

1. **Stage weights** in the inbox on sparky (from the repo root):
   ```sh
   ./sparky.sh download <hf-repo>
   ```
   Runs `scripts/download.py` via `uv` (provisions `huggingface_hub` itself — no local
   `hf` install needed) and writes a flat copy into the inbox. The next deploy moves it
   into the canonical store and mirrors to every node.
2. **Copy** an existing profile that matches your shape:
   - big-shared TP=2 → start from `step-3.5-fp8.yml` or `minimax-m2.7-nvfp4.yml`
   - single-node small → start from `qwen3-coder-nvfp4-single.yml` (snoopy, TP=1)
3. **Pick `gpu_memory_utilization` and `max_model_len`** per
   [`profile-tuning.md`](profile-tuning.md) — decide your *outside-headroom*
   target first, give vLLM the rest.
4. **`./sparky.sh lint`** (validates the whole allowlist — fleet-wide-unique engine
   names, the one front port, flags that survive the env-file round trip), then
   **`./sparky.sh check`** to dry-run and **`./sparky.sh deploy`** to install it.
5. **`./sparky.sh activate <name>`** to serve it.

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
