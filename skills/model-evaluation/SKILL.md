---
name: model-evaluation
description: Evaluating models for this cluster — the pre-deployment fit checklist for one model (memory math, config.json, vllm serve flags), plus the fleet-wide sourcing sweep that reviews the deployed models for worthwhile upgrades. Use before deploying a new model, when estimating fit, or when asked to "assess upgrading our models".
---

## Model Evaluation Checklist

Work through these in order before writing any service files or configs.
Failures here are cheap. Failures after a full deploy are expensive.

### 1. Verify actual size first — before downloading if you can

Size the repo straight from the Hub, no download needed (see [[model-discovery]]):

```bash
hf models ls <org/repo> --tree -h      # per-file sizes; sum the *.safetensors
```

Once it's staged locally, confirm with `du`:

```bash
du -sh /opt/cluster/model-cache/<model>/
```

**Disk size ≈ VRAM footprint** for quantized models. Do not trust model cards
or "active parameter" estimates — get the real byte count before any memory math.

Lesson: Step-3.5-Flash-FP8 was estimated at ~50 GiB/node based on "active
parameter" count. Actual size was 195 GiB / ~97.5 GiB per node. The model
was already downloaded before any configs were written — `du` would have
caught this immediately.

### 2. Read config.json before writing flags

```bash
cat /opt/vllm/models/<model>/config.json | python3 -m json.tool | grep -A5 quantization
```

Check for:
- `quantization_config` — if present, vLLM auto-detects it. **Never add
  `--quantization <dtype>` for a checkpoint that already declares its own
  quantization.** Doing so double-quantizes the weights and produces garbage output.
- `num_experts` / `num_experts_per_tok` — MoE models load ALL experts into
  VRAM regardless of how many are active per token. Use total param count
  for memory math, not active param count.
- `architectures` — confirms the model type and what vLLM flags apply.

### 3. Verify runtime dependencies exist in the container

Before writing any unit files, confirm the container has what you need:

```bash
sudo docker run --rm nvcr.io/nvidia/vllm:26.04-py3 python3 -c "import vllm; print(vllm.__version__)"
# Check for specific features (e.g., Ray):
sudo docker run --rm nvcr.io/nvidia/vllm:26.04-py3 python3 -c "import ray" 2>&1
```

Lesson: vLLM 0.19 dropped Ray entirely. A 30-second container check would
have caught this before writing Ray unit files.

### 4. Memory math — `gpu_memory_utilization` is a split, not a baseline

`gmu` is **per-profile** — a deliberate split between vLLM and what's left for
OS + your dev/build work. Not a universal "safety margin to set as high as
possible." Procedure:

1. Pick your outside-headroom target — what needs to run concurrently with the
   model? (e.g. *30 GiB for dev sessions*, *0 GiB for fully-committed*.)
2. **vLLM budget = `121 GiB − outside_headroom`**.
3. Subtract the weights shard and CUDA graphs to find KV available.
4. Verify KV fits your chosen `max_model_len` with reasonable batching headroom.
5. **`gmu = vLLM_budget / 121`**.

See [`docs/profile-tuning.md`](../../docs/profile-tuning.md) for the math, the
GB10 unified-memory accounting quirk, workflow archetypes, and the per-model
tunings already in use.

Footguns:
- There is no global default `gmu` — each profile declares it per engine.
- vLLM refuses to start if KV doesn't fit max_model_len; trust its
  `estimated maximum model length is N` diagnostic over back-of-envelope math.
- **Co-residency** of vLLM engines on the same node distorts KV asymmetrically
  (rank-asymmetric CUDA graphs under MoE+TP). See operational-gotcha #8 — the
  current strategy avoids co-residency entirely.

### 5. Start with the minimal flag set

Only add flags you have a reason for. For each flag ask: does this model
require it, or am I copying it from a different model's recipe?

Known footguns on this cluster:
- `--quantization fp8` on an FP8 checkpoint → double-quantization, garbage output
- `--kv-cache-dtype fp8` + `--enable-prefix-caching` → multi-turn corruption (under investigation)
- `--gpu-memory-utilization 0.70` on a model near the memory ceiling → negative KV cache

### 6. Change one flag at a time when debugging

When output is wrong or the service fails to start, change exactly one
variable per restart. With a 10+ minute restart cycle, shotgun changes make
root cause analysis nearly impossible.

---

## Fleet sourcing sweep

The checklist above evaluates *one* model you're about to deploy. This sweep is the
periodic, fleet-wide review that decides whether the models we already run are worth
upgrading — "keeping the cluster's models current." Use it when asked to **"assess
upgrading our models"**, after a notable release, or when a blocking dependency clears
(e.g. a container upgrade lands — check `docs/upgrades/`).

**Assessment only — never change a profile or deploy as part of the sweep.**

1. **Enumerate the fleet.** List each Ansible profile and the model it serves
   (`ansible/profiles/*.yml` → `model:`), grouped by shape: big-shared TP=2 (`step`,
   `minimax`) vs per-node single (`qwen`, `qwen-dual`). Note the current quant and its
   fact sheet under `docs/models/`.

2. **Find upgrades for each served model** ([[model-discovery]]): a newer generation of the
   family (Step-3.5 → 3.7; MiniMax-M2.7 → M3; a newer Qwen3-30B) or a better/newer quant
   (NVFP4 availability; official vs community; calibrated vs RTN). Record release date,
   HF repo, quant method.

3. **Assess each candidate's fit** with the checklist above — size it
   (`hf models ls <repo> --tree -h`, no download), do the per-node memory math at the
   profile's TP, and cross-check container/tooling needs against the blockers in
   `docs/upgrades/` (e.g. the 26.06/NVFP4 gate) and README "Pending investigation".

4. **Write it up** ([[documentation]]): a new candidate → fact sheet
   `docs/models/<model>.md`; a viable profile upgrade → tracker
   `docs/upgrades/profile-<profile>-<target>.md` (delta + implications + dependencies +
   completion criteria + recommendation). A candidate gated on a blocked dependency is
   **blocked**, not recommended — link the tracker that must clear first.

5. **Summarize** with a prioritized table, one row per profile:
   `profile | current model | best candidate | memory/quality delta | blockers |
   recommendation` (upgrade now / interim / hold / blocked-until-X).

Honor the **"Do not use"** list (README "Adding New Models / Profiles"). Committing the
produced docs: see [[development]] (stage, don't commit).
