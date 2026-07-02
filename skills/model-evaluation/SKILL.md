---
name: model-evaluation
description: Pre-deployment checklist for evaluating a new model for this cluster. Use when considering a new model, estimating memory fit, or writing vllm serve flags for the first time.
---

## Model Evaluation Checklist

Work through these in order before writing any service files or configs.
Failures here are cheap. Failures after a full deploy are expensive.

### 1. Verify actual disk size first

```bash
du -sh /opt/cluster/model-cache/<model>/
```

**Disk size ≈ VRAM footprint** for quantized models. Do not trust model cards
or pre-download estimates — verify with `du` before any memory math.

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
