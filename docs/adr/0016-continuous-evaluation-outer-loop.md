# ADR-0016: Continuous model-evaluation outer loop (human-authored, agent-driven sweeps)

**Date:** 2026-07-27 (scoped 2026-07-29: the deploy/activate mechanism and authorization
model moved to ADR-0018; this ADR keeps the loop, the sweep representation, and the eval
regiments. Revised 2026-08-03, while Proposed: the CI-style matrix DSL was dropped for a
flat, literal job list — see Options. Revised 2026-08-06, while Proposed: the `bench`
regiment is **rebuilt here**, HTTP-native, rather than inheriting today's container-bound
one — see ADR-0018's errata for why that hole exists and why it is left open. Revised
2026-08-08, while Proposed: **`vision` and `tools` added as first-class regiments** —
the 2026-08-08 bring-ups showed both are load-bearing capabilities that nothing scored,
and three of four failures that evening were in the vision path. Same revision: the
**tuning knobs are named as the variant axis `soak` exists to validate**.)
**Status:** Accepted

## Context

The cluster serves the **smartest model that fits**, per tier, and must keep that current as
models release (fleet-priority north star: Tier-1 = one model TP=2 fully-committed across both
nodes; secondary tiers experimental). "What's best right now" is a **standing, measured
question** — model discovery says which candidates *might* win; only deploying and measuring
says which *does*.

Today that measurement is manual and hand-held: `activate` a model, wait, `bench`, read
numbers, repeat. The MTP-3 A/B on `qwen3.6-35b` (2026-07) was the representative prototype —
and it doesn't scale to "evaluate every candidate we acquired, at its best, across two tiers."

The substrate is built: `sparky` is a programmable primitive (ADR-0015); fail-safe boot
(ADR-0009) makes an unattended bad bring-up recoverable; the smoke gate + bench regiment +
label-keyed trend store (ADR-0012) are the correctness and throughput halves of "how good is
it"; images are reproducible (ADR-0013). And **ADR-0018** gives the loop what it needs to run
without per-step hand-holding: `deploy` (human, convergent provisioning) + `activate` (the
unprivileged, agent-drivable operation that swaps the live model), with the security boundary
that makes agent-driven activation safe.

**What's missing is the loop itself:** a way to *represent* an evaluation phase, the missing
*quality* signal (we have corruption-smoke + throughput bench, but no MMLU-Pro/LiveBench-class
number), and a runner that executes a phase unattended.

## Options considered

**A. Stay manual.** A human drives every activation and reads every number. Zero new surface,
but it keeps fleet-currency an open-ended chore and wastes the fail-safe/smoke/trend substrate
built to make unattended runs safe. Remains the fallback; rejected as the end state.

**B. Bespoke scripts per evaluation.** Write a one-off for each round. Fast to start, but no
shared representation, no resume, no reuse across rounds — every phase reinvented. Rejected.

**C. A declarative sweep — a flat, human-authored job list, agent-driven (chosen).**
The phase is *data* — an explicit list of `(profile, variant, regiments)` jobs — that a human
authors, reviews, and kicks; the agent runs it to completion, recording as it goes. Planning
is forced up front; execution is uniform and resumable. An earlier draft specified a CI-style
*matrix* (`matrix × exclude × needs_tag` → an expanded job list); it was dropped: at this
cluster's scale a sweep is a dozen-odd jobs, and a reviewer checks a dozen literal lines more
accurately than they mentally expand a matrix through exclusion rules — and the expander, its
filtering DSL, and their tests all vanish. If sweeps ever outgrow enumeration, a matrix
front-end can be reintroduced *behind* the same flat job-list representation the runner
consumes.

## Decision

**Option C.** Build a continuous model-evaluation outer loop — a human-authored, agent-driven
sweep that measures **quality vs. performance** per tier.

### The sweep as a flat job list (profile × variant × regiment, enumerated)
```yaml
name: candidate-eval-r1
defaults:                        # merged into each job — the one non-literal convenience
  regiments:
    - smoke
    - bench: [latency, throughput, prefix]
    - quality: [mmlu-pro, livebench]
    - vision                       # n/a for text-only models; never a failure
    - tools                        # all four tool_choice shapes, not just liveness
    - soak: {minutes: 10}
jobs:                            # explicit, reviewed as-is — what you read is what runs
  - profile: mistral-medium-3.5-128b-nvfp4    # DEF-0002-risky TP=2/26.06 → carries the long soak
    variant: base
    regiments: [smoke, {bench: [latency, throughput, prefix]}, {soak: {minutes: 45}}]
  - {profile: mistral-medium-3.5-128b-nvfp4,   variant: fp8-kv+prefix}
  - {profile: nemotron-puzzle-75b-single, variant: base}
  - {profile: nemotron-puzzle-75b-single, variant: mtp}
```

