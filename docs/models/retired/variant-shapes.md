# Retired variant profiles — the shapes that lost

**A variant profile is a second way of serving the SAME weights** (`topology.VARIANT_SUFFIXES`):
a `-single` topology, or a `-mtp3`/`-eagle` optimization, existing so it can be A/B'd against
a bare-name twin. When the twin wins, the variant is retired and this is where it goes.

The **verdict** on a MODEL lives in [`../tombstones.md`](../tombstones.md); this file is about
*shapes*, and most of the models below are still in the fleet.

Nothing here is parsed. It is kept because reviving a shape costs a deploy, and the config is
the expensive part to reconstruct.

## Single-node (TP=1) — retired 2026-08-10

Three paired benchmarks, run back to back on the ADR-0016 HTTP-native harness. TP=2 won on
**every axis in every pair**, which is why the single-node archetype has no live members:

| model | TP=2 decode | TP=2 throughput | TP=2 KV |
|---|---|---|---|
| Qwen3-Coder-Next | **1.47x** | +41% | **7.6x** |
| Qwen3.6-35B-A3B | **1.34x** | +46% | **3.9x** |
| Nemotron-3-Puzzle-75B | **1.59x** | +50% | **4.65x** |

The surviving argument for TP=1 was never speed — it was **fleet occupancy**: TP=2 commits
both nodes, leaving ~24 GiB of dev headroom on sparky rather than the whole box. No current
model makes that trade worth the measured loss.

### `qwen3-coder-next-nvfp4-single`

```yaml
---
# Profile: qwen3-coder-next-nvfp4-single — Qwen3-Coder-Next (NVFP4) on snoopy, TP=1.
# Single-node serving runs on snoopy by design: sparky is the head (Open WebUI,
# Caddy, control panel, metrics) and the workstation-driven dev node, so single-node
# models serve on the resource-richer snoopy. Requires the 26.06 container (NVFP4).
#
# Memory math (gmu 0.55) and the "why per-node, not TP=2" rationale for the small
# NVFP4 models: see docs/profile-tuning.md.

profile_name: qwen3-coder-next-nvfp4-single
hf_repo: RedHatAI/Qwen3-Coder-Next-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [single-node, tool-calling]
# ON 26.07 (2026-08-08). This profile has NO spec-decode, so unlike the qwen3.6
# canary it does NOT mask DEF-0003 — activating it runs FULL_AND_PIECEWISE
# cudagraphs for the first time ever on GB10. That is the point: it is the DEF-0003
# experiment. Run it AFTER the canary proves the container itself is sound.
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix  # 26.07 + xgrammar WAR (DEF-0010)
serving_topology:
  - name: qwen3-coder-next-nvfp4-single
    kind: vllm
    nodes: [snoopy]
    model: Qwen3-Coder-Next-NVFP4
    served_as: qwen3-coder-next-nvfp4-single
    tensor_parallel_size: 1
    gpu_memory_utilization: "0.55"
    max_model_len: 262144
    head_extra_args:
      - --enable-chunked-prefill
      - --enable-auto-tool-choice
      - --tool-call-parser qwen3_coder
    worker_extra_args: []
```

### `qwen3.6-35b-a3b-nvfp4-single`

