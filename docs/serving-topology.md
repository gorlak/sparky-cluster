# Serving topology — profile-driven, multi-model cluster (design)

> **Companion doc:** [`profile-tuning.md`](profile-tuning.md) — how to pick
> `gpu_memory_utilization` and `max_model_len` for a given workflow, with the
> per-model math and the GB10 unified-memory accounting quirk.

**Status:** T1–T5 built and deployed. The full profile family
([`profiles.md`](profiles.md)) lives on this design: `step` and `minimax` are
big-shared TP=2; `qwen-dual` and `qwen` are per-node single-engine; `empty` takes
the cluster down to bare. Co-residency of vLLM engines on one node was attempted
(retired `multi` profile) and abandoned — see decisions log + operational
gotcha #8 (rank-asymmetric CUDA graphs under co-residency). The Ollama role is
GPU-verified on GB10 but unused by any current profile. Talkie is deferred to
a custom-runtime follow-on (see TODO.md).

## Motivation (the pre-T1 problem this design solved)

Before T1 the cluster served exactly **one** vLLM engine with node identity baked in:

- `inventory.yml` + `group_vars/{head,worker}.yml` hardwire `head`=sparky (rank 0,
  API on `:8000`) and `worker`=snoopy (rank 1, headless). The `vllm` role
  templates **one** unit per host off `node_role`.
- Four other services hardcode that single engine: `prometheus.yml.j2` scrapes
  `groups['head'][0]:8000`; `teardown.yml` stops the two fixed unit names;
  `open-webui` points at a single `localhost:8000`; the control panel
  health-checks `localhost:8000` and the fixed unit names.

So we cannot express "two engines on snoopy," "serve on snoopy instead of
sparky," or "models move between nodes per profile" — and even if we could, every
model-dependent service would drift out of sync on deploy. A migration has to
reconfigure **all** of them in lockstep, or the dashboard, metrics, proxy, and
control panel point at the wrong place.

## Goals

1. A profile declares the **serving topology once**, as structured data.
2. Every model-dependent service is a **projection** of that one declaration, so
   `make deploy PROFILE=X` reshapes serving *and* reconfigures its dependents in
   the same run — they stay in sync by construction.
3. Support **N engines**, each spanning one or more nodes, with more than one
   engine co-resident on a node (hand-partitioned GPU memory).
4. Node identity is **inventory-only** (closes the `TODO.md` item); "head/worker"
   becomes a per-engine, computed property (rank 0 vs rank > 0), not a host fact.
5. Switching profiles **prunes** engines a prior profile created.

## Non-goals

- Runtime service discovery (Consul/registries). For a 2-node homelab,
  declarative-from-profile rendered by Ansible is simpler and sufficient.
- Autoscaling, cross-node weight auto-distribution (weights stay pre-staged),
  GPU MIG partitioning (GB10 unified memory — N/A).

## The core abstraction: `serving_topology`

A profile carries a list of **engines**. Two kinds: `vllm` (tensor-parallel
across 1+ nodes, one OpenAI API) and `ollama` (standalone, single-node, many
models — one server per engine; to run Ollama on both nodes, declare two engines).

```yaml
serving_topology:
  # vLLM, tensor-parallel across both nodes. nodes[0] is rank 0 / the API host.
  - name: minimax
    kind: vllm
    nodes: [sparky, snoopy]      # order = ranks; nodes[0] exposes the API
    port: 8000
    model: MiniMax-M2.7-AWQ-4bit # dir under /opt/vllm/models
    served_as: minimax-m2
    tensor_parallel_size: 2
    gpu_memory_utilization: 0.55 # co-resident engines must sum < ~0.95/node
    max_model_len: 32768
    head_extra_args: [--enable-chunked-prefill, --enable-auto-tool-choice]
    worker_extra_args: [--enable-chunked-prefill]

  # vLLM, single-node on snoopy, second engine co-resident with minimax's shard.
  - name: qwen30
    kind: vllm
    nodes: [snoopy]
    port: 8001                   # unique per node across co-resident engines
    model: Qwen3-30B-A3B-Instruct-2507-FP8
    served_as: qwen3-30b
    tensor_parallel_size: 1
    gpu_memory_utilization: 0.33
    max_model_len: 32768

  # Ollama, persistent on sparky; hosts one or more GGUF models.
  - name: fun
    kind: ollama
    nodes: [sparky]
    port: 11434
    models: [talkie-1930-13b-it]
```

