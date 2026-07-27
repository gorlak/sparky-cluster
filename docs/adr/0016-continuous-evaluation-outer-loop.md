# ADR-0016: Continuous model-evaluation outer loop (human-authorized, agent-driven sweeps)

**Date:** 2026-07-27
**Status:** Proposed

## Context

The cluster's purpose is to serve the **smartest model that fits**, per tier, and to
keep that current as models release (see the fleet-priority north star:
Tier-1 = one model TP=2 fully-committed across both nodes; secondary tiers are
experimental). Deciding "what's best right now" is not a one-time choice — it's a
**standing, measured question**. A model discovery sweep (skills/model-discovery) tells
us which candidates *might* win; only deploying and measuring them tells us which
*does*.

Today that measurement is **manual and human-hand-held**. Each candidate is: a human
runs `./sparky.sh deploy <profile>` (password-gated), waits, a human/agent runs
`./sparky.sh bench`, reads numbers, repeats. The MTP-3 A/B on `qwen3.6-35b` (2026-07)
is a representative dry-run: deploy → bench baseline → deploy variant → bench → compare —
with a person crossing the one password gate at each step. That does not scale to
"evaluate every candidate we just acquired, at its best, across two tiers."

**Most of the substrate is now built:**
- `sparky` is a programmable operator primitive (ADR-0015) — deploy/bench/report/smoke
  are importable functions, not just a CLI.
- Fail-safe boot (ADR-0009) means a bad bring-up lands a node **empty and reachable**,
  not bricked — so an *unattended* risky deploy is recoverable.
- The smoke gate (ADR-0012 / ADR-0011) fails a corrupt engine **before** it's recorded
  live; the benchmark regiment (ADR-0012) is the throughput half of "how good is it."
- Container images are reproducible artifacts (ADR-0013) — a candidate blocked on
  upstream support can be **built** (a derived image) rather than skipped.
- This session added the observability an unattended loop needs: a **no-sudo live
  status** surface (`/status.json`, `sparky status`, exit-code = health) and a **durable
  deploy-gate breadcrumb** (`last-smoke.json`, written pass *or* fail), plus guards so
  a deploy is safe while downloads stage (`*.incomplete` skip) and bench no longer
  crashes on worker-node engines.

**What's missing** is the loop itself, and specifically:
1. **A non-interactive deploy path.** `./sparky.sh deploy` shells `sudo -u deploy …`,
   which prompts for the human's password — the deliberate automation gate from the
   three-tier identity model (ADR-0001). An agent cannot cross it. *But* the control
   panel (ADR-0008) already runs as `User=deploy` (NOPASSWD) and deploys by invoking
   `ansible-playbook` directly — the un-gated deploy-context already exists; it is just
   not exposed as a clean, scoped, agent-callable primitive.
2. **An industry-standard quality signal.** We have multiturn-corruption smoke and
   throughput bench, but no standardized *quality* eval (MMLU-Pro / LiveBench-class) to
   put a "how smart, measured here" number next to the tok/s.
3. **The guardrails** an unattended multi-hour sweep needs: an allowlist of what may be
   deployed, per-candidate soak, quarantine of a node-killer, and durable breadcrumbs so
   the sweep resumes after a hang/restart instead of re-running or re-freezing.

The crux is a **trust-boundary decision**: ADR-0001 made `deploy` a password-gated
context for the human on purpose. A continuous loop requires letting an agent reshape
serving without that password. That is not a capability gap — the seam exists — it is a
decision about *how much* autonomy to grant, and *within what envelope*.

## Options considered

**A. Stay fully manual (status quo).** A human runs every deploy; the agent only benches
and reports. Zero new trust surface. Rejected as the end state: it makes keeping the
fleet current an open-ended human chore (hours of hand-holding per candidate) and
squanders the fail-safe + smoke + breadcrumb substrate that exists precisely to make
unattended deploys safe. It remains the fallback when no sweep is authorized.

**B. Give the agent the deploy password / broad NOPASSWD sudo.** Simplest to wire, and
catastrophic: it dissolves the three-tier identity model (ADR-0001) — an agent with
unrestricted root is exactly what that separation prevents. An agent bug or bad prompt
could do anything to either node. Rejected outright.

**C. A scheduled autonomous daemon.** A service that runs sweeps on a cron with no human
in the loop per-sweep. Deferred, not rejected — it's a plausible *evolution* of D once
D is trusted, but starting there over-grants autonomy before the loop's failure modes
are understood on this hardware.