```yaml
---
# Qwen3.6-35B-A3B (NVFP4) on snoopy, TP=1 — **the no-MTP sibling**, and the fleet's
# cheapest vision model.
#
# WHY IT EXISTS. `qwen3.6-35b-a3b-nvfp4-mtp3-single` runs the same weights with MTP-3
# speculative decoding, which buys 2.3× single-stream decode (ADR-0014) at two costs
# that are only now measurable:
#   * MTP **corrupts image number-reads**, so that profile is deliberately text-only —
#     it is a vision model we have forbidden from seeing.
#   * MTP breaks constrained decoding on 0.24.0 (DEF-0011): `tool_choice: "required"`
#     and named-function calls 500 there, while `auto` is fine.
# Dropping MTP gives back **vision on 22 GiB and one node**, leaving sparky entirely
# free — and it should clear DEF-0011, since `minimax-m2.7-nvfp4` (reasoning, no
# spec-decode) passes all four tool_choice shapes on the same container.
#
# IT IS ALSO THE A/B BASELINE. Same weights, same gmu, same context — the only variable
# is MTP. That makes `report base mtp3` a real comparison rather than a guess, which is
# exactly the paired-row shape ADR-0016's sweep expects and ADR-0014's register wants.
#
# NO SPECULATIVE DECODING also means cudagraphs are NOT downgraded, so this profile runs
# `FULL_AND_PIECEWISE` for real — a second data point for DEF-0003 alongside
# `qwen3-coder-next-nvfp4-single`.
#
profile_name: qwen3.6-35b-a3b-nvfp4-single
hf_repo: nvidia/Qwen3.6-35B-A3B-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [single-node, tool-calling]
# The parsers are NOT guesses: `qwen3_xml` and `qwen3` are inherited from the MTP sibling,
# which serves with them today. That is why this profile can carry tool flags while the
# four brand-new ones deliberately cannot — a proven name is not a guessed one.
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix  # 26.07 + xgrammar WAR (DEF-0010)
serving_topology:
  - name: qwen3.6-35b-a3b-nvfp4-single
    kind: vllm
    nodes: [snoopy]
    model: Qwen3.6-35B-A3B-NVFP4
    served_as: qwen3.6-35b-a3b-nvfp4-single
    tensor_parallel_size: 1
    gpu_memory_utilization: "0.55"
    max_model_len: 262144
    head_extra_args:
      - --enable-chunked-prefill
      - --kv-cache-dtype fp8
      - --enable-auto-tool-choice
      - --tool-call-parser qwen3_xml
      - --reasoning-parser qwen3
    worker_extra_args: []
```

### `nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4-single`

```yaml
---
# NVIDIA Nemotron-3-Puzzle-75B-A9B (NVFP4) on snoopy, TP=1.
#
# WHY: a US/NVIDIA model — a third ecosystem alongside the Chinese incumbents and the
# European Mistral — and the only *hybrid* architecture staged
# (`NemotronHPuzzleForCausalLM`: Mamba/attention hybrid, 512 experts, A9B active).
# At 50 GiB it is a single-node profile, so it costs sparky nothing.
#
# QUANT: `quant_algo: MIXED_PRECISION` (modelopt), like Mistral-Medium — mixed FP8/NVFP4
# layers, resolved by vLLM's `modelopt_mixed`. `kv_cache_scheme` FP8 is declared, so
# `--enable-prefix-caching` stays absent (DEF-0007). **No `--quantization` flag.**
#
# MEMORY (gmu 0.65, snoopy TP=1): 78.6 GiB budget − ~50 weights − ~1 graphs → ~27 GiB KV.
#   KV is cheap on a hybrid (2 kv_heads, head_dim 128, and the Mamba blocks carry state
#   rather than KV), so 131072 context is comfortable. ~42 GiB/node left outside vLLM.
#   gmu is 0.65 rather than the 0.55 the other single-node profiles use because the
#   weights are 2× theirs; drop it toward 0.55 if snoopy needs more dev room.
#
# HYBRID CAUTION: Mamba state layers historically constrain tensor-parallel sharding.
# TP=1 sidesteps that entirely — which is a reason to keep this profile single-node even
# though 50 GiB would nominally fit a TP=2 split.
#
# TOOL CALLING IS DELIBERATELY ABSENT, and this is now an informed choice rather than a
# pending one. `./sparky.sh probe parsers` (2026-08-08) lists 43 tool parsers and **none
# of them is a Nemotron parser** — while the reasoning side does ship `nemotron_v3`. So
# there is no vendor-specific tool format to name here, and the generic candidates
# (`hermes`, `pythonic`) are unverified for this model. A wrong name is a refusal to
# start, and it costs a whole deploy to discover, so this profile serves without tools
# until one is confirmed. Plain chat and reasoning are unaffected.
profile_name: nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4-single
hf_repo: nvidia/NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [single-node]
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix
serving_topology:
  - name: nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4-single
    kind: vllm
    nodes: [snoopy]
    model: NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-NVFP4
    served_as: nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4-single
    tensor_parallel_size: 1
    gpu_memory_utilization: "0.65"
    max_model_len: 131072
    head_extra_args:
      - --enable-chunked-prefill
    worker_extra_args: []
```

