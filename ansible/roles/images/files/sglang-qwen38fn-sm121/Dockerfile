# Derived SGLang image for Qwen3.8-Flash-Next-NVFP4 on DGX Spark (GB10 / SM121), ADR-0030.
#
# "Stock base + recipe data": FROM the PUBLIC lmsysorg/sglang:qwen38flashnext (the LMSYS
# day-0 image — arm64/SM121 kernels, FlashInfer and a good NCCL already compiled), plus the
# reproducible SM121 QSA fix carried as REVIEWABLE SOURCE in this repo (patch_qsa.py + the
# Triton fallback kernel) — not a wheel build, and not tonyd2wild's unpublished kernel-on +
# workaround image. This is MiaAI-Lab's root-cause fix (sglang#36806 exclude SM121 from the
# TRT-LLM sparse-decode kernel; #36845 the Triton packed-varlen fallback).
#
# TEXT-ONLY on purpose. The NVFP4-KV-cache patches (qsa_nvfp4_kv.py / apply_nvfp4_patches.py)
# and the M-RoPE vision fix are deliberately NOT applied here — the QSA fix does not depend
# on them (the kernel upcasts fp8 K/V loads to fp32), and the first bring-up is text-only.
# They are a separate concern for a later vision/NVFP4-KV bring-up.
#
# BUILT BY the images role on every node (ADR-0013). FROM is pinned to the same digest the
# role pulls as the base (the SGLang `pull:` entry in container_images — keep the two in
# step), so the build is byte-reproducible on both TP=2 ranks. Mismatched images across ranks
# would be a silent correctness hazard, which is why the base is a digest, not the floating tag.
# The patch's asserts ARE the acceptance test: a base whose file layout shifted fails the
# DEPLOY, not an engine 20 minutes into a weight load.
FROM lmsysorg/sglang@sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1
# Ownership mark so the images role's dangling-layer prune stays scoped to OUR builds
# (`--filter label=net.flummoxed.sparky-cluster=true`); a bare prune is daemon-wide.
LABEL net.flummoxed.sparky-cluster="true"
COPY sm121_varlen.py /sgl-workspace/sglang/python/sglang/srt/layers/attention/qsa/sm121_varlen.py
COPY patch_qsa.py /tmp/patch_qsa.py
RUN python3 /tmp/patch_qsa.py \
 && python3 -c "p='/sgl-workspace/sglang/python/sglang/srt/layers/attention/qwen_sparse_attn_backend.py'; s=open(p).read(); assert 'qsa.sm121_varlen' in s and 'dspark: SM121 must not use TRT-LLM sparse decode' in s, 'SM121 QSA patch did not land'; import os; assert os.path.exists('/sgl-workspace/sglang/python/sglang/srt/layers/attention/qsa/sm121_varlen.py'), 'sm121_varlen kernel missing'; print('SM121 QSA patch verified in the image')" \
 && rm -f /tmp/patch_qsa.py
