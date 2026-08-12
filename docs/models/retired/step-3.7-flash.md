> ⚰️ **RETIRED 2026-08-12** — broken generation paths — `<think>` and images both emit token soup.
> The **verdict** and its *reconsider-when* live in [`../tombstones.md`](../tombstones.md), which owns them; nothing here restates one.
>
> This sheet is kept for the **engineering**: the memory math, the footprints, the
> quant findings, the workarounds. Its status and "next steps" sections are frozen
> in place and are **history, not a plan** — read them as what was believed then.

# Step-3.7-Flash on 2× DGX Spark — Status Tracker

**Last updated:** 2026-07-02
**Hardware:** sparky + snoopy — GB10 Blackwell (SM 12.1), 128 GiB unified memory each, ConnectX-7 200Gbit RoCE
**Installed quant:** None — not yet downloaded
**Target quant:** NVFP4 (`stepfun-ai/Step-3.7-Flash-NVFP4`, ~129 GiB) — preferred; FP8 available as fallback (~213 GiB, very tight)

---

## Model Overview

- **Developer:** StepFun AI
- **Architecture:** Sparse Mixture-of-Experts (MoE), vision-language model
- **Total parameters:** ~198B (196B language backbone + 1.8B vision encoder)
- **Active parameters per token:** ~11B
- **Context window:** 256K tokens
- **Reasoning levels:** Low / Medium / High (selectable per request)
- **Speculative decoding:** MTP-3 (Multi-Token Prediction, 3 draft tokens)
- **Vision:** Native image understanding via 1.8B ViT encoder
- **HuggingFace (BF16):** https://huggingface.co/stepfun-ai/Step-3.7-Flash
- **HuggingFace (FP8):** https://huggingface.co/stepfun-ai/Step-3.7-Flash-FP8
- **HuggingFace (NVFP4):** https://huggingface.co/stepfun-ai/Step-3.7-Flash-NVFP4
- **HuggingFace (GGUF):** https://huggingface.co/stepfun-ai/Step-3.7-Flash-GGUF

---

## Quantization Formats & Footprint

| Format | Disk | Per node at TP=2 | Fit on this cluster |
|---|---|---|---|
| BF16 | ~400 GiB | ~200 GiB | ❌ Does not fit |
| **FP8** | **~213 GiB** | **~106.5 GiB** | ⚠️ Fits, but fully-committed+ (tighter than 3.5) |
| **NVFP4** | **~129 GiB** | **~64.5 GiB** | ✅ Fits with real headroom |
| GGUF Q4_K_S | ~111.5 GiB | ~55.75 GiB | ✅ Fits, but GGUF serving not currently in use |

**Context:** Step-3.5-Flash-FP8 was 195 GiB / 97.5 GiB per node — already the tightest fit
on this cluster (gmu=0.90, ~11 GiB KV headroom). Step-3.7-Flash-FP8 at 213 GiB / 106.5 GiB
per node is meaningfully larger, primarily due to the added 1.8B vision encoder (~4 GiB in
FP8) and a larger language backbone. The NVFP4 quant is the right format for 3.7 on this
hardware.

---

## FP8 Analysis

### Memory fit (TP=2)

| | Per node |
|---|---|
| Model weights (FP8, TP=2) | ~106.5 GiB |
| At gmu=0.90 (108.9 GiB budget): KV headroom | **~2.4 GiB** |
| At gmu=0.95 (114.95 GiB budget): KV headroom | **~8.5 GiB** |
| At gmu=0.95: outside headroom | **~6 GiB** |

gmu=0.90 leaves ~2.4 GiB after weights — less than typical CUDA graph overhead alone.
vLLM would likely refuse to start or report a very small estimated maximum model length.
gmu=0.95 gives ~8.5 GiB for KV + CUDA graphs, which is marginal but may work with a
conservative `max_model_len` (e.g., 16384–32768).

**This is a fully-committed+ profile:** even less outside headroom than Step-3.5-Flash
(~6 GiB vs ~5 GiB). Both nodes are effectively unavailable for any other work while
serving. The upside over 3.5 is purely model quality — no memory or headroom advantage.

### FP8 tooling status

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ✅ Should work | Same code path as Step-3.5-Flash-FP8; auto-detects FP8 from config.json |
| **Docker image** | ✅ 26.04-py3 | No new image required for FP8 (same as 3.5) |
| **TP=2** | ✅ Expected | Official recipe uses TP=8 (4×H200), but MoE expert sharding works at TP=2 |
| **`--kv-cache-dtype fp8`** | ⚠️ Blocked | Same multi-turn corruption issue as 3.5; avoid until resolved |
| **`--enable-prefix-caching`** | ⚠️ Blocked | Same as above |

