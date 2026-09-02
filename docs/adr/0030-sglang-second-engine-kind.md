# ADR-0030: SGLang as a second reconciler-managed engine kind

**Date:** 2026-08-30
**Status:** Accepted — SGLang joins the fleet as a permanent second engine kind, a
standing tool for day-1 models vLLM cannot yet serve on GB10 (not a one-model expedient).

**Build status (2026-08-30):** Phases 1–2 landed — the `sglang@.service` unit + `sglang`
role render surface, and the kind-aware reconciler — with 637 tests green and lint clean,
and **no fleet change** (no `kind: sglang` profile deployed yet, so a deploy renders
nothing new). Phase 3 (the image) and Phase 4 (profiles + attended bring-up) follow.
Bring-up order:

1. **qwen3.6-35b-a3b-nvfp4** — already in hand, so it is the *first* SGLang bring-up: a
   vLLM-vs-SGLang A/B on identical NVFP4 weights, and a lower-risk standard-MoE arch that
   proves the engine on real weights before the exotic target.
2. **RadixArk/Qwen3.8-Flash-Next-NVFP4** — the target (`qwen4_exp`); needs the SM121 QSA patch.

**Llama-3.1-8B-FP8** is staged but held in RESERVE, not a bring-up step: a pure-stock,
single-node infra control to fall back on only if a bring-up misbehaves in a way we cannot
otherwise isolate. The bet is we will not need it.

## Context

