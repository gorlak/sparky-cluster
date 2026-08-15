# ADR-0026: Measuring long context — depth as a swept axis, RULER as the method

**Date:** 2026-08-15
**Status:** Proposed

## Context

[`docs/measurement.md`](../measurement.md) names three outcome axes — quality, speed, and the
context length at which a model stays useful. Two have instruments. The third has none:
`max_model_len` is a number we chose, never validated, and the README says so outright —
*"Every profile is offering far less context than it holds."*

Every profile advertises between 131k and 262k tokens. Nobody has measured where any of them
stops being **correct**, only where it stops fitting in memory. Those are different limits
and only one of them has ever been checked.

### The distinction an earlier draft got wrong

An earlier argument held that context should not be permuted at all, on the grounds that
`max_model_len` is not a tuning dial. Half of that is right and it was applied to the wrong
thing:

- **`max_model_len` is a cap, not a dial.** It allocates nothing — KV is sized by
  `gpu_memory_utilization` — and it is bounded above by the model's trained length and by
  KV ÷ desired concurrency. At the fleet's numbers there is 60–200× headroom on that
  second bound. There is no trade-off curve to sweep; the honest value is wherever the model
  stays correct.
- **Context *depth* is absolutely an axis**, and it is a **free** one: how much context a
  request carries varies per request, with no reload. It is among the cheapest axes
  available, and it is the only one that reveals the degradation curve at all.

The interaction is the interesting part. Whether FP8 KV, a given `gmu`, or prefix caching
still holds up at depth is exactly the kind of thing a permutation sweep finds and
hand-tuning misses.

### "The gap" is real and named

Information in the middle of a long context is retrieved worse than information at either
end — the **lost-in-the-middle** effect. It is an architectural property, not a bug in any
particular model, and it persists across the field. It is invisible to any measurement that
does not vary *where* in the context the needed information sits.

## Decision

**Measure context as two dimensions — depth and position — on our own corpus, using RULER's
method rather than RULER's packaging.**

### 1. Depth and position are both swept

Depth (4k / 32k / 128k / 256k, per configuration's cap) and position (where the material the
model needs sits within the padding) are per-request axes. Neither costs a reload, so both
nest inside a single activation and are cheap relative to every other axis in a campaign.

Position is what exposes the gap. A configuration that answers correctly with the material
at the start and fails with it in the middle has a specific, actionable defect.

### 2. Reject needle-in-a-haystack as the instrument

It is the obvious first idea and the published numbers are disqualifying:

- Models achieve **perfect NIAH scores and then fail** the other twelve RULER tasks as input
  grows.
- Synthetic NIAH **overestimates real-world retrieval by 20–40%**.
- **Single-needle overstates production capability by 15–40 points** against multi-needle,
  and real workloads are multi-needle.

A green NIAH chart would tell us a 262k cap is fine when it is not. The failure mode is
worse than having no instrument, because it produces a confident number.

The root cause is that finding a planted sentence is *lexical retrieval* — the question
shares vocabulary with the needle, so it can be solved by matching words. RULER's other
tasks (multi-hop tracing, aggregation, QA) and NoLiMa exist to break exactly that shortcut.

### 3. Take RULER's method: the task taxonomy and the threshold

Two things are adopted outright, and they cost nothing to adopt:

- **The task shapes.** Retrieval alone is not long-context capability. Multi-needle
  integration, multi-hop tracing, aggregation over the whole context, and QA are what
  separate a model that can *use* its context from one that can *search* it.
- **The effective-length convention.** RULER defines effective context as the longest length
  at which a model still scores **≥85.6%**, a threshold anchored to Llama-2-7B's score at 4K —
  its *claimed* length. This is the important borrowing: it converts a degradation curve into
  a single number a profile can carry.

### 4. The in-suite instrument is our own corpus, padded

The corpus already holds tasks with known-correct answers that **are** the downstream work.
Running MMLU-Pro and the coding sets at increasing depth answers the production question
directly — *does this configuration still solve my problems at 100k?*

This matters because of a criticism RULER's own authors raise: they noted the lack of
correlation between synthetic tasks and realistic long-context ones, and subsequent work
(HELMET) found synthetic benchmarks have interpretable failure modes but are **not
predictive of downstream performance**. Padding our own corpus sidesteps that entirely —
there is no proxy, because the tasks are the work.