Derived facts (not authored): an engine's **rank** for a node = its index in
`nodes`; the **API host** = `nodes[0]`; **master_addr** = `nodes[0]`'s ConnectX
IP (today a global constant — becomes per-engine); unit/container name =
`vllm-<name>` / `ollama-<name>`.

## How each service projects from the spec

| Service | Projection |
|---|---|
| **vllm role** | For each `kind: vllm` engine, for each node in `nodes`: template a `vllm-<name>.service` (rank = list index; API+`served_as` only on rank 0; `--headless` on ranks > 0). A host loops over every engine that lists it — so one host can run several units on distinct ports. |
| **ollama role** | For each `kind: ollama` engine (single-node), on its node: prune stale `ollama-*` units, bring the `ollama-<name>` container up (persistent), wait for its API, and `ollama pull` each model (idempotent). |
| **open-webui** | `OPENAI_API_BASE_URLS` = each vllm engine's `api_host_ip:port/v1`; `OPENAI_API_KEYS` matching; `OLLAMA_BASE_URLS` = each ollama engine's `node_ip:port`; `ENABLE_OLLAMA_API=true` if any. (PersistentConfig — see below.) |
| **prometheus** | vLLM scrape targets = each vllm engine's `api_host_ip:port`. Prefer `file_sd` (Ansible writes a targets file) so adding/removing engines reloads without a Prometheus restart. Node/GPU exporter jobs are unchanged (per-host, topology-agnostic). |
| **grafana** | Dashboard panels group by the `model_name` label vLLM already emits, so multiple engines render as separate series instead of aggregating. A removed model just leaves a gap (graceful). |
| **control panel** | Reads the deployed state file (below): health-check each engine's API + each unit; P3 actions act per-engine ("restart minimax") or all. Replaces the hardcoded `localhost:8000` + fixed unit names. |
| **teardown** | Stop/disable/rm every managed engine unit + Ollama. A deploy **prunes** stale units (below). |
| **caddy** | Unaffected unless we later expose a model API route (`api.`); the landing page is hostname-stable. |

## Mechanisms

**State file.** A deploy writes `/opt/cluster/current-topology.json` (the resolved
`serving_topology` + `profile_name` + timestamp). It's the runtime source of truth
for the control panel (status + "which profile is live?" — also closes that open
question in `control-interface.md`).

**Pruning (the migration safety piece).** Managed units are namespaced
`vllm-<name>` / `ollama-<name>`. After bringing up the desired set, each host
enumerates its `vllm-*` / `ollama-*` units and stops+disables+removes any **not**
in the desired set. This makes a profile switch converge to exactly the declared
topology without orphaning a prior profile's engines — and only ever touches
units in our namespace.

**Deploy ordering & the transient.** vLLM workers already retry until their API
host's rendezvous is reachable (`Restart=on-failure`), so we start all engine
units, then wait on each API host's `:port/v1/models`. Dependents (Open WebUI,
Prometheus, control panel) are reconfigured **after** engines, preserving today's
ordering. During a migration an endpoint is briefly down for its load window
(10–20 min for big models); Open WebUI shows that model unavailable, then
self-heals on refetch. Benign.

**Open WebUI PersistentConfig.** `OPENAI_API_BASE_URLS` / `OLLAMA_BASE_URLS` are
PersistentConfig: env seeds them once on a fresh data volume, then the DB wins and
env is ignored on later starts (same gotcha as `webui_enable_signup`). To let each
deploy re-assert connections, either set `ENABLE_PERSISTENT_CONFIG=false` (env
authoritative every start; trade-off: loses UI-side persistence of all settings —
acceptable for config-as-code) **or** push the connection list via Open WebUI's
admin REST API as a deploy task (surgical; keeps persistence for other settings).
Leaning toward `false` for simplicity; revisit if UI-managed settings matter.

**Memory partitioning.** `gpu_memory_utilization` is per-engine; vLLM engines are
blind to each other (and to Ollama) on a shared GPU and grab their fraction of
*total* memory up front. Co-resident engines on a node must have fractions summing
< ~0.95, with weights + KV verified against the per-node budget (0.90 × 121 =
108.9 GiB). This is a profile-authoring responsibility — see the `model-evaluation`
skill and run a memory-profiled bring-up before committing a dense profile.

