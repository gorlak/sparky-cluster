---
name: model-evaluation
description: Pre-deployment checklist for evaluating a new model for this cluster. Use when considering a new model, estimating memory fit, or writing vllm serve flags for the first time.
---

## Model Evaluation Checklist

Work through these in order before writing any service files or configs.
Failures here are cheap. Failures after a full deploy are expensive.

### 1. Verify actual disk size first

```bash
du -sh ~/Projects/DGX-Spark-Setup/model-cache/<model>/
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

### 4. Memory math — use 0.90 utilization as the baseline

```
Budget per node = 0.90 × usable_VRAM
               = 0.90 × 121 GiB = 108.9 GiB on GB10

Headroom = Budget − weights_per_node
         = 108.9 − (disk_size / num_nodes)
```

If headroom < ~8 GiB, KV cache will be severely constrained.
If headroom < 0, the model does not fit — do not proceed.

`--gpu-memory-utilization 0.70` (old default) is too low for large models.
Use 0.90 unless there is a specific reason not to.

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
