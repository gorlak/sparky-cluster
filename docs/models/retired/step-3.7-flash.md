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

---

## The retired profile configuration

Kept because reviving this model costs a deploy and the config is the expensive part to
reconstruct — the parser names, the flag combinations and the memory math were each learned
the hard way. Nothing parses it; `ansible/profiles/*.yml` is the allowlist and this is not
there.

### `step-3.7-flash-nvfp4`

```yaml
---
#    WHY: two of its three generation paths emit token soup, and the one that works is the
#    one nothing uses.
#      * `<think>` -> garbage (DEF-0017). The chat template opens `<think>` on EVERY
#        generation, so every Open WebUI message is broken.
#      * images    -> garbage/empty (DEF-0015).
#      * a bare completion with no `<think>` -> perfect. That is the only healthy path, and
#        no ordinary request takes it.
#    Reproduced on raw /v1/completions with no parsers and no chat template, so it is the
#    checkpoint or its quantization — not vLLM's parsing and not our integration.
#
#    It passed EVERY GATE WE HAVE while being unusable, which is the part worth remembering:
#    ready / tool-shape 200 / quality pass / vision n/a -> "live and gated". The gate reads
#    the final `content`, and this model recovers after the garbage.
#
#    Verdict and revisit condition are owned by docs/models/tombstones.md. This file keeps
#    the CONFIG and what was learned, which was a great deal: the `step3p5` parser names
#    (proven), the DEF-0006 re-diagnosis, and the `--limit-mm-per-prompt` pattern for making
#    a broken capability absent rather than wrong — that one generalises.
#    See docs/models/retired/README.md.
#
#    HISTORY. The fact sheet moved to docs/models/retired/step-3.7-flash.md (kept for the
#    engineering); the upgrade tracker was DELETED, because a tracker is a delta to a
#    target and the target is gone. Both were forward-looking — "target quant", "not yet
#    downloaded", a planned A/B against Step-3.5 — and a living doc that plans for a
#    retired model is how a future sweep talks itself back into one. Read them at
#    docs/models/retired/step-3.7-flash.md and
#    `git show fd4c6d8:docs/upgrades/profile-step-3.7-flash.md`. The verdict lives in
#    docs/models/tombstones.md, which OWNS it; the error text lives in
#    docs/bring-up-failures.md.
#
#
# Profile: step-3.7-flash-nvfp4 — Step-3.7-Flash-NVFP4, big-shared TP=2 across sparky + snoopy.
# The successor candidate to `step-3.5-flash-fp8` (activate each in turn to A/B; both are
# full-cluster TP=2, so the comparison is sequential).
#
# ⚠️ ON TRIAL — DEF-0006, unparked 2026-08-11 after the defect was re-diagnosed. Parked
#   since 2026-07-02 on a reading of the error that turned out to name the wrong repo.
#
#   WHAT THE 2026-07 FAILURE ACTUALLY WAS. On 26.06 the engine crash-looped with
#     AttributeError: 'Step3VLProcessor' object has no attribute '_get_num_multimodal_tokens'
#     (vllm/model_executor/models/transformers/multimodal.py, in max-image-token profiling)
#   and that path is the giveaway: `transformers/multimodal.py` is vLLM's **generic
#   transformers-backend fallback**, taken only when vLLM has no native implementation of
#   the architecture. This checkpoint is `Step3p7ForConditionalGeneration` whose
#   `auto_map` points at remote code IN THE CHECKPOINT ITSELF (`processing_step3.py`), so
#   the `Step3VLProcessor` in the error is **a file in /opt/vllm/models**, not vLLM's and
#   not transformers'. It defines `get_num_image_tokens`, never the underscore-prefixed
#   method the fallback demands. Chasing this upstream was chasing three wrong repos.
#
#   WHY IT SHOULD WORK NOW. vLLM 0.24.0 (26.07) ships a **native** `step3p7` model
#   module, so the fallback that raised is never entered. Probed 2026-08-11, no
#   activation (ADR-0019):
#     probe archs Step3p7ForConditionalGeneration                        -> true
#     probe attr vllm.model_executor.models.step3p7 Step3p7ForConditionalGeneration -> true
#   The 2026-08-10 re-probe that reported "still missing" asked
#   `step3_vl.Step3VLProcessor._get_num_multimodal_tokens` — a class that does not exist
#   in that module, so the probe answered false for the absent CLASS and was read as an
#   absent METHOD. `probe attr` cannot tell those two apart; probe the arch, not the
#   attribute, when the question is "does vLLM implement this model".
#
#   RESIDUAL RISK, and it is the reason this is a trial rather than a fix: the engine
#   runs with the template's global `--trust-remote-code`, so config/processor loading
#   may still resolve through `auto_map` even though the MODEL class is now native. If it
#   does, expect the same AttributeError — in which case the honest options are StepFun's
#   own `vllm/vllm-openai:stepfun37` image (arm64-cu130 variant exists, and it carries
#   sm_121 cubins) or rejecting the checkpoint. Note that image sets
#   `ENTRYPOINT ["vllm","serve"]` while our unit already appends `vllm serve …`, so
#   adopting it needs a derived image that clears the entrypoint — not just a pull.
#
#   ✅ RESULT 2026-08-11 — IT SERVES. Weights loaded (58.58 GiB, 427 s), `modelopt_fp4`
#   auto-detected, KV 22.33 GiB → 42.6× concurrency at 32k, API up, multiturn quality
#   PASSED, and the AttributeError never fired. DEF-0006 is CLOSED.
#
#   ✅ RESOLVED 2026-08-12 — SERVED AS A TEXT MODEL, and its gate is green on that basis.
#   Two things failed the gate on 2026-08-11, neither of them DEF-0006:
#     1. `tool-shape: 400` — no parsers. FIXED: `step3p5` for both tool-call and reasoning,
#        evidenced three ways (proven on this cluster with step-3.5, `text_config.model_type`,
#        byte-identical tool markup). Re-tested 2026-08-12: **400 -> 200**.
#     2. Vision returns nothing usable -> **DEF-0015**, an UPSTREAM correctness bug: the
#        checkpoint's vision tower is complete and unquantized, vLLM has a `PerceptionEncoder`
#        for it, weights load without a warning — and the embeddings reaching the language
#        model are still meaningless. Four theories ruled out; see the defect row.
#
#   THE INTEGRATION ANSWER, which is separate from the model's defect: a broken feature we
#   cannot fix is acceptable to CARRY, but not to ADVERTISE. Serving a model that accepts an
#   image and returns nonsense is a broken vertical in OUR system whoever's bug it is. So
#   `--limit-mm-per-prompt {"image":0}` makes images a 400 — the capability is ABSENT, not
#   WRONG — and `archetypes` drops `vision` because that is what this profile now is.
#
#   Full story: `git show fd4c6d8:docs/upgrades/profile-step-3.7-flash.md`. Weights staged on both nodes.

# ONE NAME. profile == engine == served_as == the lowercased canonical HF model
# name, so the scoreboard, the systemd unit, the API and huggingface.co all agree.
# `hf_repo` is the exact upstream id for eyeball-matching against the Hub.
profile_name: step-3.7-flash-nvfp4

# ⛔ PARKED — DEF-0017. Its THINKING path emits token soup, and the chat template opens
# `<think>` on every single generation, so every Open WebUI message is broken. Weights and
# engine files are kept; drop this line to re-test when a container or checkpoint changes.
blocked: true
hf_repo: stepfun-ai/Step-3.7-Flash-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [big-shared]

# 26.07 is the container the unblock rests on — it is the release that carries the
# native `step3p7` module (see above). The fleet default is this same image, but the
# override stays explicit: this profile's container choice is now load-bearing evidence,
# not inherited convenience, and a future default bump must re-probe before taking it.
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix  # 26.07 + xgrammar WAR (DEF-0010)

serving_topology:
  - name: step-3.7-flash-nvfp4
    kind: vllm
    model: Step-3.7-Flash-NVFP4  # dir under /opt/vllm/models (stage first — see PREREQ)
    served_as: step-3.7-flash-nvfp4
    # NVFP4 is ~62 GiB/shard → "big-shared with headroom": gmu 0.75 leaves ~30 GiB/node
    # free. See docs/models/retired/step-3.7-flash.md.
    gpu_memory_utilization: "0.75"
    # 32768 was the first-bring-up number and its own comment said to raise toward 131072
    # "once it loads and vLLM confirms" — which it now has, twice. Measured KV holds
    # **1,568,254 tokens** (23.11 GiB), so 32768 was serving 2% of the cache at 47.9x
    # concurrency: headroom nobody was using.
    #
    # 131072 is the TRAINED length (`rope_scaling.original_max_position_embeddings`), so
    # this asks for no extrapolation at all, and still leaves ~12 concurrent full-length
    # sequences. The checkpoint's nominal ceiling is 262144 via llama3 rope factor **2.0**
    # — mild, and a DGX Spark forum recipe runs this exact model at 262K — which would
    # halve concurrency to ~6x. That is available as a one-line change if long documents
    # ever matter more than parallelism; 131072 is the honest default because it is the
    # length the model was actually trained at.
    max_model_len: 131072
    # Minimal NVFP4 flag set (model-level flags on BOTH ranks). Parsers / MTP added
    # after a clean bring-up.
    #
    # `--quantization modelopt` WAS HERE AND IS GONE (2026-08-11). It was written in
    # 2026-07 as "REQUIRED for NVFP4", which is the standing footgun of this cluster
    # stated backwards: the checkpoint SELF-DECLARES (`quant_algo: NVFP4` in both
    # config.json and hf_quant_config.json), and every other NVFP4 profile here passes
    # no `--quantization` at all. Overriding a self-declaring checkpoint double-quantizes.
    # `--kv-cache-dtype fp8` stays — the checkpoint declares `kv_cache_quant_algo: FP8`
    # and qwen3.6-35b-a3b-nvfp4 serves with it set explicitly.
    # `--enable-expert-parallel` stays too: it is StepFun's OWN recipe recommendation
    # (docs/models/retired/step-3.7-flash.md), not a guess of ours.
    # PARSERS — added 2026-08-11 after the bring-up that closed DEF-0006 came up with
    # `tool-shape: 400`. Head only: the API node is what parses (same split the retired
    # step-3.5-flash-fp8 profile used). NOT GUESSED — a wrong parser name is a refusal to
    # start, so `step3p5` is evidenced three independent ways:
    #   1. PROVEN ON THIS CLUSTER — `step-3.5-flash-fp8` served for months with exactly
    #      `--tool-call-parser step3p5 --reasoning-parser step3p5`
    #      (docs/models/retired/step-3.5-flash-fp8.yml). The probe returns MODULE
    #      names, which are a candidate set and not a lookup; a name already proven here
    #      beats anything derived.
    #   2. This checkpoint's `config.json` declares `text_config.model_type: step3p5` —
    #      the language model IS step3p5, whatever the wrapper is called.
    #   3. The markup matches BYTE FOR BYTE where it counts: Step-3.5 and Step-3.7 emit
    #      the same tool-call line (`'<tool_call>\n<function='`) and the same marker set
    #      (`<tool_call> <function= <parameter= <think> <tool_response> <|im_start|>`).
    #      That is the check that matters — profiles.md records `qwen3_xml` on Qwen3-VL
    #      returning HTTP 200 with `{}` and garbage arguments, i.e. a plausible-but-wrong
    #      parser fails by producing rubbish, not by refusing.
    # The format is nested XML, not JSON in a wrapper:
    #   <tool_call><function=NAME><parameter=NAME>value</parameter></function></tool_call>
    # DEF-0011 does not apply: no `--speculative-config` here, so no MTP to break
    # constrained decoding.
    head_extra_args:
      # SERVED AS A TEXT MODEL, DELIBERATELY (2026-08-12, DEF-0015). The checkpoint has a
      # complete vision tower and vLLM has a `PerceptionEncoder` for it, but the embeddings
      # that reach the language model are meaningless — an upstream correctness bug we
      # cannot fix from here.
      #
      # A defect in the MODEL is something this cluster can carry. A broken VERTICAL in how
      # we integrate it is not: without this flag we advertise vision on the stable endpoint
      # and hand Open WebUI users a confident wrong answer for any image they paste. This
      # makes the capability ABSENT rather than WRONG — vLLM rejects image content with 400,
      # and a refusal a caller can see beats a plausible answer they cannot check.
      #
      # It also makes the smoke gate honest instead of red: `sparky/vision.py` reports a 400
      # as `n/a` and never counts it as a failure ("a text-only model is not broken for
      # lacking a vision tower"), so the profile passes its gate as what it actually is.
      # `archetypes` drops `vision` for the same reason — it is not a VL model as served.
      #
      # ⚠️ Note DEF-0012 recorded that this flag does NOT prevent multimodal PROFILING at
      # startup. That is a different goal; here the model starts fine and we only need image
      # REQUESTS refused. Verify on the next activation that vision reads `n/a`, not `pass`
      # and not an error.
      #
      # REMOVE THIS FLAG to re-test vision the day DEF-0015 clears.
      - --limit-mm-per-prompt {"image":0}
      - --kv-cache-dtype fp8
      - --enable-expert-parallel
      - --enable-chunked-prefill
      - --enable-auto-tool-choice
      - --tool-call-parser step3p5
      - --reasoning-parser step3p5
    worker_extra_args:
      # On BOTH ranks: it configures the model's multimodal half, and every rank builds its
      # own VllmConfig. Head-only is the shape that deadlocked TP=2 on 2026-08-12.
      - --limit-mm-per-prompt {"image":0}
      - --kv-cache-dtype fp8
      - --enable-expert-parallel
      - --enable-chunked-prefill
```