## Phases (each shippable; step keeps working throughout)

1. **T1 — engine spec + `vllm` role refactor.** Define the schema; make the role
   template N instanced units from `serving_topology`; per-engine `master_addr`;
   node identity inventory-only. Convert `step.yml` to the new schema and
   prove `make check` shows no functional change. Add pruning. (Closes the
   node-identity `TODO.md` item.)
2. **T2 — metrics topology-aware.** Prometheus targets from the spec (`file_sd`);
   Grafana dashboard group-by `model_name`.
3. **T3 — Open WebUI from the spec.** Plural URL vars + PersistentConfig solution.
4. **T4 — Ollama role.** Persistent on listed nodes; models pulled from the spec;
   wired into Open WebUI + (best-effort) metrics.
5. **T5 — state file + control panel + teardown.** Write `current-topology.json`;
   control panel reads it for status and per-engine P3 actions; teardown prunes.
6. **Then (done differently):** authored a profile *family* instead of a single
   multi-tenant config — `step`, `minimax`, `qwen-dual`, `qwen`, `empty` (see
   [`profiles.md`](profiles.md)). The original `multi` (MiniMax TP=2 + Qwen30
   single-node co-resident on snoopy + talkie via Ollama) was attempted, hit a
   rank-asymmetric CUDA-graph KV squeeze under co-residency, and was retired in
   favor of "TP for big, per-node for small, **no co-residency**" — see
   operational-gotchas #8 and `profile-tuning.md`. Talkie deferred to its own
   custom-runtime follow-on (TODO.md).

## Open questions

- **Port convention** — explicit `port:` per engine (chosen here) vs auto-assign.
  Explicit is safer; document the in-use set (8000/8001 vLLM, 11434 Ollama, plus
  8080/3000/9090/9100/9835/8088 already taken).
- **Ollama metrics (resolved 2026-05-25)** — Ollama exposes **no** Prometheus
  `/metrics` (verified on GB10). GPU utilization is covered by the GPU exporter;
  loaded models via `/api/ps`. A sidecar exporter is a later option if per-model
  throughput visibility is wanted.
- **Pruning blast radius** — keep the `vllm-*`/`ollama-*` namespace strict so the
  enumerate-and-remove step can never touch an unmanaged unit.

## Decisions log

- **2026-05-25** — Adopt a single declarative `serving_topology` (list of engines)
  in the profile as the source of truth; every model-dependent service is a
  projection of it. Declarative-from-profile over runtime service discovery.
  Node identity becomes inventory-only; head/worker is a per-engine computed rank.
  Pruning via a strict `vllm-*`/`ollama-*` namespace. A `current-topology.json`
  state file serves runtime consumers and "which profile is live?".
- **2026-05-25 (T3)** — Open WebUI connections are generated from `serving_topology`
  (plural `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`, `OLLAMA_BASE_URLS`, and
  `ENABLE_OPENAI_API`/`ENABLE_OLLAMA_API` toggled by whether each engine kind is
  present). Chose `ENABLE_PERSISTENT_CONFIG=false` (env authoritative on every
  start) over the REST-API push, so each deploy re-asserts connections. Trade-off
  — Admin Panel becomes read-only for config; drive admin settings via their env
  vars instead — documented in README "Known Shortcomings".
- **2026-05-25 (T4)** — Added the `ollama` role, mirroring the vllm role's
  prune-and-bring-up pattern (strict `ollama-*` namespace). An Ollama engine is
  **single-node** (no multinode/TP): one server = one Open WebUI URL, so "Ollama
  on both nodes" is two engine entries. Models pulled from each engine's `models:`
  list (idempotent). GPU offload verified on GB10/sm_121 (CUDA v13, 100% offload,
  co-resident with vLLM). `OLLAMA_CONTEXT_LENGTH` capped because Ollama's
  vram-based default is sized to TOTAL unified memory. No Prometheus metrics.
- **2026-05-25 (T5)** — Added `current-topology.json` (written by the new
  `topology-state` role at the end of a deploy; cleared by teardown) as the single
  record of "which profile is live + its engines, with each engine's unit and API
  URL". The control panel reads it for topology-driven status (per-engine, per-node
  health + API check) and per-engine restart buttons (worker-first, API node last),
  with a unit-discovery fallback when the file is absent (e.g. mid-deploy).