### FP8 serve flags (draft — not yet tested)

Do **not** pass `--quantization fp8` — the checkpoint declares its own quantization in
`config.json`; adding the flag causes double-quantization and garbage output.

```
vllm serve /models/Step-3.7-Flash-FP8 \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name step-3.7-flash \
    --tensor-parallel-size 2 \
    --distributed-executor-backend mp \
    --nnodes 2 \
    --node-rank 0 \
    --master-addr 10.0.200.12 \
    --trust-remote-code \
    --max-model-len 16384 \
    --gpu-memory-utilization 0.95 \
    --enable-chunked-prefill \
    --enable-auto-tool-choice \
    --tool-call-parser step3p5 \
    --enable-expert-parallel
```

`max-model-len 16384` is conservative given the tight budget. Verify vLLM's
`estimated maximum model length` diagnostic at startup before raising it.

### FP8 assessment

Viable but not compelling. Tighter memory fit than Step-3.5-Flash with no headroom
advantage, and model quality improvements may not justify the operational risk of running
at gmu=0.95. The NVFP4 quant is the better deployment target for 3.7.

---

## NVFP4 Analysis

### Memory fit (TP=2)

| | Per node |
|---|---|
| Main weight shard (NVFP4, TP=2) | ~62.2 GiB |
| MTP draft layer (bf16, per node) | ~4.86 GiB |
| CUDA graphs (estimate) | ~2 GiB |
| **Total vLLM footprint** | **~69 GiB** |

| gmu | vLLM budget | KV available | Outside headroom |
|---|---|---|---|
| 0.90 | 108.9 GiB | ~40 GiB | ~12 GiB |
| 0.85 | 102.9 GiB | ~34 GiB | ~18 GiB |
| **0.75** | **90.8 GiB** | **~22 GiB** | **~30 GiB** |

This is a **big-shared-with-headroom** profile: fits comfortably, 30 GiB free per node
at gmu=0.75. Compare to Step-3.5-Flash-FP8 which was fully-committed at 97.5 GiB/node
with ~11 GiB KV and ~5 GiB outside headroom.

The NVFP4 format cuts the per-node footprint nearly in half vs FP8 (62 GiB vs 106.5 GiB),
freeing enough memory to run the model *and* maintain a comfortable dev margin on both nodes.

### NVFP4 tooling status

| Component | Status | Notes |
|---|---|---|
| **vLLM** | ⚠️ Container bump required | b12x SM121 FP4 kernels merged to main 2026-05-20 (post 26.04 image) |
| **Docker image** | ⚠️ Needs 26.05+ | 26.04 (April 2026) predates the SM121 FP4 kernel merge |
| **`--quantization modelopt`** | ⚠️ Verify in new image | Different code path from FP8 auto-detect; requires ModelOpt support |
| **`--kv-cache-dtype fp8`** | 🔴 **Blocker** | Mandatory for NVFP4; same multi-turn corruption issue as 3.5 |
| **`--enable-expert-parallel`** | ✅ Recommended | Tested with NVFP4 in community deployments |
| **TP=2** | ✅ Confirmed | Community-tested on dual SM120/SM121 setups |
| **Marlin fallback (26.04)** | ⚠️ Slower | Without b12x kernels, falls back to Marlin FP4 GEMMs; 18–32% slower than AWQ |

### NVFP4 serve flags (draft — not yet tested)

```
vllm serve /models/Step-3.7-Flash-NVFP4 \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name step-3.7-flash \
    --tensor-parallel-size 2 \
    --distributed-executor-backend mp \
    --nnodes 2 \
    --node-rank 0 \
    --master-addr 10.0.200.12 \
    --trust-remote-code \
    --quantization modelopt \
    --kv-cache-dtype fp8 \
    --max-model-len 131072 \
    --gpu-memory-utilization 0.75 \
    --enable-chunked-prefill \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --enable-expert-parallel \
    --speculative-config '{"method": "mtp", "num_speculative_tokens": 3}'
```

`--tool-call-parser hermes` is from community NVFP4 testing — verify against the
model card before deploying. `--speculative-config` enables MTP-3 draft decoding
(the MTP bf16 draft layer is included in the NVFP4 checkpoint).

### NVFP4 assessment

The right deployment target for Step-3.7-Flash on this hardware. The memory story is
substantially better than FP8, and MTP-3 speculative decoding support is baked into the
checkpoint. Two blockers must be resolved in order:

1. **`--kv-cache-dtype fp8` multi-turn corruption** — investigate on the current model
   (Step-3.5-Flash-FP8) first: enable fp8 KV cache alone, run the multiturn benchmark
   quality check. If clean, NVFP4 inherits that clearance.
