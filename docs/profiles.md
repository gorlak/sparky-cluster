# Profiles — the deployable cluster configurations

A *profile* is a YAML file under `ansible/profiles/<name>.yml` that fully
describes what the cluster serves: which model(s), on which node(s), at what
TP and `gmu`, with what context length, plus which front-end services run.
Deploy with `make deploy PROFILE=<name>` from `ansible/`.

This doc is the **catalog** of profiles that exist today. Companion docs:

- [`profile-tuning.md`](profile-tuning.md) — *why* the `gmu` and `max_model_len`
  values below were picked, with the per-model memory math and the GB10
  unified-memory accounting quirk.
- [`serving-topology.md`](serving-topology.md) — the `serving_topology` schema
  and how each engine kind (`vllm`, `ollama`) projects into the various roles.

## Catalog

| Profile | Shape | gmu | `max_model_len` | Outside headroom (per node) | Workflow archetype |
|---|---|---|---|---|---|
| [`step`](#step) | TP=2 big-shared | 0.90 | 32768 | ~5 GiB | fully-committed |
| [`minimax`](#minimax) | TP=2 big-shared | 0.75 | 131072 | ~30 GiB | big-shared with dev headroom |
| [`qwen-dual`](#qwen-dual) | per-node small × 2 | 0.50 | 131072 | ~60 GiB | small + dev-friendly |
| [`qwen`](#qwen) | per-node small × 1 (snoopy) | 0.50 | 131072 | sparky free + ~60 GiB on snoopy | sparky-free for dev |
| [`empty`](#empty) | no engines | — | — | full hardware | bare cluster |

### step
- **Model:** `Step-3.5-Flash-FP8` (~195 GiB total, ~97.5 GiB per shard)
- **Serves as:** `step-3.5-flash` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Why this tuning:** weights nearly fill the per-node budget. At `gmu 0.90`
  KV available is ~11.7 GiB — `max_model_len 32768` is the
  empirically-confirmed value (sliding_window=512 may allow more; not measured).
- **Workflow:** fully committed. Use other machines for dev that day.

### minimax
- **Model:** `MiniMax-M2.7-AWQ-4bit` (~122 GiB total, ~61 GiB per shard;
  compressed-tensors int4 MoE, custom modeling code with `--trust-remote-code`)
- **Serves as:** `minimax-m2` at `sparky:8000` (TP=2 across sparky + snoopy)
- **Why this tuning:** `gmu 0.75` deliberately leaves **~30 GiB free per node**
  for dev/builds/distcc while still giving the engine ~28 GiB KV — room for
  1–2 concurrent 128k-token sequences (single/few-user feel).
- **Workflow:** sparky state b — lending TP capacity while remaining usable
  for concurrent dev or build-farm tasks on either node.

### qwen-dual
- **Model:** `Qwen3-30B-A3B-Instruct-2507-FP8` (~30 GiB, block-FP8 MoE), two
  independent engines:
  - `qwen-sparky` → `qwen3-30b-sparky` at `sparky:8000`
  - `qwen-snoopy` → `qwen3-30b-snoopy` at `snoopy:8000`
- **Why this tuning:** each engine alone on its node at `gmu 0.50` → ~60 GiB
  free per node + ~29 GiB KV per engine (4–5 concurrent 128k sequences).
- **Workflow:** two independent small-model endpoints; load-balance, run
  parallel experiments, or serve + dev on both nodes simultaneously.

### qwen
- **Model:** same engine as `qwen-dual`'s `qwen-snoopy`, **byte-identical**
  serving entry. Switching `qwen ↔ qwen-dual` only adds/removes the sparky
  engine — `qwen-snoopy` keeps serving without a restart.
- **Workflow:** sparky state c — sparky free for dev; snoopy serves Qwen3-30B.

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

1. **Stage weights** in the inbox on sparky:
   ```sh
   hf download <repo> --local-dir /opt/cluster/model-cache/<MODEL>
   ```
   The next deploy moves them into the canonical store and mirrors to every node.
2. **Copy** an existing profile that matches your shape:
   - big-shared TP → start from `step.yml` or `minimax.yml`
   - per-node small → start from `qwen-dual.yml` (two engines) or `qwen.yml` (one)
3. **Pick `gpu_memory_utilization` and `max_model_len`** per
   [`profile-tuning.md`](profile-tuning.md) — decide your *outside-headroom*
   target first, give vLLM the rest.
4. **`make check PROFILE=<name>`** to dry-run, then **`make deploy PROFILE=<name>`**.

See [`serving-topology.md`](serving-topology.md) for the full schema (every
field an engine entry can take).