**D. Human-authorized, agent-driven sweeps through the deploy-context (chosen).** Expose
a **scoped, allowlisted, non-interactive** deploy primitive that routes through the
existing `User=deploy` control-panel context (never the human's password, never broad
sudo). A human **authorizes a sweep** — a candidate queue plus an envelope (which
profiles, soak duration, which evals) — and the agent is autonomous *within* it: deploy →
soak → eval → record → next, quarantining failures. The trust boundary is crossed only
inside a bounded, observable, abortable sweep.

## Decision

**Option D.** Build a **continuous model-evaluation outer loop**: a human-authorized,
agent-driven sweep that, per tier and within that tier's hardware envelope, measures
**quality vs. performance** for a queue of candidate profiles.

Design:

- **Non-interactive deploy primitive.** A `sparky` entrypoint (e.g. `sparky sweep` /
  a deploy call with an explicit non-interactive flag) that triggers deploy/teardown via
  the deploy-context — reusing the control panel's `User=deploy` seam (ADR-0008), not
  `sudo -u deploy`. It accepts **only allowlisted profiles** (never `blocked: true`),
  and every deploy runs behind fail-safe boot (ADR-0009) and the smoke gate (ADR-0012).
- **The loop, per candidate:** publish/deploy → **soak** (a multi-hour hold that catches
  the deadlock class, e.g. DEF-0002, which strikes 35–55 min in — a clean bring-up is
  necessary but not sufficient) → **quality eval** → **performance bench** (the ADR-0012
  regiment) → **record** a quality-vs-performance row to the trend store (ADR-0012's
  SQLite store) → next.
- **Quality-eval harness (new).** A standardized eval (lm-eval-harness or a curated
  MMLU-Pro/LiveBench-class set) run against the engine's OpenAI-compatible endpoint,
  recorded next to the bench numbers so "smart vs. fast within the tier" is one query.
- **Guardrails:** a **profile allowlist**; a per-candidate **soak timeout**;
  **quarantine** — a candidate that hangs a node is marked and skipped, and the sweep
  resumes (rather than re-deploying the node-killer); **durable breadcrumbs** (extending
  `current-topology.json` / `last-smoke.json`) so a sweep survives a hang/hard-reset and
  resumes where it left off; and **node-aware benching** (bench any engine on its node,
  closing the worker-engine gap hit in 2026-07).
- **Authorization model:** the **human authorizes a sweep** (queue + envelope: profiles,
  soak length, evals) and can abort it; the **agent is autonomous within that scope**.
  No standing blanket autonomy. Every action is observable live (no-sudo `/status.json`)
  and left as a durable breadcrumb, so the sweep is auditable after the fact.

## Consequences

- **The platform, not a chore.** Keeping the fleet current becomes an authorized sweep
  the agent runs for hours, instead of a human deploying and reading numbers one model
  at a time. This is the fleet-orchestrator north star realized.
- **The trust boundary is crossed deliberately and narrowly.** Autonomy is granted
  *inside a scoped, time-boxed, allowlisted, observable, abortable sweep* through the
  existing deploy-context — not by handing the agent the human's password or root
  (contrast Option B). ADR-0001's separation is preserved for everything outside a sweep.
- **Operationalizes prior ADRs.** The loop *runs* the ADR-0014 optimization A/Bs (MTP-3,
  FP8-KV/prefix re-test) automatically; consumes ADR-0012's bench regiment as its perf
  half; uses ADR-0013 to build-to-unblock candidates; and is fed by the model-discovery
  sweeps. It stands on ADR-0009 (safe unattended bring-up), ADR-0015 (the programmable
  primitive), and this session's status/breadcrumb + ingest/bench hardening.
- **New build surface**, each incremental: the non-interactive deploy trigger +
  allowlist, the quality-eval harness, quarantine/breadcrumb state, and node-aware
  benching. Node-aware benching and the deploy primitive are the first concrete items
  (both were hit as gaps during the 2026-07 MTP-3 A/B).
- **A new authorization surface to get right.** An allowlist and a scoped sweep are only
  as safe as their enforcement; the primitive must refuse anything off-allowlist and the
  quarantine must actually stop a re-freeze. These are the load-bearing details to
  validate before trusting a long unattended sweep.

Implementation lands incrementally; this ADR flips to **Accepted** when the first
end-to-end sweep (authorize → deploy → soak → eval → bench → record → next, with
quarantine) runs unattended. Build/progress tracking lives in the README / TODO, not in
this status.
