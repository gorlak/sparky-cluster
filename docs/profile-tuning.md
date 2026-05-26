# Profile tuning — memory budgets, headroom, and workflow fit

How to choose `gpu_memory_utilization` and `max_model_len` for a profile *on
this cluster's hardware*. The numbers below are specific to our 2-node DGX
Spark setup (128 GiB unified memory per node, **~121 GiB usable**, ConnectX-7
NCCL). Companion to [`serving-topology.md`](serving-topology.md), which covers
the profile structure itself.

## The two headrooms

`gmu` looks like a single "how much memory does vLLM get" knob, but it controls
two **independent things**:

1. **Headroom *inside* vLLM's reservation** — KV cache / batching capacity. The
   slack vLLM has between (weights + CUDA graphs) and its gmu budget. Determines
   how many concurrent sequences the engine can hold and how long
   `max_model_len` can be. More helps **throughput under concurrency**; useless
   beyond what your real workload uses.

2. **Headroom *outside* vLLM's reservation** — system / user / dev memory. What
   the OS, other containers (Open WebUI, Prometheus/Grafana, exporters, control
   panel), and **whatever else you run on the box** (dev work, builds, distcc
   clients, ad-hoc experiments) get to use. On unified memory this is genuinely
   scarce; gmu directly trades it against KV.

**Treat `gmu` as a split, not as a safety margin.** Decide your *outside*
target first (real workflow needs), give vLLM the rest.

## The GB10 unified-memory accounting quirk

GB10 has **no separate VRAM** — the GPU and CPU share the host's 128 GiB pool.
That means:

- `nvidia-smi` reports GPU memory as **N/A** — there's no separate pool to
  report.
- `htop` and `free` **include** CUDA allocations (weights, graphs, KV
  reservation) in their "used" total.
- `ps` (RSS) and `docker stats --no-stream` **don't** include them, because
  those bytes are allocated through the CUDA driver, not via process page
  faults. A vLLM container's `MemUsage` will look tiny (~4 GiB) while the host
  shows 100+ GiB "used."

So when `free` says `used 120 GiB` with vLLM at gmu 0.90, the numbers are
correct *and* internally consistent — ~108 of those 120 belong to the vLLM CUDA
pool, not the visible process tree. The unit's `MemoryMax` (`124G`) is set
generously because cgroup *does* count CUDA allocations against the unit.

## Picking `gpu_memory_utilization`

Decision procedure:

1. **Pick your outside-headroom target.** What needs to run concurrently with
   the model? E.g. *"20 GiB for dev sessions"*, *"32 GiB for builds + distcc"*,
   *"0 GiB, fully committed"*.
2. **vLLM budget = `121 GiB − outside_headroom`**.
3. **Subtract weights shard and CUDA graphs** to find KV available.
4. **Check KV fits your `max_model_len`** with reasonable batching: aim for
   per-sequence KV × (2–5) so the engine can hold a few concurrent sequences.
5. **gmu = vLLM_budget / 121.**

If step 4 fails: lower `max_model_len`, raise `gmu` (eats into outside
headroom), or accept lower concurrent capacity.

## Workflow archetypes

### Fully-committed (e.g. `step`)
Model is so large that even at max gmu, outside headroom is ~5 GiB — just
enough for the service stack + OS. **No meaningful capacity left for user
work.** Honest constraint, not a problem: use other machines for dev that day.

### Big-shared with dev headroom (e.g. `minimax` at gmu 0.75)
Big model TP'd across both nodes, but gmu deliberately set below max so each
node keeps **~30 GiB free**. Single/few-user serving still gets ample KV for
long context with some batching. You can dev on sparky concurrently, or run
snoopy as a distcc backend, etc.

### Small-and-dev-friendly (e.g. `qwen-dual`, `qwen` at gmu 0.50)
Small models that comfortably fit in ~half their node's memory. **~60 GiB
free per node** for user work; serving still gets plenty of KV for several
concurrent long-context sequences.

### Bare (`empty`)
No engines. Both nodes' resources are yours — use this when working purely with
cloud AI (Claude etc.) or for cluster-as-build-farm time.

## Per-model math (this cluster, today)

Numbers from measured deploys, 2026-05.

### Step-3.5-Flash-FP8 — `step`, TP=2
| | value |
|---|---|
| Weights per shard | **~97.5 GiB** |
| CUDA graphs (measured) | ~2.2 GiB |
| At gmu 0.90: KV available | ~11.7 GiB |
| At gmu 0.90: outside headroom | ~5 GiB |
| **Workflow fit** | Fully-committed; no realistic dev-headroom variant |
| `max_model_len` | `32768` empirically confirmed; sliding_window=512 may allow more but hasn't been measured |

### MiniMax-M2.7-AWQ-4bit — `minimax`, TP=2
| | value |
|---|---|
| Weights per shard | **~60.93 GiB** |
| CUDA graphs alone (measured) | ~0.65 GiB |
| KV per token | ~124 KiB (62 layers MoE, 8 kv heads) |
| At gmu 0.90: KV ~46 GiB, headroom ~5 GiB | Wasteful for our usage |
| **At gmu 0.75 (current)**: KV ~28 GiB, headroom **~30 GiB** | 128k = ~16 GiB/seq → 1–2 concurrent |
| At gmu 0.65: KV ~16 GiB, headroom ~42 GiB | 1 concurrent 128k sequence; more dev margin |

### Qwen3-30B-A3B-Instruct-2507-FP8 — `qwen`, `qwen-dual`, TP=1
| | value |
|---|---|
| Weights | **~30 GiB** |
| CUDA graphs (estimate) | ~1 GiB |
| KV per token | ~48 KiB (48 layers MoE, 4 kv heads) |
| At gmu 0.85: KV ~70 GiB, headroom ~17 GiB | Wasteful |
| **At gmu 0.50 (current)**: KV ~29 GiB, headroom **~60 GiB** | 128k = ~6 GiB/seq → 4–5 concurrent |

### Co-residency note
Two vLLM engines on the same node interact badly even when the gmu math adds.
See operational-gotcha #8: rank-asymmetric CUDA graphs under co-residency. The
profile strategy (`docs/serving-topology.md`) deliberately avoids this — one
big-TP engine *or* per-node small engines, not mixed.

## Updating these numbers

After any deploy, re-read vLLM's startup log lines:
```
[gpu_model_runner.py] Model loading took X GiB memory
[gpu_model_runner.py] Estimated CUDA graph memory: Y GiB total
[gpu_worker.py]       Available KV cache memory: Z GiB
```
Plug `X + Y + Z` back into the math here; verify the per-rank min `Z` covers
your chosen `max_model_len` at the concurrency you actually need. If vLLM
refuses to start it'll emit `Based on the available memory, the estimated
maximum model length is N` — **believe that number over the back-of-envelope
budget**.

## See also

- [`docs/serving-topology.md`](serving-topology.md) — profile structure and the
  `serving_topology` schema.
- README *"Adding New Models / Profiles"* — workflow for staging new weights.
- Operational-gotchas memory #8 — rank-asymmetric CUDA-graph memory under
  co-residency.
