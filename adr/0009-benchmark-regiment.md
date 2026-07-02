# ADR-0009: Benchmark regiment design

**Date:** 2026-07-02
**Status:** Accepted

## Context

The existing `benchmark/run.sh` measures throughput and latency well but has
two gaps that matter as the cluster evolves:

1. **No quality verification.** It measures speed but cannot detect the
   multi-turn output corruption seen with `--kv-cache-dtype fp8` on
   Step-3.5-Flash (garbage/nonstop thinking tokens after turn N). A
   throughput benchmark would have passed while the model was producing
   unusable output.

2. **Hardcoded to one model.** `MODEL_PATH` and `SERVED_NAME` are literals in
   the script. Comparing Step-3.5-Flash-FP8 against Step-3.7-Flash-NVFP4
   requires editing the script, which defeats the purpose of a comparison tool.

Additionally, the investigation of `--kv-cache-dtype fp8` (required for NVFP4)
and the rollout of NVFP4 itself both need a systematic validation harness, not
ad-hoc manual testing.

## Options considered

**A. Keep existing benchmark, add manual multiturn testing**
Manual testing is not reproducible and won't catch regressions passively.

**B. One unified benchmark run that does everything**
Run the full battery (latency + throughput + prefix cache + multiturn quality)
on every deploy. A full run is 5–20 minutes per model — unacceptable as a
deploy gate.

**C. Two-mode regiment: smoke test (post-deploy) + full run (on demand)**
- **Smoke test** (`--smoke`): multiturn quality check only. Fixed 8-turn
  English conversation with heuristic corruption detection. ~2 minutes. Runs
  at the end of `site.yml`; skips if no API is up (empty profile). Blocks
  deploy completion and exits non-zero on failure.
- **Full run** (manual or scheduled): latency + throughput + prefix_cache +
  multiturn. Run when you care about trend numbers — post driver update, post
  container bump, post new model deploy. Pushes results to SQLite (see
  ADR-0010). Scheduled weekly on Sunday at 04:00 via a systemd timer on sparky.

## Decision

Two-mode regiment (option C).

## Quality check heuristics

The multiturn check detects the specific failure modes observed on this cluster:

- **CJK bleed:** >30% non-Latin characters in any response to an English prompt
  indicates multilingual garbage output.
- **Thinking token loop:** >200 consecutive `<think>` tokens without a closing
  tag indicates the nonstop-thinking failure mode.
- **TPOT spike:** inter-token latency in turn N is >10× the baseline from
  turn 1 (indicates the model is spinning rather than generating).

A pass/fail result is written to the benchmark log and to the SQLite results db
(as `quality_pass` boolean).

## Weekly schedule

The systemd timer fires Sunday at 04:00 local time on sparky. If the vLLM API
is not responding at `/health` when the timer fires, the run exits 0 (silent
skip) and writes a `benchmark_skipped=1` row to SQLite so the gap in the
Grafana trend is distinguishable from a missed push.

## Consequences

- Every deploy gets a lightweight quality gate with no meaningful time cost.
- Throughput regressions from driver updates, container bumps, or config
  changes are caught passively via the weekly run without any operator action.
- The benchmark is model-agnostic: `run.sh <label> <model-path> <served-name>
  [--smoke]` works against any running vLLM endpoint.
- The quality heuristics are conservative (false-negative tolerant). They will
  not flag mild quality degradation; they detect the catastrophic failure modes
  actually observed. Tuning them requires observed data from future failures.
- The weekly timer depends on sparky being up Sunday morning. If sparky is
  rebooting or in a maintenance window, the run is skipped (silent skip as
  above).