It is also structurally stronger than NIAH: the thing buried in the padding is not a fact to
find, it is **a problem to solve**. That cannot be answered by lexical matching.

**What the padding is made of is the experiment**, and it must be declared rather than
defaulted:

| padding | measures |
|---|---|
| unrelated filler | distraction resistance |
| related-but-unneeded material | discrimination — can it tell signal from plausible noise |
| the needed material at varying offsets | position sensitivity — the gap |

### 5. RULER itself is an occasional external cross-check, never vendored

Run through its **`openai` backend**, RULER is a pure HTTP client: it generates prompts,
posts them, scores replies. Pointed at the stable model endpoint it needs no container
access and no privilege — the same property that makes the rest of the harness agent-drivable.

That is the only supported way to run it here, and it is worth doing occasionally: it gives a
number comparable to a published methodology, which our own corpus by construction cannot.

**It is not vendored, and not installed by a deploy.** The reasons are concrete:

| | |
|---|---|
| prebuilt image `cphsieh/ruler:0.2.0` | **amd64 only** — this cluster is `aarch64` |
| its base image | `nvcr.io/nvidia/pytorch:23.10-py3`, from October 2023 — predates CUDA 13 and sm_121 |
| dependencies | flash-attention, plus reported conflicts (`nemo-toolkit`, `huggingface_hub`) |
| licence | **none stated** — *"strictly for research purposes, and not an official product from NVIDIA"* |

Building that stack on GB10 is the same fight as the CPU-only torch wheel, and vendoring
unlicensed code into a public repository is a separate problem. Taking the method costs
nothing and carries neither.

### 6. `max_model_len` is set from the measurement, not swept

Once effective context is measured, the cap follows from it. A profile advertising more than
its measured effective length is making a promise it does not keep — and the failure is the
one this cluster has already documented for vision: *"a subject held at ~1% of the frame gets
a confident wrong answer rather than a refusal."* Capability advertised beyond where it was
validated fails silently, which is the worst way to fail.

## Consequences

- **The third axis gets an instrument**, and `max_model_len` stops being a guess. Expect the
  honest numbers to be lower than what is advertised today: published results show most
  models losing **15–30% accuracy between 4K and 128K**.
- **Two cheap dimensions join the sweep.** Depth and position cost no reload, so they
  multiply campaign length far less than any configuration axis.
- **Some profiles will have to lower their advertised context**, which is a visible
  reduction in a headline number and the right thing to do anyway.
- **A new external dependency, deliberately kept at arm's length.** RULER is a thing we run
  occasionally by hand, not a thing the cluster installs. If its `openai` backend ever stops
  working, nothing in the suite breaks.
- **Corpus results become depth-qualified.** A quality score without a stated depth stops
  being meaningful, so the trend store must record it — otherwise two rows measured at
  different depths look comparable and are not.

## Alternatives rejected

**Needle-in-a-haystack as the instrument.** Disqualified by its own published numbers (§2).
Retained only as one shape among RULER's thirteen, never as the measurement.

**Vendor RULER and run it in-suite.** The architecture is wrong (amd64), the base image is
three years stale against this hardware, and there is no licence. Its `openai` backend gives
us the useful part with none of that.

**Sweep `max_model_len` as a configuration parameter.** It is a cap, not a dial: it allocates
nothing and has no trade-off curve. Sweeping it would measure its cost and never its benefit.

**Skip context until the rest of the sweep is proven.** This was the earlier position and it
was based on the wrong reading of what the axis is. Depth is free and its instrument is our
existing corpus with padding — cheaper than most of what is already planned.

## References

- [`docs/measurement.md`](../measurement.md) — the three axes and what each metric may be compared against
- [ADR-0024](0024-coding-measurement.md) — the corpus this pads, and §8's rule that efficiency is measured beside correctness rather than folded into it
- [ADR-0016](0016-continuous-evaluation-outer-loop.md) — the outer loop a depth sweep runs inside
- [RULER: What's the Real Context Size of Your Long-Context Language Models?](https://arxiv.org/html/2404.06654) · [NVIDIA/RULER](https://github.com/NVIDIA/RULER)
