# ADR-0014: Optimization register — re-enabling tabled performance options

**Date:** 2026-07-03
**Status:** Proposed

## Context

To get the first wave of models **stable**, we turned off a number of performance
optimizations. Some were disabled for a **real bug** (FP8 KV cache + prefix caching
produced multi-turn corruption on vLLM 0.19); others were **first-bring-up caution**
(MTP/speculative decoding off, conservative `gpu_memory_utilization` /
`max_model_len`, no explicit attention backend). These decisions are correct as
*bring-up* defaults, but they're now scattered across profiles as "intentionally
omitted" comments and a README "Pending investigation" — easy to forget, and
leaving measurable performance on the table.

Two things make now the right time to re-assess:
1. **The fleet is stabilizing** — 26.06/NVFP4 is validated end-to-end, models serve.
2. **Some disables were container-version-specific.** The multi-turn corruption that
   forced FP8-KV/prefix-caching off was on **vLLM 0.19 (26.04)**; the 26.06 image runs
   **0.22.1**, which may already fix it. A disable tied to a since-patched bug is pure
   debt.

This ADR is the **register** of what's disabled and the **methodology** to turn it
back on safely — not a decision to flip everything at once.

## Options considered

**A. Leave the bring-up defaults permanently.** Safe, zero work, but permanently
slower — e.g. qwen3.6-35b stuck at ~28–30 tok/s when MTP-3 reaches ~97, and step-3.5
without prefix caching paying full TTFT on every shared system prompt. Rejected —
that's a lot of free performance forgone on hunches that may no longer hold.

**B. Turn everything back on at once.** Fast, but if serving breaks you can't tell
*which* knob did it, and the corruption-class bugs are subtle (Nth-turn, not
first-turn). Rejected — un-attributable, and it re-buries the exact "which was
load-bearing" question the disables were meant to answer.

**C. A register + one-at-a-time re-enable (chosen).** Maintain the table below;
re-enable a single knob, validate (multi-turn for the corruption class, benchmark for
throughput), keep or revert with the reason recorded. Each row is independent.

## Decision

Option C. Maintain this **optimization register** and re-enable opportunistically as
the fleet stabilizes, **one knob at a time**, behind the fail-safe net (ADR-0009),
validated against the right test:
- **corruption-class** knobs (FP8 KV, prefix caching) → a **multi-turn conversation**
  test, not a single shot (the bug appeared on the *Nth* turn);
- **throughput** knobs (MTP, cudagraphs, backend) → a **benchmark A/B** (ADR-0012);
- **version-specific** disables → **re-test on the current container** first (the
  bug may already be fixed).

Keep a knob only if it's stable *and* wins; otherwise revert and record why.

## The register

| Optimization | Where | Why disabled | Re-test | Expected win | Status |
|---|---|---|---|---|---|
| **FP8 KV cache** (`--kv-cache-dtype fp8`) — DEF-0007 | `step-3.5-fp8` (and other FP8/AWQ profiles on 26.04) | Multi-turn corruption (Nth-turn garbage / nonstop thinking), vLLM 0.19 — suspected interaction with prefix caching | Enable alone, run multi-turn convos; **also re-test on 26.06 (0.22.1)** — may be fixed | ~2× KV capacity → longer context / more concurrency | ⚪ tabled (README "Pending investigation") |
| **Prefix caching** (`--enable-prefix-caching`) — DEF-0007 | `step-3.5-fp8` | Same corruption; suspected FP8-KV × prefix-cache interaction | Enable alone (BF16 KV) → then with FP8 KV | Large TTFT win on shared prefixes (system prompts, multi-turn) | ⚪ tabled |
| **MTP / speculative decoding** (MTP-3) | `qwen3.6-35b-nvfp4-*`; `step-3.7-nvfp4` (native MTP-3) | First-bring-up caution; GB10 forum: MTP causes **image** number-misreads (VL) | Enable **text-only**, A/B single-stream TPS behind fail-safe | **~3× single-stream** on qwen3.6-35b (~28–30 → ~97 tok/s, community) | ⚪ tabled — highest-value |
| **flashinfer attention** (`--attention-backend flashinfer`) | `qwen3.6-35b-nvfp4-*` (community-recommended for GB10) | Not set — vLLM auto-selects | Pin it, A/B | Recommended backend for this model on GB10 | ⚪ candidate |
| **Relax conservative gmu / `max_model_len`** | `step-3.7-nvfp4` (ctx 32768 → "raise toward 131072"); qwen gmu 0.55 | First-bring-up conservatism | Raise once stable; trust vLLM's *estimated max model length* | More context / concurrency | ⚪ per-profile tuning |
| **Full cudagraphs / TP=2 restore on 26.06** — DEF-0003 / DEF-0002 | container-level | 26.06 WARs — cudagraph inference hang ([vllm#40969](https://github.com/vllm-project/vllm/issues/40969)), TP=2 deadlock ([#41725](https://github.com/vllm-project/vllm/issues/41725)) | Restore when upstream fixes land | throughput / latency | ⚪ upstream-gated — see the 26.06 tracker WAR register |

## Consequences

- Performance is recovered **incrementally, with attribution** — each re-enable is
  tied to a test result, so a regression points at exactly one knob.
- **Subsumes** the README "Pending investigation" (FP8 KV + prefix caching) — that
  moves here as the first two register rows.
- **Cross-references** ADR-0012 (benchmark A/B is how throughput knobs are judged),
  ADR-0009 (every re-enable runs behind the fail-safe net), ADR-0011 (a multi-turn
  smoke belongs in the regiment), and the 26.06 container tracker (the container-level
  WARs are the mirror image — *those* re-enable when the upstream bugs close).
- **Highest-value first:** MTP-3 on qwen3.6-35b (~3×, and we're already on the
  recommended Marlin backend) and re-testing FP8-KV/prefix-caching on 26.06 (may be a
  free win now that the bug's container is behind us).
- This is a **living register**: each row updates its status as it's re-enabled
  (kept / reverted, with the measured result).
