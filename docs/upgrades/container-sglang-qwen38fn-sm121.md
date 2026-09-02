# Image: `dgx-spark/sglang:qwen38fn-sm121` (the first SGLang image, ADR-0030)

**Status:** 🟠 **First bring-up attempted 2026-08-30 — deploy + image + weights + TP=2
rendezvous all GOOD; failed at warmup on the KV dtype.** The image built clean on both nodes,
weights mirrored, and — the biggest de-risk — the **NCCL SM121 TP=2 rendezvous succeeded**
(cross-node torch.distributed init in <4 s, `nccl==2.29.7`, no deadlock). It crashed in warmup
with `Unsupported rhs dtype fp8e4nv`: the **compressed-QSA path** (`--page-size 64`) does
`tl.dot(q, keys)` on the **raw fp8 KV**, and Triton has no fp8 `tl.dot` on SM121. Our own
`sm121_varlen.py` upcasts fp8→fp32 and was fine; the crash is in a *different*, base-image QSA
kernel. **Fix in flight:** dropped `--kv-cache-dtype fp8_e4m3` → default bf16 (universally
dottable). Awaiting a re-deploy + re-activate. If bf16 is insufficient, the proven path is the
deferred NVFP4-KV patch (below).
**Base:** `lmsysorg/sglang:qwen38flashnext`, pinned by digest
`sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1` — the LMSYS day-0 image
(arm64/SM121 kernels, FlashInfer and NCCL precompiled).
**Last updated:** 2026-08-30

This is a **living tracker**, not a decision record — [ADR-0030](../adr/0030-sglang-second-engine-kind.md)
owns the decision to run SGLang at all. Here we track the one image and its patch.

---

## What the image adds to the base

The stock base already serves `qwen4_exp` on x86; on **GB10 (SM121)** the QSA decode path needs
one source fix, which this image applies as **reviewable Python in the repo** (not a wheel build,
not an opaque third-party image):

- **`patch_qsa.py`** — two guarded, idempotent inserts into
  `sglang/srt/layers/attention/qwen_sparse_attn_backend.py`:
  - **sglang#36806** — exclude SM121 from FlashInfer's TRT-LLM sparse-decode kernel (it silently
    emits token id 0 at long context, sglang#36537).
  - **sglang#36845** — route SM121 to a Triton packed-varlen fallback instead of FA4's CuTe varlen
    (which fails to compile for the QSA call shape).
- **`sm121_varlen.py`** — the Triton fallback kernel (192 lines), verbatim from the proven recipe.

This is **MiaAI-Lab's root-cause fix**, chosen over tonyd2wild's `radixark/…:sm121-qsa` image
because the latter is unpublished (not pullable) and takes the opposite approach (kernel ON +
token-0 workaround flags). The patch's build-time asserts are the acceptance test: a base whose
file layout shifts fails the **deploy**, not an engine 20 minutes into a weight load.

## Deliberately deferred (text-first)

Not applied here, and NOT needed for a text bring-up:

- **NVFP4 KV-cache** (`qsa_nvfp4_kv.py` + `apply_nvfp4_patches.py`) — the QSA fallback kernel
  upcasts fp8 K/V loads to fp32, so the profile runs `--kv-cache-dtype fp8_e4m3` with no host-side
  dequant. Add later for a larger KV pool.
- **M-RoPE / vision fix** (`rotary_triton.py`, from tonyd2wild) — the model is vision-capable, but
  vision + this arch needs an M-RoPE OOB-read fix and is incompatible with MTP. Vision is a
  **separate later engine**; this image is text + tools only (hence no `vision` archetype on the
  profile).

## Bring-up watch (the attended activation)

1. **Memory** — re-derive the ADR-0028 headroom floor for SGLang (`--mem-fraction-static 0.80` is
   the proven value; the 51B PLE table shares the unified pool). Lower to 0.75 or add
   `--ple-offload-embedding` if the load OOMs.
2. **NCCL SM121 cubins** — the #1 TP=2 hazard: PyTorch-bundled NCCL lacks them → cross-node
   collectives deadlock ~30 min in. The LMSYS base *should* ship a good NCCL; confirm, and
   `LD_PRELOAD` one via the profile's `engine_env:` if it deadlocks.
3. **`qsa/` import** — the patch imports `sglang…attention.qsa.sm121_varlen`; the COPY assumes the
   base ships `qsa/` as a package. Proven in MiaAI-Lab's recipe, but a runtime ImportError here
   means the base changed — add an `__init__.py`.

## Remove / revisit when

- **vLLM gains `qwen4_exp` on GB10** (an arm64 image registering the arch with a working SM121 QSA
  path). Then this model could move to `kind: vllm` and the image retire — but SGLang stays a
  standing engine kind for the *next* day-1 arch regardless (ADR-0030).
- **Upstream SGLang ships the SM121 QSA fix in a stock tag.** Then drop `patch_qsa.py` +
  `sm121_varlen.py` and repoint the SGLang `pull:` entry in `container_images` at that tag
  (still digest-pinned).
- **The base tag moves.** It is digest-pinned in two places — the SGLang `pull:` entry in
  `container_images` and the Dockerfile `FROM` — which must be bumped together; re-read with
  `docker manifest inspect lmsysorg/sglang:qwen38flashnext`.
