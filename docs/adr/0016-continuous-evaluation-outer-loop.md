# ADR-0016: Continuous model-evaluation outer loop (human-authored, agent-driven sweeps)

**Date:** 2026-07-27 (scoped 2026-07-29: the deploy/activate mechanism and authorization
model moved to ADR-0018; this ADR keeps the loop, the sweep representation, and the eval
regiments)
**Status:** Proposed

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

**C. A declarative CI-style matrix sweep, human-authored and kicked, agent-driven (chosen).**
The phase is *data* — a matrix of models × variants × regiments — that a human authors,
reviews, and kicks; the agent runs it to completion, recording as it goes. Planning is forced
up front; execution is uniform and resumable.

## Decision

**Option C.** Build a continuous model-evaluation outer loop — a human-authored, agent-driven
sweep that measures **quality vs. performance** per tier.

### The sweep as a CI-style matrix (profile × variant × regiment)
```yaml
name: candidate-eval-r1
matrix:                          # profile × variant = what gets DEPLOYED (sequential)
  profile:
    - {name: mistral-medium-3.5-nvfp4, tags: [multimodal, big-shared]}
    - {name: nemotron-puzzle-75b-single, tags: [reasoning, mtp-capable]}
  variant: [base, {knob: mtp, needs_tag: mtp-capable}, {knob: fp8-kv+prefix}]
regiments:                       # what RUNS against each live model (cheap)
  - smoke
  - bench:   [latency, throughput, prefix]
  - quality: [mmlu-pro, livebench]
  - soak:    {minutes: 10}          # longer only for DEF-0002-risky TP=2/26.06 candidates
exclude:
  - {regiment: quality, profile_tag: single-node}
```

- **Sequential by activation, unlike CI.** Only one model is live at a time, so profile×variant
  is the outer, serial loop — each combo is one `activate` (ADR-0018) — and regiments run
  against the live model (cheap). The runner expands `matrix` → filtered job list
  (`exclude`/`needs_tag`) → sequential activate-and-regiment loop, with durable **breadcrumbs**
  to resume after an interruption and **quarantine** a node-killer (mark, skip, continue).
- **Optimization A/Bs are just the `variant` axis** — no special machinery; since the trend
  store keys by label, comparison falls out for free (`report base mtp`). The ADR-0014 register
  becomes "add a variant."
- **Regiments are pluggable:** `smoke`/`bench` exist; `quality` and `soak` are to build.
  - **`quality`** — a standardized eval against the endpoint (recorded next to the bench
    numbers). Note the cost: full MMLU-Pro/LiveBench is *hours per variant*, impractical across
    a matrix — so it runs a **subset / fast proxy**, sized deliberately, not the whole suite.
  - **`soak`** — a hold that catches instability a clean bring-up misses. **Default 10 min**;
    but the DEF-0002 deadlock class strikes **35–55 min in**, so 10 min *cannot* catch it — a
    longer soak (45–60 min) is a **per-candidate knob** for the DEF-0002-risky TP=2/26.06
    profiles. A blanket 10 min would pass slow deadlocks through the gate.
  - **Node-aware benching** (bench any engine on its node) is a prerequisite fix, hit during
    the MTP-3 A/B.

### How it runs (mechanism → ADR-0018)
The human authors + kicks a sweep by **adding its `(profile × variant)` set to the allowlist
and running `deploy`** (convergent, whole-fleet, password-gated — the out-of-band
authorization). The agent then **`activate`s** across the deployed set, running regiments and
recording — no privilege, no per-step hand-off. A sweep **commandeers the cluster**: only one
model is live at a time, so the human-facing serving (Open WebUI / the stable endpoint) is
**suspended for the sweep's duration** and restored on completion — no collision between eval
traffic and chat. The deploy/activate control model, the selector, the stable serving surface,
and the authorization/trust boundary are all in **[ADR-0018](0018-provision-select-split.md)**.

## Consequences

- **The platform, not a chore.** Candidate evals + the ADR-0014 optimization register run as one
  kicked matrix-sweep instead of dozens of hand-offs. The fleet-orchestrator north star realized.
- **Planning is designed-in.** Authoring the matrix forces up-front reasoning about which models,
  variants, and regiments apply.
- **Reuse over rebuild.** The trend store gives free comparison; ADR-0018 gives deploy/activate;
  the discovery sweeps feed the queue; ADR-0013 builds-to-unblock candidates.
- **New build surface:** the matrix expander + sweep runner (breadcrumbs, quarantine); the
  quality-eval harness; the soak monitor; node-aware benching. (The control model — selector,
  stable endpoint, no-sudo `activate` — is ADR-0018's build.)
- **Relationships.** Depends on ADR-0018 (the how) and ADR-0012 (bench + trend store); stands on
  ADR-0009 (safe unattended bring-up) and ADR-0015 (programmable primitive); operationalizes
  ADR-0014 (runs its A/Bs) and the model-discovery sweeps.

## Test plan (following ADR-0011's layered regiment)

- **Layer 2/3 (no hardware):** the **matrix expander** (`matrix × exclude × needs_tag → job
  list`); the **quarantine/resume** breadcrumb logic (a node-killer is marked and skipped; a
  sweep resumes at the right job after an interruption); regiment selection per profile tags.
- **Integration:** a 2-job dry sweep on already-deployed profiles — `activate` → smoke → bench →
  record → next — asserting the trend-store rows and resume-after-interrupt.
- **Per regiment, as built:** `soak` — a hang is detected within the window (and the 10-min
  default provably *doesn't* catch a >35-min stall, so DEF-0002-class candidates carry the
  longer knob); `quality` — the subset harness records a score against the endpoint.

Status flips to **Accepted** when a first human-kicked matrix-sweep runs end to end (deploy the
set → activate → soak → eval → bench → record → next, with quarantine). Build/progress tracking
lives in README / TODO, not this status.