2. **Container bump to 26.05+** — pull new image on both nodes (verify digest match),
   then test NVFP4. Without the b12x kernels, FP4 GEMMs fall back to Marlin and
   throughput is significantly reduced.

---

## Comparison: Step-3.7-Flash vs Step-3.5-Flash

| | Step-3.5-Flash-FP8 | Step-3.7-Flash-FP8 | Step-3.7-Flash-NVFP4 |
|---|---|---|---|
| Total weights on disk | ~195 GiB | ~213 GiB | ~129 GiB |
| Per node at TP=2 | ~97.5 GiB | ~106.5 GiB | ~64.5 GiB |
| Recommended gmu | 0.90 | 0.95 | 0.75 |
| KV headroom per node | ~11 GiB | ~8.5 GiB | ~22 GiB |
| Outside headroom per node | ~5 GiB | ~6 GiB | ~30 GiB |
| Profile archetype | Fully-committed | Fully-committed+ | Big-shared with headroom |
| MTP speculative decoding | ⚠️ Not in checkpoint | ✅ In checkpoint | ✅ In checkpoint (bf16 draft) |
| Vision encoder | ❌ | ✅ 1.8B ViT | ✅ 1.8B ViT |
| Standard vLLM (26.04)? | ✅ | ✅ | ⚠️ Needs 26.05+ for native FP4 |
| fp8 KV cache required? | No | No | 🔴 Yes |
| **Ready to deploy?** | **✅ (running)** | **⚠️ Marginal fit** | **⚠️ Two blockers** |

---

## Deployment Blockers & Sequencing

**To deploy Step-3.7-Flash-NVFP4 (the target):**

1. **Resolve fp8 KV cache multi-turn corruption** (see README Pending Investigation).
   Use the Step-3.5-Flash-FP8 model with `--kv-cache-dtype fp8` alone; run multiturn
   benchmark quality check. This investigation unblocks NVFP4 regardless of container.

2. **Bump container to 26.05+** (or whichever image includes the b12x SM121 FP4 merge
   from 2026-05-20). Pull both nodes, verify digest match, then proceed.

3. **Download NVFP4 weights** to `/opt/cluster/model-cache/Step-3.7-Flash-NVFP4` on sparky.
   The model role will move them to `/opt/vllm/models` and mirror to snoopy.

4. **Deploy and benchmark.** Use `run.sh --smoke` as the initial quality gate, then
   the full benchmark run for throughput numbers.

**To deploy Step-3.7-Flash-FP8 (fallback if NVFP4 remains blocked):**

1. Download weights to `/opt/cluster/model-cache/Step-3.7-Flash-FP8`.
2. Deploy with gmu=0.95 and `max-model-len 16384`. Check vLLM startup log for
   `estimated maximum model length` — trust that number over the flags.
3. Expect fully-committed behaviour: no meaningful dev headroom on either node.

---

## What to Watch For

1. **fp8 KV cache investigation result** — primary gate for NVFP4
2. **NVIDIA vLLM 26.05+ release** — must include the b12x SM121 FP4 kernel merge (PR #40082, merged 2026-05-20)
3. **MTP-3 community results at TP=2** — throughput uplift from speculative decoding not yet measured on this hardware profile
4. **`--tool-call-parser` for 3.7** — verify correct parser name (step3p5 vs hermes vs other) from model card before deploying

---

## Key Links

| Resource | URL |
|---|---|
| HuggingFace (BF16) | https://huggingface.co/stepfun-ai/Step-3.7-Flash |
| HuggingFace (FP8) | https://huggingface.co/stepfun-ai/Step-3.7-Flash-FP8 |
| HuggingFace (NVFP4) | https://huggingface.co/stepfun-ai/Step-3.7-Flash-NVFP4 |
| HuggingFace (GGUF) | https://huggingface.co/stepfun-ai/Step-3.7-Flash-GGUF |
| NVFP4 discussions (TP requirements, MTP) | https://huggingface.co/stepfun-ai/Step-3.7-Flash-NVFP4/discussions/1 |
| vLLM PR #40082 (b12x SM121 FP4 kernels) | https://github.com/vllm-project/vllm/pull/40082 |
| NVIDIA forum: FP4/NVFP4 on DGX Spark | https://forums.developer.nvidia.com/t/psa-state-of-fp4-nvfp4-support-for-dgx-spark-in-vllm/353069 |
| NVIDIA NIM model card | https://build.nvidia.com/stepfun-ai/step-3.7-flash/modelcard |
| GitHub repo | https://github.com/stepfun-ai/Step-3.7-Flash |