Qwen3.8-Flash-Next (125B-A6B, vision, `qwen4_exp`) is the strongest flagship candidate we have
found — general **and** multimodal in one model
([`docs/models/candidates/qwen3.8-flash-next.md`](../models/candidates/qwen3.8-flash-next.md)).
But **vLLM cannot serve it on GB10**: the arm64 image does not register
`Qwen4ExpForConditionalGeneration`, and the SM121 QSA sparse-decode path fails in warmup. The
vLLM route is a source build + toolchain recompile ([vLLM #31128](https://github.com/vllm-project/vllm/issues/31128),
[#36821](https://github.com/vllm-project/vllm/issues/36821)) — heavy *and* unproven for this arch
at TP=2.

**SGLang serves it, proven at our exact hardware.**
[tonyd2wild](https://github.com/tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark) ran it on **2× DGX
Spark, TP=2, NVFP4, ~50–70 tok/s** via a one-line SM121 QSA guard and an existing patched image.
[ADR-0029](0029-model-sourcing-strategy.md) already established that off-the-shelf is a convenience,
not a gate. This ADR takes the next step it did not: a *serving engine* we do not already run.

## Decision

**Add SGLang as a second engine `kind`, managed exactly like a vLLM engine** — reconciler-driven,
activatable, one-live-at-a-time, behind the ADR-0009 fail-safe boot gates. **Not** the persistent
ollama model: this engine is a flagship the fleet activates and measures, so it must be a
first-class member of the activation machinery.

The schema already anticipates this — profiles carry `kind:`, and today it admits `vllm` and
`ollama`. `kind: sglang` is the third. Five pieces change, and the split between generic and
engine-specific is the whole reason the cost is bounded:

### 1. A `sglang@.service` template unit — copy the generic 80%, swap the 20%
The `vllm@.service` boot gates (the two `ConditionPathExists`), the `.running` marker lifecycle,
the docker harness, the model/NCCL mounts, and the TP=2 timeouts are **engine-agnostic** and are
reproduced verbatim — the ADR-0009 fail-safe generalises to SGLang unchanged. Only the launch line
differs: `${SGLANG_IMAGE} python -m sglang.launch_server --model-path /models/${ENGINE_MODEL}
$SGLANG_SERVE_ARGS` in place of the `vllm serve` invocation, with `SGLANG_*` env names.

### 2. A `sglang` role — the env projection
Parallel to the `vllm` role: render `<engine>.env` / `.docker-env` and the unit from the profile's
`serving_topology`, projecting SGLang's flags (`--tp`, `--mem-fraction-static`, the KV/mamba pool
sizes, `--ple-offload-embedding`, the tool/reasoning parsers) instead of vLLM's. Same "one flat
diffable file per engine" property (README's projection model).

### 3. The reconciler becomes kind-aware — the one safety-critical change
Every `systemctl` call already routes through `unit_of(engine)`. It gains the kind:
`unit_of(engine, kind) → f"{kind}@{engine}.service"`, with `ENGINE_KIND` carried in the env file the
reconciler already parses. **The decision logic — markers, the desired/cleanly-stopped gates, the
start/stop plan, fail-to-`empty` — is untouched and stays engine-agnostic.** This is the change that
gets the full ADR-0011 Layer-3 unit-test treatment before it ships.

### 4. The `images` role gains the SGLang image
Built/patched by us, the ADR-0029 way — the SM121 QSA guard on top of an SGLang base (the
`radixark/…:sm121-qsa` recipe is the reference). A `docs/upgrades/container-*` tracker carries it.

### 5. Smoke is unchanged; memory is re-derived
The smoke gate probes the **OpenAI HTTP API** (`api_url`) — readiness, the tool-call shape,
multiturn, vision — so it gates an SGLang engine as-is. **Memory does not transfer:** SGLang's
`--mem-fraction-static` and separate KV/mamba pools have different accounting than vLLM's
`--gpu-memory-utilization`, so the ADR-0028 headroom floor is re-established empirically for
`kind: sglang` at bring-up (tonyd2wild's pinned 600k KV pool and `--max-mamba-cache-size 97` are the
starting point, not the answer).

## Consequences

- **The fleet becomes a two-engine shop.** Two images to patch, two memory models to reason about,
  two projection roles. The **OpenAI API is what bounds the blast radius** — the stable endpoint,
  Open WebUI, Prometheus, and the bench/eval harness all speak HTTP and are untouched.
- **The activation machinery generalises rather than forks.** Because SGLang reuses the boot gates,
  the marker lifecycle, and the reconciler's decision logic, "a new engine kind" is a unit template
  + a role + a kind-aware `unit_of` — not a parallel control plane. That is the payoff of keeping it
  reconciler-managed instead of ollama-style.
- **ADR-0028 safety is non-transferable and must be redone for SGLang** — the one place the
  generalisation genuinely stops. Recorded so it is not assumed.
- **`kind: sglang` unlocks more than one model.** Any future model SGLang serves and GB10 vLLM does
  not rides this seam.
- **Engines never co-serve — the one-live invariant spans kinds.** The one-front-port rule
  (ADR-0018, `lint`-asserted) is per port fleet-wide, so a `vllm@` and an `sglang@` engine
  cannot be live at once — they would contend for `:8000`. Comparing two engines for the
  *same* model is therefore **sequential**: two profiles (the bare vLLM name and a
  `-sglang` twin), activated in turn, with the scoreboard comparing the recorded runs —
  the same shape as a config A/B. There is deliberately no side-by-side path, so the
  static stable endpoint and Caddy's model-agnostic upstream are untouched by the second
  kind: whichever single engine is live answers on `:8000` as `sparky`.
- **"HTTP-native and untouched" held for the SCRAPE, not the QUERIES (found 2026-08-31).** The
  claim above — Prometheus and the harness are untouched — is true at the transport: Prometheus
  scrapes the one live engine model-agnostically, and the bench regiment is HTTP-native, so both
  ran against sglang with no change. But two consumers read ENGINE-SPECIFIC metric *names*
  underneath: the Grafana panels queried `vllm:*` (blank for sglang until `or`-ed with the
  `sglang:*` peer), and bench's context probe read `vllm:cache_config_info` (sglang publishes KV
  as `sglang:max_total_num_tokens`). Both fixed. The general rule it produced — a surface that
  spans engine kinds must use metrics every kind emits with identical meaning, or leave the
  metric off — is recorded in `skills/development/SKILL.md`.

## Alternatives considered

- **vLLM source build for GB10.** Keeps one engine, but registering `qwen4_exp` + the SM121 kernels
  is a from-source toolchain recompile, unproven at TP=2 for this arch. Heavier and riskier than
  adopting SGLang's *proven* recipe.
- **vLLM single-node** ([blazux](https://github.com/blazux/qwen3.8-Flash-DGX), the n-gram-mmap image)
  — proven and one-engine, but ~half the speed (~26–31 tok/s) and it leaves a node idle, which the
  fleet priority counts as a cost. Kept as the fallback if SGLang integration stalls.
- **ollama-style persistent engine.** Wrong shape: a flagship must be activatable and measurable,
  one-live-at-a-time under the fail-safe — not a always-on sidecar.

## Build plan (phased, each independently testable)

1. **`sglang@.service` + `sglang` role** — render tests (the boot gates, the env round-trip), no hardware.
2. **Reconciler kind-awareness** (`unit_of(engine, kind)`, `ENGINE_KIND`) — ADR-0011 Layer-3 unit tests for the plan under a mixed vllm/sglang allowlist.
3. **`images` role + container tracker** — the patched SGLang image.
4. **The Qwen3.8-Flash-Next `kind: sglang` profile** + weights, then an attended TP=2 bring-up: smoke, the SGLang memory floor, an exercise. Promote the candidate sheet.

## References

- [ADR-0029](0029-model-sourcing-strategy.md) — off-the-shelf isn't a gate; this is its first non-vLLM application
- [ADR-0018](0018-provision-select-split.md) / [ADR-0009](0009-fail-safe-boot.md) — the activation + fail-safe model SGLang joins unchanged
- [ADR-0028](0028-unified-memory-oom-headroom.md) — the headroom floor that must be re-derived for SGLang
- [ADR-0011](0011-functional-tests.md) — the Layer-3 reconciler tests the kind-aware change earns
- [`docs/serving-topology.md`](../serving-topology.md) — the `kind:` field and the per-kind role pattern
- [tonyd2wild](https://github.com/tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark) — the proven 2× DGX Spark TP=2 SGLang recipe