- **Sequential by activation, unlike CI — a two-node fact, not a design commitment.** Only one
  model can be live on two nodes, so the job list runs as a serial loop — each job is one
  `activate` (ADR-0018), regiments run against the live model (cheap) — with durable
  **breadcrumbs** to resume after an interruption and **quarantine** for a node-killer (mark,
  skip, continue). The runner is a short loop in the harness (ADR-0015) — which is also why
  the sweep is *flat data + Python control flow*, not a DSL: if the fleet grows past two
  nodes, the natural upgrade is parallel evaluation of single-node candidates on free nodes —
  a small scheduler, i.e. control flow, miserable to express in YAML and natural in the
  already-programmable primitive.
- **Optimization A/Bs are just paired rows** — no special machinery; since the trend store
  keys by label, comparison falls out for free (`report base mtp`). The ADR-0014 register
  becomes "add a row."
- **Regiments are pluggable:** `smoke` exists as-is; `bench` is **rebuilt** (below);
  `quality` and `soak` are new.
  - **`quality`** — a standardized eval against the endpoint (recorded next to the bench
    numbers). Note the cost: full MMLU-Pro/LiveBench is *hours per variant*, impractical across
    a sweep — so it runs a **subset / fast proxy**, sized deliberately, not the whole suite.
  - **`soak`** — a hold that catches instability a clean bring-up misses. **Default 10 min**;
    but the DEF-0002 deadlock class strikes **35–55 min in**, so 10 min *cannot* catch it — a
    longer soak (45–60 min) is a **per-candidate knob** for the DEF-0002-risky TP=2/26.06
    profiles. A blanket 10 min would pass slow deadlocks through the gate.

    **Soak is where the tuning knobs are decided**, because every one of them fails
    *late* — which is exactly why they cannot be settled by a bring-up or a bench run:

    | Knob | What it buys | How it fails, and when |
    |---|---|---|
    | **speculative decoding** (MTP-n) | 2.3× single-stream decode (ADR-0014) | corrupts image **number-reads** (vision regiment) and breaks constrained decoding (DEF-0011) — both at generation time, never at load |
    | **KV cache dtype** (FP8 vs bf16) | ~2× the KV, so ~2× concurrency | DEF-0007 multi-turn corruption — by construction only on the **Nth** turn |
    | **prefix caching** | large prefill savings on repeated context | DEF-0007's other half; suspected to interact with FP8 KV specifically |
    | **`max_model_len`** | usable context | OOM under *concurrency* rather than at load; and quality decay at heavily extrapolated rope (Mistral-Medium is YaRN **×64** from 4096) |
    | **`gpu_memory_utilization`** | KV budget vs dev headroom | OOM under load — and at the extreme, a host-memory exhaustion that takes the **node** down (DEF-0004) |

    So each is a **variant row** paired against `base`, with a soak long enough for its own
    failure mode, and the trend store makes the comparison free (`report base fp8-kv`).

    **A finding that changes the baseline (2026-08-08):** vLLM *auto-enables* FP8 KV when a
    checkpoint declares `kv_cache_quant_algo` — Nemotron came up with `kv_cache_dtype=fp8_e4m3`
    without the flag being set anywhere. So "FP8 KV is disabled" is **false** for every
    modelopt checkpoint in the fleet, and ADR-0014's register describes a state we are not
    in. The knob is being set by the file, not by us; the sweep has to measure what is
    actually running rather than what the profile omits.
  - **`vision`** — can it *see*, and does it see **correctly**? Every model staged since
    2026-08 is vision-capable and none had its vision path exercised, so the capability was
    entirely unverified. Two levels, and the gap between them is the point:
    - the **gate** (built 2026-08-08, `sparky/vision.py`): one generated image, count the
      shapes, pass/fail on every activation. It asks *counting* rather than *describing*
      because ADR-0014 found MTP corrupts image **number-reads** while leaving prose
      plausible — a "describe this" check would pass a model whose vision is quietly wrong.
    - the **regiment**: a scored set (counting, colour, text-in-image, chart-reading) run
      like `quality`, so vision is comparable across candidates rather than merely present.
      A text-only model scores `n/a`, never a failure.

    The 2026-08-08 evidence for making this first-class: `mistral-medium-3.5` (DEF-0012)
    and `step-3.7-flash-nvfp4` (DEF-0006) both **fail before serving** on a vision processor, and
    `qwen3.6-35b` serves vision *today* while being deliberately run text-first. None of
    that was visible to any regiment.
  - **`tools`** — does tool calling actually work, across the shapes callers send? Not the
    smoke gate's liveness check (`tool_choice: "auto"` returns 200) but a scored regiment:
    all four shapes (`none` / `auto` / `required` / named function), argument fidelity
    against a schema, and multi-tool selection. This is what makes a model usable as an
    *agent* rather than a chatbot, and it is the capability that decides whether the
    cluster's models can search the web themselves rather than having results pasted into
    their context.

    It earns first-class status by having broken three separate ways in one evening:
    DEF-0010 (an xgrammar version below vLLM's own floor → **every** tool call 500s, which
    breaks ordinary chat because Open WebUI sends `auto`), DEF-0011 (MTP breaks constrained
    decoding, so `required` and named-function fail while `auto` passes), and a bring-up
    lost to a **guessed `--tool-call-parser` name**. A regiment that exercises all four
    shapes catches the first two; `sparky probe parsers` (ADR-0019) prevents the third.
  - **`bench`** — **rebuilt HTTP-native**, against ADR-0018's fixed model endpoint, with
    input lengths controlled from the model's `tokenizer.json` (readable on disk, no
    privilege). Today's regiment shells `sudo docker exec … vllm bench serve` inside the
    engine's container, which costs the sweep two things it cannot pay: **root** (ADR-0018
    retired the passwordless `docker` grant that made it free) and **head-locality** —
    bench currently refuses every single-node profile, so it cannot measure the model that
    has actually been serving. Both are accidents of `vllm bench serve` living in the
    container, and both dissolve against a stable endpoint rather than needing to be
    solved. The reasoning, and the fidelity risk that could still overturn it, are recorded
    in **[ADR-0018's errata](0018-provision-select-split.md#errata-2026-08-06--bench-is-knowingly-left-in-a-hole)**.
    *Node-aware benching* — previously listed here as a prerequisite — is retired by the
    same move: against a fixed endpoint the question does not arise.

### How it runs (mechanism → ADR-0018)
The human authors + kicks a sweep by **adding its `(profile × variant)` set to the allowlist
and running `deploy`** (convergent, whole-fleet, password-gated — the out-of-band
authorization). The agent then **`activate`s** across the deployed set, running regiments and
recording — no privilege, no per-step hand-off. A sweep **commandeers the cluster**: only one
model is live at a time, so the human-facing serving (Open WebUI / the stable endpoint) is
**suspended for the sweep's duration** and restored on completion — no collision between eval
traffic and chat. The deploy/activate control model, the reconciler, the stable serving surface,
and the authorization/trust boundary are all in **[ADR-0018](0018-provision-select-split.md)**.

## Consequences

- **The platform, not a chore.** Candidate evals + the ADR-0014 optimization register run as one
  kicked sweep instead of dozens of hand-offs. The fleet-orchestrator north star realized.
- **Planning is designed-in.** Authoring the job list forces up-front reasoning about which models,
  variants, and regiments apply.
- **Reuse over rebuild.** The trend store gives free comparison; ADR-0018 gives deploy/activate;
  the discovery sweeps feed the queue; ADR-0013 builds-to-unblock candidates.
- **New build surface:** the sweep runner (job-list validation, breadcrumbs, quarantine — no
  expander; jobs are literal); the quality-eval harness; the soak monitor; and the
  **rebuilt HTTP-native `bench` regiment** (above), which is what takes `bench` out of the
  hole ADR-0018 knowingly left it in. (The control model — reconciler, stable endpoint,
  no-sudo `activate` — is ADR-0018's build.)
- **Relationships.** Depends on ADR-0018 (the how) and ADR-0012 (bench + trend store); stands on
  ADR-0009 (safe unattended bring-up) and ADR-0015 (programmable primitive); operationalizes
  ADR-0014 (runs its A/Bs) and the model-discovery sweeps.

## Test plan (following ADR-0011's layered regiment)

- **Layer 2/3 (no hardware):** **job-list validation** (every job names an allowlisted
  profile, a known variant knob, and known regiments — anything unknown is rejected before
  the first `activate`; `defaults` merge correctly, per-job overrides win); the
  **quarantine/resume** breadcrumb logic (a node-killer is marked and skipped; a sweep
  resumes at the right job after an interruption).
- **Integration:** a 2-job dry sweep on already-deployed profiles — `activate` → smoke → bench →
  record → next — asserting the trend-store rows and resume-after-interrupt.
- **Per regiment, as built:** `soak` — a hang is detected within the window (and the 10-min
  default provably *doesn't* catch a >35-min stall, so DEF-0002-class candidates carry the
  longer knob); `quality` — the subset harness records a score against the endpoint;
  `bench` — **the fidelity spike gates the rebuild**: TTFT / ITL / throughput reproduce from
  a streaming response plus `stream_options.include_usage`, and the two genuinely uncertain
  parts — Poisson arrival shaping and the prefix-cache scenario's token-exact shared
  prefixes — are demonstrated before the container-bound method is retired. If either needs
  the container after all, the fallback is a bounded privileged trigger on the
  `vllm-activate` pattern, and *that* is a boundary decision needing its own ADR, not a line
  item here.

Status flips to **Accepted** when a first human-kicked sweep runs end to end (deploy the
set → activate → soak → eval → bench → record → next, with quarantine). Build/progress tracking
lives in README / TODO, not this status.

---

## Errata — 2026-08-10, on acceptance: what shipped, and two things that did not

Accepted with the runner built (`sparky/sweep.py`, `./sparky.sh sweep <spec>`). Recording
the deltas here rather than editing the decision above, so the original reasoning stays
readable.

### The `variant` axis was dropped — ADR-0018 removed the thing it varied

The job list was specified as `profile × variant × regiment`, where a variant was a flag
set (`variant: fp8-kv+prefix`). **That cannot exist.** ADR-0018 moved serve flags into
deploy-rendered engine env files, so changing one is a privileged, password-gated deploy —
precisely what an unattended agent-driven sweep cannot do. This ADR was written first and
scoped afterwards; the axis is a leftover from before the split.

A variant IS a profile. That is already how we express an A/B: `qwen3.6-35b-a3b-nvfp4`
beside `qwen3.6-35b-a3b-nvfp4-mtp3-single`, one variable apart, compared by label in the
trend store. The ADR's own claim that "optimization A/Bs are just paired rows" survives —
it is the *rows* that are paired, and a row is a profile.

### `livebench` was dropped from the `quality` regiment

Specified as `quality: [mmlu-pro, livebench]`. Only `mmlu-pro` shipped, and the second
should not be added on the same terms. Investigating a coding benchmark on 2026-08-10
established that **every off-the-shelf set is contaminated for models this recent**:
LiveCodeBench — the contamination-free option — stopped updating in June 2025, so its
newest problems predate every model we serve. A benchmark whose headline property has
expired is worse than none, because the number still looks authoritative.

The regiment mechanism is pluggable and unchanged; what is missing is a dataset worth
plugging in. **We still have no measurement of coding ability**, which for this cluster's
operator is the most decision-relevant axis there is. That gap is now the largest one in
the loop and belongs to a future ADR, along with the execution sandbox it requires.

### What the runner is, concretely

Four features, and each is here because of a specific incident during the 2026-08-09/10
campaign, when the sweep was a bash script in `/tmp`:

| feature | the incident |
|---|---|
| flat job list, enumerated | three scripts rewritten for one campaign |
| breadcrumbs, per **regiment** | a brownout landed mid-run; a finished 25-min eval was re-run |
| quarantine, per **profile** | DEF-0004 froze a node during weight load — a retry costs the night |
| exclusive lock | a stray manual bench overlapped the sweep's own and silently contaminated a TP=1 baseline |

Two bugs found while building it, both worth recording because they are the same class —
*a mechanism defeating its own purpose*:

- `soak` used `with ThreadPoolExecutor(...)`, whose exit calls `shutdown(wait=True)` — so
  the regiment built to **detect** a hang would itself hang on the request it had just
  detected as stuck, holding the whole sweep behind it.
- The readiness check in the throwaway script polled the head's IP, while single-node
  profiles serve on snoopy by design. It would have completed **green** having measured
  three TP=2 profiles with no TP=1 baselines to compare them against.

Neither was caught by a passing test suite, because neither had a test. Both do now.

### Still outstanding, deliberately

`tools` and `soak` ship as regiments but have never run against hardware — the fleet was
mid-rename when they were written. The first real sweep (`sweeps/nemotron-family.yml`) is
their first exercise, and it should be read as testing the runner as much as the models.