## Optimization variants

### `qwen3.6-35b-a3b-nvfp4-mtp3-single` — retired 2026-08-10

MTP-3 measured the **slowest** of the three qwen3.6 shapes (35.3 tok/s against 100.2 at TP=2)
while forfeiting vision and constraining tool calling. See ADR-0014's errata.

```yaml
---
# Profile: qwen3.6-35b-a3b-nvfp4-mtp3-single — Qwen3.6-35B-A3B (NVFP4) on snoopy, TP=1,
# MTP-3 speculative decoding ENABLED. Single-node serving runs on snoopy by design
# (sparky = head/frontends + dev). Requires the 26.06 container.
#
# MTP-3 is KEEP per ADR-0014 (A/B 2026-07-27): single-stream decode 2.3× faster
# (TPOT 37.6→16.6 ms, ~27→~60 tok/s), stable, exact output. TEXT-ONLY (MTP corrupts
# image number-reads). Memory math (gmu 0.55) + the max_num_batched_tokens follow-up:
# see docs/profile-tuning.md and ADR-0014's register.

profile_name: qwen3.6-35b-a3b-nvfp4-mtp3-single
hf_repo: nvidia/Qwen3.6-35B-A3B-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [single-node, tool-calling]
# FIRST 26.07 CANARY (2026-08-08). Chosen deliberately: it keeps MTP-3, and spec-decode
# is what makes vLLM downgrade cudagraphs to PIECEWISE — so DEF-0003 stays masked here
# and the container is the ONLY variable that moves. A spec-decode-free profile on 26.07
# (qwen3-coder) is the separate, later experiment that unmasks it.
# 26.07 = vLLM 0.24.0, NCCL 2.30.7, fastapi 0.136.3 (capped -> retires the derived image).
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix  # 26.07 + xgrammar WAR (DEF-0010)
serving_topology:
  - name: qwen3.6-35b-a3b-nvfp4-mtp3-single
    kind: vllm
    nodes: [snoopy]
    model: Qwen3.6-35B-A3B-NVFP4
    served_as: qwen3.6-35b-a3b-nvfp4-mtp3-single
    tensor_parallel_size: 1
    gpu_memory_utilization: "0.55"
    max_model_len: 262144
    head_extra_args:
      - --enable-chunked-prefill
      - --kv-cache-dtype fp8
      - --enable-auto-tool-choice
      - --tool-call-parser qwen3_xml
      - --reasoning-parser qwen3
      # Written UNSPACED and UNQUOTED on purpose (ADR-0018): serve flags travel
      # through the engine env file as one single-quoted value that systemd re-splits
      # on whitespace with no quote processing, so JSON must carry no spaces and no
      # outer quotes. The fleet role rejects a flag that would mis-split.
      - --speculative-config {"method":"mtp","num_speculative_tokens":3}
    worker_extra_args: []
```

### `qwen3-vl-32b-instruct-nvfp4-single` — retired 2026-08-10

Retired because its **niche closed**, not because it was blocked: it existed as the cheapest
route to vision, and `qwen3-vl-235b-a22b-instruct-nvfp4` now serves vision and tools at 75.0%.
DEF-0013 also still refuses the checkpoint. The tombstone row owns that verdict.

