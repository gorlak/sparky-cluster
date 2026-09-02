# Measurement

What we measure, how a **run** produces it, and what it is for. The
arguments behind each choice live in the ADRs and are not repeated here:
[0016](adr/0016-continuous-evaluation-outer-loop.md) the outer loop ·
[0024](adr/0024-coding-measurement.md) the coding corpus and sandbox ·
[0026](adr/0026-long-context-measurement.md) long context ·
[0018](adr/0018-provision-select-split.md) why an agent may activate but not provision.

The premise: there are **N models × M configuration parameters**. Those parameters move how
well a model performs in each domain we care about, and how fast it delivers. Production
configurations are chosen by permuting that space and reading the results, not by reasoning
about it.

---

## What we measure

### Quality, by domain

| domain | instrument |
|---|---|
| general knowledge and reasoning | MMLU-Pro |
| writing correct code | the coding sets — execution-scored, not judged ([ADR-0024](adr/0024-coding-measurement.md)) |
| holding a long context | the corpus padded to depth, with position varied ([ADR-0026](adr/0026-long-context-measurement.md)) |

**Domains are scored and recorded separately, and not conflated by default.** A model may be
excellent at one and poor at another, and that is a useful result rather than a problem to
average away: switching the serving model to match the task is a routine, unprivileged
operation, so the fleet does not need a model that is good at everything.

Combining domains afterwards is a legitimate analysis and stays available. Combining them
*at collection* does not, because it cannot be undone — so the data is kept disaggregated
and any roll-up is a downstream choice made deliberately.

Two questions fall out of keeping them apart:

- **which model is best at each domain** — what to activate for a given kind of work
- **which models are adequate at all of them** — whose value is specifically that they
  avoid a switch

Sets still version and deploy as one artifact; that is packaging convenience and implies
nothing about how they are scored. Scoring stays per-set in any case — answer-matching and
execution are not interchangeable.

**Every result records what it is comparable to** — the corpus version, the exact
configuration that produced it, and the depth it was taken at. A measurement also describes
exactly one configuration: it begins after the engine is ready and never spans an
activation.

### Speed

Derived from the quality runs rather than from a separate synthetic load corpus, so the
numbers describe work we actually care about.

| number | corrects for | valid for comparing |
|---|---|---|
| **TTFT** | — | anything |
| **`output_toks_s`** | nothing — raw decode rate | **config vs config**, same model |
| **`total_toks_s`** — (prompt+output)/elapsed | prefill cost at depth | long-context configs |
| **goodput** — *answer* tokens/elapsed | verbosity — thinking is cost, not credit | **model vs model** |

**The metric is chosen by the comparison.** Within one model verbosity is constant, so raw
decode rate isolates what the config did. Across models it is confounded — a model that
spends 2,700 tokens thinking and 300 answering posts a fine rate while being slow to be
useful.

**Timing is measured at concurrency 1**, the condition we serve at. Correctness is
concurrency-independent and may run parallel.

### Efficiency

**Tokens per correct answer.** Conditioned on correctness, so being terse and wrong scores
nothing.

**Reported beside the score rather than folded into it.** Thinking tokens are what buy a
reasoning model its correct answers, and whether 90%-at-3,000-tokens beats
60%-at-300-tokens is a decision about what this cluster is for. Weighting the two together
is a downstream choice, available whenever it is wanted — the measurement does not settle it
in advance.

---

## What a profile declares

A profile is one model configured one way. The **core** is what serves in production —
hand-authored, reviewed, and expected to be the best configuration we currently know of for
that model.

```yaml
profile_name: qwen3.6-35b-a3b-nvfp4
serving_topology:
  engines:
    - memory_fraction: 0.80
      kv_cache_dtype: fp8
      tensor_parallel_size: 2
```

**Anything we intend to permute is a named property.** Some serving options are carried today
as raw strings in an argument list; such a parameter is promoted to a property before it can
be permuted. That is the price of admission, and it is deliberate — it keeps the permuted
surface explicit and typed rather than turning a variant into string editing.

### Profile variants

A **profile variant** is the same model at different settings. Variants exist so a run
has something to compare — and they stay activatable afterwards, so a human can reach for
one deliberately when a specific question comes up.

```yaml
variants:
  memory_fraction: [0.70, 0.80, 0.88]
  kv_cache_dtype: [auto, fp8]
```

Each key names a property of the core; each list is the levels to try. The core's own value
need not appear — it is measured anyway, as the baseline.

Only settings that require a **reload** belong here. Anything varying per request —
temperature, context depth, position, concurrency — is varied inside a single run and needs
no profile of its own.

### How that realizes

One **deploy** — password-gated, human, once per run — expands the block into the
cross-product: the core, plus one variant per cell, each with its own env file and each
separately **activatable**.

Names are flattened from the values that produced them, so a cell always resolves to the
same name:

```
qwen3.6-35b-a3b-nvfp4                     the core — what serves
qwen3.6-35b-a3b-nvfp4@gmu0.70-kvauto
qwen3.6-35b-a3b-nvfp4@gmu0.70-kvfp8
qwen3.6-35b-a3b-nvfp4@gmu0.88-kvfp8       …six cells for the block above
```

A generated name has three jobs: **deterministic**, so a result joins back to the
configuration that produced it; **unique fleet-wide**, which the engine allowlist requires;
and **legible**, so a scoreboard row can be recognised without decoding it.

From there a run activates its way through them with no privilege, unattended, for as
long as it takes. The expensive human step happens once per run rather than once per
cell.

**Winning settings are promoted into the core.** A variant that measures better is not left
running as the answer — its values move into the profile's own configuration, and the core
becomes the better thing. Defaults are therefore expected to shift over time as models are
tuned, and each shift should be traceable to the run that justified it.

---

## What we expect to learn

**Which model to activate for which kind of work.** A per-domain ranking, so a specialist
is judged on the thing it is for — and, separately, which models are good enough across
domains to be worth leaving up.

**Which configuration to run each model at.** The settings that most affect serving — memory
split, KV dtype, parallel shape — are chosen from measured effect rather than from
arithmetic about what should happen.

**An honest context number per model**, so the advertised cap is one the model has been shown
to hold.

**Where a configuration is fragile.** A setting that measures well at shallow depth and falls
apart at long depth, or with the needed material in the middle rather than at the edges, is a
defect worth finding before a user does.

**What the fleet is bad at.** Per-problem pass rates across every model make "grow the set
where models actually fail" a question the data answers.

Domains conflict with each other and quality conflicts with speed, so what comes out is the
set of configurations not beaten on everything at once. Picking from that set is a judgement
about what this cluster is *for*, and it stays with the operator.
