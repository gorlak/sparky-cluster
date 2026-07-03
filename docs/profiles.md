# Profiles — the deployable cluster configurations

A *profile* is a YAML file under `ansible/profiles/<name>.yml` that fully
describes what the cluster serves: which model(s), on which node(s), at what
TP and `gmu`, with what context length, plus which front-end services run.
Deploy with `make deploy PROFILE=<name>` from `ansible/`.

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
| [`step-3.7-nvfp4`](#step-37-nvfp4) | TP=2 big-shared (**26.06**) | 0.75 | 32768 | ~30 GiB | ⛔ **BLOCKED** — upstream vLLM VL bug; hidden from deploy UI |
| [`minimax-m2.7-awq`](#minimax-m27-awq) | TP=2 big-shared | 0.75 | 131072 | ~30 GiB | big-shared with dev headroom |
| [`minimax-m2.7-nvfp4`](#minimax-m27-nvfp4) | TP=2 big-shared (**26.06**) | 0.80 | 131072 | ~24 GiB | NVFP4 A/B candidate vs the AWQ profile |
| [`qwen3-coder-nvfp4-dual`](#qwen3-coder-nvfp4-dual--single) | per-node ×2 (**26.06**) | 0.55 | 262144 | ~55 GiB | small + dev-friendly |
| [`qwen3-coder-nvfp4-single`](#qwen3-coder-nvfp4-dual--single) | per-node ×1 (snoopy, **26.06**) | 0.55 | 262144 | sparky free + ~55 GiB on snoopy | sparky-free for dev |
| [`qwen3.6-35b-nvfp4-dual`](#qwen36-35b-nvfp4-dual--single) | per-node ×2 (**26.06**) | 0.55 | 262144 | ~55 GiB | reasoning-generalist A/B |
| [`qwen3.6-35b-nvfp4-single`](#qwen36-35b-nvfp4-dual--single) | per-node ×1 (snoopy, **26.06**) | 0.55 | 262144 | sparky free + ~55 GiB on snoopy | sparky-free for dev |
| [`empty`](#empty) | no engines | — | — | full hardware | bare cluster |

### step-3.5-fp8
- **Model:** `Step-3.5-Flash-FP8` (~195 GiB total, ~97.5 GiB per shard); container 26.04.
- **Serves as:** `step-3.5-flash` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Why this tuning:** weights nearly fill the per-node budget. At `gmu 0.90`
  KV available is ~11.7 GiB — `max_model_len 32768` is the
  empirically-confirmed value (sliding_window=512 may allow more; not measured).
- **Workflow:** fully committed. Use other machines for dev that day.

### step-3.7-nvfp4
- **Model:** `Step-3.7-Flash-NVFP4` (~129 GiB total, ~64.5 GiB per shard); **pins container 26.06**
  (per-profile override — NVFP4/modelopt needs it).
- **Serves as:** `step-3.7-flash` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Status:** ⛔ **BLOCKED / parked** (`blocked: true` in the profile → hidden from the
  control-panel deploy UI; a deliberate CLI `make deploy PROFILE=step-3.7-nvfp4` still works to
  re-test). **NVFP4 loaded + ran on 26.06 with no hang** (2026-07-02) — the hard part works
  and per-profile pinning is validated. The remaining blocker is an upstream vLLM bug, not
  NVFP4/the container: Step-3.7's `Step3VLProcessor` crash-loops on startup (missing
  `_get_num_multimodal_tokens`). **Unblock when** vLLM ships the fix. See
  [`docs/upgrades/container-nvidia-vllm-26.06-py3.md`](upgrades/container-nvidia-vllm-26.06-py3.md)
  and [`docs/models/step-3.7-flash.md`](models/step-3.7-flash.md).
- **Workflow:** big-shared with ~30 GiB/node headroom (NVFP4 roughly halves the FP8 footprint).

### minimax-m2.7-awq
- **Model:** `MiniMax-M2.7-AWQ-4bit` (~122 GiB total, ~61 GiB per shard;
  compressed-tensors int4 MoE, custom modeling code with `--trust-remote-code`); container 26.04.
- **Serves as:** `minimax-m2` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Why this tuning:** `gmu 0.75` deliberately leaves **~30 GiB free per node**
  for dev/builds/distcc while still giving the engine ~28 GiB KV — room for
  1–2 concurrent 128k-token sequences (single/few-user feel).
- **A/B partner:** `minimax-m2.7-nvfp4` (same model, NVFP4 quant, on 26.06).
- **Workflow:** lending TP capacity while remaining usable for concurrent dev or
  build-farm tasks on either node.

### minimax-m2.7-nvfp4
- **Model:** `MiniMax-M2.7-NVFP4` (~140 GiB total, ~70 GiB per shard; modelopt NVFP4 MoE);
  **pins container 26.06**.
- **Serves as:** `minimax-m2` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Why this tuning:** NVFP4 weights are *bigger* than the AWQ (~70 vs ~61 GiB/node), so
  `gmu 0.80` → ~26 GiB KV/rank and ~24 GiB/node outside headroom. The NVFP4 A/B candidate
  for the MiniMax slot; once it proves out on 26.06 it can become the default MiniMax profile.
- **Workflow:** big-shared with ~24 GiB/node headroom.

### qwen3-coder-nvfp4-dual / -single
- **Model:** `Qwen3-Coder-Next-NVFP4` (80B-A3B linear-attention hybrid, ~45 GiB NVFP4;
  compressed-tensors — staged as the `RedHatAI/Qwen3-Coder-Next-NVFP4` build; the
  GB10-targeted `saricles/Qwen3-Coder-Next-NVFP4-GB10` is an alternative if needed);
  **pins container 26.06**. Coding-tuned agent.
- **`-dual`:** two independent engines — `qwen3-coder-sparky` (`sparky:8000`) and
  `qwen3-coder-snoopy` (`snoopy:8000`).
- **`-single`:** only `qwen3-coder-snoopy` — byte-identical to `-dual`'s snoopy engine, so
  switching `single ↔ dual` only adds/removes the sparky engine (no snoopy restart).
- **Why per-node, not TP=2:** weights fit one node with room to spare; with no NVLink between
  the Sparks, cross-node TP=2 for a 3B-active model is bandwidth/latency-bound and would only
  *cost* decode throughput. See the tuning doc for the full TP=2 cost analysis.
- **Why this tuning:** `gmu 0.55` → ~20 GiB KV (cheap here — hybrid attn), ~55 GiB/node free
  for dev/builds. **Fallback:** if compressed-tensors won't load on 26.06, use the official
  `Qwen/Qwen3-Coder-Next-FP8` (80 GiB) and rename the family to `-fp8`.

### qwen3.6-35b-nvfp4-dual / -single
- **Model:** `Qwen3.6-35B-A3B-NVFP4` (35B-A3B MoE, ~22 GiB NVFP4; **nvidia modelopt** —
  the proven-loads path on 26.06); **pins container 26.06**. Reasoning-generalist (also VL —
  run text-first, MTP off on first bring-up). Arch `Qwen3_5MoeForConditionalGeneration`,
  registry-confirmed on 26.06's vLLM 0.22.1 / transformers 5.6.0.
- **`-dual`:** two independent engines — `qwen3.6-35b-sparky` + `qwen3.6-35b-snoopy` (a
  master + worker pair for agentic use).
- **`-single`:** only `qwen3.6-35b-snoopy` (byte-identical to `-dual`'s snoopy engine).
- **Why per-node, not TP=2:** same as the coder family — fits one node, cross-node TP=2 is a
  throughput loss for a 3B-active model.
- **Why this tuning:** `gmu 0.55` → ~43 GiB KV (ample; cheap hybrid-attn KV), ~55 GiB/node
  free. Drop to ~0.45 for more dev headroom — KV is already overkill.

### empty
- **What it serves:** nothing. No vLLM or Ollama engines anywhere; only the
  always-on services (Caddy, control panel, Open WebUI, Prometheus, Grafana,
  exporters).
- **What it does on deploy:** prunes everything in the `vllm-*` / `ollama-*`
  namespaces; re-templates Open WebUI with empty `OPENAI_API_BASE_URLS` (UI
  loads clean, not pointing at dead endpoints); writes
  `{profile: empty, engines: []}` to `current-topology.json`.
- **Workflow:** working with cloud AI (Claude etc.), running the cluster as a
  build farm, or just freeing the hardware.

## Switching profiles

```sh
cd ansible && make deploy PROFILE=<name>     # apply
make check  PROFILE=<name>                   # dry-run (--check --diff) to preview
make teardown                                 # stop all engines (keeps front-end)
```

The deploy publishes the repo to `/opt/cluster/ansible`, runs
`ansible-playbook site.yml -e @profiles/<name>.yml`, and:

- the `vllm` and `ollama` roles **prune** any unit in the `vllm-*` /
  `ollama-*` namespace that the new profile doesn't declare (strict-namespace,
  never touches unmanaged units),
- Open WebUI re-templates its connection list to match the new engines,
- `current-topology.json` is written at the end so the control panel
  (`/admin`) reflects the new state.

For the **live** state: see `/admin`, or `cat /opt/cluster/current-topology.json`,
or `make status`.

## Adding a new profile

1. **Stage weights** in the inbox on sparky (from the repo root):
   ```sh
   make download REPO=<hf-repo> [DEST=<dir-name>]
   ```
   Runs `scripts/download.py` via `uv` (provisions `huggingface_hub` itself — no local
   `hf` install needed) and writes a flat copy into the inbox. The next deploy moves it
   into the canonical store and mirrors to every node.
2. **Copy** an existing profile that matches your shape:
   - big-shared TP=2 → start from `step-3.5-fp8.yml` or `minimax-m2.7-awq.yml`
   - per-node small → start from `qwen3-coder-nvfp4-dual.yml` (two engines) or
     `qwen3-coder-nvfp4-single.yml` (one)
3. **Pick `gpu_memory_utilization` and `max_model_len`** per
   [`profile-tuning.md`](profile-tuning.md) — decide your *outside-headroom*
   target first, give vLLM the rest.
4. **`make check PROFILE=<name>`** to dry-run, then **`make deploy PROFILE=<name>`**.

See [`serving-topology.md`](serving-topology.md) for the full schema (every
field an engine entry can take).
