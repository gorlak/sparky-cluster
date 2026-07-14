# Upgrade: `step` profile → Step-3.7-Flash

**Status:** ⛔ Blocked on upstream vLLM (parked; `blocked: true` in the profile)
**Profiles:** `step-3.5-fp8` (serves `Step-3.5-Flash-FP8`, 26.04, stable) · `step-3.7-nvfp4`
(candidate: `Step-3.7-Flash-NVFP4`, 26.06 — **parked/hidden**)
**Target:** Step-3.7-Flash-NVFP4 on 26.06 (the two profiles A/B against each other)
**Last updated:** 2026-07-02

> **Result (2026-07-02):** deployed `step-3.7-nvfp4` — **NVFP4 loaded + ran on 26.06 with no
> hang.** The hard part works and per-profile container pinning is validated (26.06 for
> `step-3.7-nvfp4`, 26.04 for everything else). The remaining blocker is an **upstream vLLM VL
> bug, not NVFP4/the container**: Step-3.7's `Step3VLProcessor` crash-loops on startup —
> `AttributeError: … has no attribute '_get_num_multimodal_tokens'`
> (`vllm/model_executor/models/transformers/multimodal.py`).
> **Unblock when:** vLLM ships a `Step3VLProcessor` with `_get_num_multimodal_tokens`
> (bump `step-3.7-nvfp4`'s `vllm_image` to the container that carries it), then remove
> `blocked: true` and re-test. Detail: `docs/upgrades/container-nvidia-vllm-26.06-py3.md`.

This tracks what changes for the **`step` Ansible profile** if it moves from
Step-3.5-Flash to Step-3.7-Flash — the *delta* and its cluster implications. For the
standalone per-model analysis (facts, memory math, serve flags, each independent of the
other) see the model sheets: `docs/models/step-3.5-flash.md` and
`docs/models/step-3.7-flash.md`. This doc is the transition, not the fact sheet.

## Why consider it

- 3.7 is a newer Step-Flash generation, and its checkpoint adds two things 3.5 lacks:
  a native **vision encoder** (1.8B ViT) and **MTP-3 speculative decoding**.
- The **NVFP4** quant of 3.7 roughly halves the per-node footprint vs FP8 — which would
  change the *character* of the `step` profile, not just the model quality.

## The three options for the `step` profile

| | Step-3.5-Flash-FP8 (current) | Step-3.7-Flash-FP8 | Step-3.7-Flash-NVFP4 |
|---|---|---|---|
| Disk | ~195 GiB | ~213 GiB | ~129 GiB |
| Per node @ TP=2 | ~97.5 GiB | ~106.5 GiB | ~64.5 GiB |
| Recommended `gmu` | 0.90 | 0.95 | 0.75 |
| KV headroom / node | ~11 GiB | ~8.5 GiB | ~22 GiB |
| Outside headroom / node | ~5 GiB | ~6 GiB | ~30 GiB |
| Profile archetype | fully-committed | fully-committed+ | big-shared + headroom |
| Vision / MTP-3 | ❌ / ❌ | ✅ / ✅ | ✅ / ✅ |
| Container | 26.04 ✅ | 26.04 ✅ | **26.06** ⚠️ (upgrade blocked) |
| `--kv-cache-dtype fp8` | not used | not used | **required** ⚠️ |

## Implications for the cluster

- **FP8 path (3.7-FP8):** runs on the *current* container, but it is *tighter* than 3.5
  (gmu 0.95, ~6 GiB outside headroom) with no memory or dev-headroom gain — a pure
  quality bump bought with a higher OOM risk. Marginal fit; trust vLLM's
  `estimated maximum model length` at startup over the configured `max_model_len`.
- **NVFP4 path (3.7-NVFP4):** the compelling one — it *changes the profile's character*,
  freeing ~30 GiB/node for dev/build work while serving a stronger, multimodal model.
  Today `step` is fully-committed (both nodes essentially maxed); NVFP4 would move it into
  the same "big-shared with headroom" class as the `minimax-m2.7-awq` profile.

## Dependencies (what must land first for the NVFP4 path)

1. **Container upgrade to 26.06** — NVFP4 needs the b12x SM12.1 FP4 kernels that 26.04
   lacks. That upgrade is itself in-progress and upstream-blocked — see
   `docs/upgrades/container-nvidia-vllm-26.06-py3.md`.
2. **fp8 KV cache multi-turn investigation** — NVFP4 requires `--kv-cache-dtype fp8`, the
   same knob implicated in the `step` multi-turn corruption (README "Pending
   investigation"). Must be cleared before NVFP4 can be trusted.

## Recommendation

Hold `step` on **Step-3.5-Flash-FP8** for now. NVFP4 is the target but is gated on the
container upgrade (upstream-blocked) and the fp8-KV-cache investigation. The 3.7-**FP8**
interim isn't worth it: marginal fit, no headroom benefit, higher OOM risk, quality-only.
Revisit when the container upgrade clears.

## Deployment note

Step-3.7 weights are not downloaded (`Installed: None`). Whichever quant is chosen, stage
it into the inbox first (`/opt/cluster/model-cache/Step-3.7-Flash-<QUANT>`), then point
`ansible/profiles/step-3.7-nvfp4.yml` at it and `./sparky.sh deploy step-3.7-nvfp4`.

## References

- `docs/models/step-3.5-flash.md`, `docs/models/step-3.7-flash.md` — per-model fact sheets
- `docs/upgrades/container-nvidia-vllm-26.06-py3.md` — the container upgrade the NVFP4 path depends on
- ADR-0009 (fail-safe boot); README "Pending investigation" (fp8 KV cache)