```yaml
---
#    still [FP8, NVFP4], no NVFP4_AWQ — the park was legitimate). Retired because its
#    ARGUMENT died: it existed as the cheapest vision option — 21 GiB, one node, sparky
#    free — and the single-node shape lost on measurement the same day, while
#    qwen3-vl-235b-nvfp4 serves vision + tools at 75.0%. Unblocking it would not make us
#    run it.
#    Verdict is owned by docs/models/tombstones.md. See retired/README.md.
#
# Qwen3-VL-32B-Instruct (NVFP4) on snoopy, TP=1 — the fleet's first VISION model.
#
# WHY: nothing in the fleet currently accepts an image. `step-3.7-nvfp4` was supposed to
# and cannot (DEF-0006, upstream VL processor bug), so the capability has been missing
# since it was parked. This is the cheapest possible way to get it: 21 GiB, one node,
# sparky untouched and free for dev.
#
# QUANT: `quant_algo: NVFP4_AWQ` — NVFP4 weights with AWQ-style scaling, produced by
# modelopt. Despite the name this is NOT the compressed-tensors WNA16 Marlin path that
# DEF-0004 lives in (that one froze a node); it resolves through modelopt. Auto-detected,
# so **no `--quantization` flag**. Worth confirming in the first bring-up log that it
# picks a modelopt kernel and not Marlin.
#
# MEMORY (gmu 0.55, snoopy TP=1): 66.5 GiB budget − ~21 weights → ~44 GiB KV.
#   KV/token = 2 × 64 layers × 8 kv_heads × 128 head_dim × 2 = 256 KiB
#   → ~180k tokens at 131072 context, comfortably several concurrent sequences.
#   ~55 GiB/node left outside vLLM.
#
# FLAGS ARE DELIBERATELY MINIMAL. No tool-call or reasoning parser yet: this is an
# `-Instruct` (non-reasoning) checkpoint, and the correct `--tool-call-parser` name for
# the Qwen3-VL family is unverified. A wrong parser name is not a warning, it is a
# refusal to start — so the names get confirmed with `./sparky.sh probe parsers` after
# this deploy, and added in the next one. Until then tool_choice=auto will be refused,
# which affects Open WebUI tool use but not plain chat or vision.
# ⛔ PARKED 2026-08-08 — vLLM 0.24.0 REFUSES THIS CHECKPOINT.
# Its `quant_algo` is `NVFP4_AWQ`, and ModelOpt in this build accepts only
#   FP8 · FP8_PER_CHANNEL_PER_TOKEN · FP8_PB_WO · NVFP4 · W4A16_NVFP4 · MXFP8 · MIXED_PRECISION
# so it fails at config validation, before any weight load. Note that
# `modelopt_fp4` IS among vLLM's quantization *methods* — the algo allowlist is a
# different, shorter list, and that distinction is what made this look supported.
#
# Parked rather than deleted (ADR-0018's gesture): the 21 GiB of weights stay on
# snoopy, so re-testing costs no download when vLLM adds NVFP4_AWQ. The sibling
# `qwen3-vl-235b-nvfp4` is plain NVFP4 and unaffected.
#
# UNBLOCK WHEN: `./sparky.sh probe quant` lists NVFP4_AWQ under `modelopt_algos`.
# ALTERNATIVE: stage a plain-NVFP4 or FP8 build of Qwen3-VL-32B instead.
blocked: true
profile_name: qwen3-vl-32b-instruct-nvfp4-single
hf_repo: cybermotaz/Qwen3-VL-32B-Instruct-NVFP4
# What this profile is an EXAMPLE OF, so tests can bind to the SHAPE rather than
# to this model's name (sparky/topology.py: ARCHETYPES).
archetypes: [single-node, vision]
vllm_image: dgx-spark/vllm:26.07-xgrammar-fix
serving_topology:
  - name: qwen3-vl-32b-instruct-nvfp4-single
    kind: vllm
    nodes: [snoopy]
    model: Qwen3-VL-32B-Instruct-NVFP4
    served_as: qwen3-vl-32b-instruct-nvfp4-single
    tensor_parallel_size: 1
    gpu_memory_utilization: "0.55"
    max_model_len: 131072
    head_extra_args:
      - --enable-chunked-prefill
    worker_extra_args: []
```
