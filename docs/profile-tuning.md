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

### Fully-committed (e.g. `step-3.5-flash-fp8`)
Model is so large that even at max gmu, outside headroom is ~5 GiB — just
enough for the service stack + OS. **No meaningful capacity left for user
work.** Honest constraint, not a problem: use other machines for dev that day.

### Big-shared with dev headroom (e.g. `minimax-m2.7-nvfp4` at gmu 0.80)
Big model TP'd across both nodes, but gmu deliberately set below max so each
node keeps **~30 GiB free**. Single/few-user serving still gets ample KV for
long context with some batching. You can dev on sparky concurrently, or run
snoopy as a distcc backend, etc.

### Small-and-dev-friendly (e.g. `qwen3-coder-next-nvfp4-single`, `qwen3.6-35b-a3b-nvfp4-mtp3-single` at gmu 0.55)
Small models that comfortably fit in well under half their node's memory. **~55 GiB
free per node** for user work; serving still gets plenty of KV for several
concurrent long-context sequences.

### Bare (`empty`)
No engines. Both nodes' resources are yours — use this when working purely with
cloud AI (Claude etc.) or for cluster-as-build-farm time.

## Per-model math (this cluster, today)

Numbers from measured deploys, 2026-05.

### Step-3.5-Flash-FP8 — `step-3.5-flash-fp8`, TP=2
| | value |
|---|---|
| Weights per shard | **~97.5 GiB** |
| CUDA graphs (measured) | ~2.2 GiB |
| At gmu 0.90: KV available | ~11.7 GiB |
| At gmu 0.90: outside headroom | ~5 GiB |
| **Workflow fit** | Fully-committed; no realistic dev-headroom variant |
| `max_model_len` | `32768` empirically confirmed; sliding_window=512 may allow more but hasn't been measured |

### MiniMax-M2.7-AWQ-4bit — `minimax-m2.7-awq`, TP=2 *(retired 2026-08-08 — kept for the math)*
| | value |
|---|---|
| Weights per shard | **~60.93 GiB** |
| CUDA graphs alone (measured) | ~0.65 GiB |
| KV per token | ~124 KiB (62 layers MoE, 8 kv heads) |
| At gmu 0.90: KV ~46 GiB, headroom ~5 GiB | Wasteful for our usage |
| **At gmu 0.75 (current)**: KV ~28 GiB, headroom **~30 GiB** | 128k = ~16 GiB/seq → 1–2 concurrent |
| At gmu 0.65: KV ~16 GiB, headroom ~42 GiB | 1 concurrent 128k sequence; more dev margin |

### Qwen3-Coder-Next-NVFP4 — `qwen3-coder-next-nvfp4-single`/`-dual`, TP=1 per node
| | value |
|---|---|
| Weights | **~45 GiB** (80B-A3B, compressed-tensors NVFP4) |
| Attention | hybrid `qwen3_next` — only 12/48 layers full attention → tiny KV |
| **At gmu 0.55 (current)**: budget ~66 GiB − ~45 weights → **~20 GiB KV**, headroom **~55 GiB** | KV is cheap; many concurrent 32–64k sessions |

### Qwen3.6-35B-A3B-NVFP4 — `qwen3.6-35b-a3b-nvfp4-mtp3-single`/`-dual`, TP=1 per node
| | value |
|---|---|
| Weights | **~22 GiB** (35B-A3B, nvidia modelopt NVFP4, FP8 KV baked in) |
| Attention | hybrid `qwen3_5_moe` — 10/40 full attention → cheap KV |
| **At gmu 0.55**: budget ~66 GiB − ~22 weights → **~43 GiB KV** (ample), headroom **~55 GiB** | drop to ~0.45 for more dev room |

**~~Why both are per-node (TP=1), never TP=2~~ — MEASURED WRONG, 2026-08-10.**

> The claim was: *"the two Sparks have no NVLink — cross-node TP=2 for a ~3B-active model
> is bandwidth/latency-bound and only costs decode throughput. TP=2 is reserved for models
> too big for one node."* It was reasoned, never measured. **The opposite is true:** TP=2
> is faster on every axis we measure, for models that fit on one node.

Paired TP=1/TP=2 profiles, same weights, same container, benched back to back
(ADR-0016 HTTP-native harness):

| | Qwen3-Coder-Next | Qwen3.6-35B-A3B |
|---|---|---|
| decode, TPOT | 26.5 → **18.0 ms** (1.47×) | 12.9 → **9.6 ms** (1.34×) |
| single-stream | 36.9 → **54.0** tok/s | 74.9 → **100.2** tok/s |
| throughput @16 | 249 → **351** tok/s | 371 → **540** tok/s |
| prefix_cache | 128 → **175** tok/s | 200 → **270** tok/s |
| KV capacity | 787k → **5.98M** tok | 4.18M → **16.3M** tok |

**Why the reasoning failed.** It was sound about the *interconnect* and aimed at the wrong
bottleneck. GB10 decode is **memory-bandwidth-bound**: every token reads the weights, and
sharding them across two nodes doubles the aggregate bandwidth doing that reading. The RoCE
all-reduce adds latency per layer, but for these hidden sizes it costs less than halving the
per-node weight traffic buys. "No NVLink" predicts a penalty only if the workload is
interconnect-bound, and decode here is not.

**Caveat on the KV column.** Those pairs differ in *two* variables — the TP degree **and**
`gpu_memory_utilization` (0.55 single vs 0.80 TP=2). Sharding frees the memory; raising gmu
is what claims it. A single-node profile at gmu 0.80 would hold roughly 1.9M KV tokens for
Qwen3-Coder, so TP=2's genuine context advantage is nearer **3×** than the 7.6× the table
implies. The speed rows are unaffected — gmu does not change generation rate.

**What this means for profile design:** the performance argument for `-single` profiles is
gone. What remains is *fleet occupancy* — TP=2 takes both nodes and leaves ~24 GiB of dev
headroom on sparky rather than the whole box. That is a real cost, and it is now the **only**
reason to run a small model on one node.

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
