# ADR-0012: Test strategy

**Date:** 2026-07-02
**Status:** Accepted

## Context

The project has **no automated tests**: no `tests/`, no pytest, no CI
workflow, no `ansible-lint` / `yamllint`. The only thing under "testing" is
`benchmark/` (performance measurement — and `run.sh` is currently broken, see
ADR-0009). Everything shipped to date has been validated by hand or by watching
a deploy.

That gap has already cost us. Several recent changes could have been caught
before touching hardware: the `docker exec vllm` staleness in `run.sh` (the
container is `vllm-<engine>` now), documentation drift on unit names, and the
kind of Jinja/logic regressions that are cheap to catch by rendering a template
and asserting on the output. The 26.06 hang wasn't a testable-in-CI failure, but
the *recovery* machinery built to survive it (ADR-0011) very much is — and
should not be validated only by wedging a live node.

This ADR records the decision to build a layered test regiment and the
inventory of what to build. It does **not** implement the tests; they are
erected incrementally as follow-up work.

## Options considered

**A. Keep validating by hand / by deploy (status quo)**
Zero setup, but every regression is found late (post-deploy, or in production),
reproduction is manual, and nothing guards against drift. Rejected — the cost
has already shown up repeatedly.

**B. Full integration testing (Molecule + ephemeral containers per role)**
Highest fidelity, but heavy: spinning up containers to exercise roles is slow,
brittle on this aarch64 / GB10 / RoCE-specific stack, and most of what would
break is either hardware-specific (not reproducible in CI) or already covered
more cheaply by static + render + unit layers. Rejected as the primary approach
— disproportionate for a 2-node cluster.

**C. Layered, cheapest-first regiment (chosen)**
Static checks + template-render tests + control-panel unit tests as the fast
core (seconds, no hardware), plus a small set of scripted synthetic infra tests
for the stateful paths, plus the runtime quality regiment already designed in
ADR-0009. Each layer is independent and adds value on its own.

## Decision

Option C. Build the layers below, cheapest/highest-value first. Molecule/full
integration is explicitly **out of scope**.

## The layers (priority order)

1. **Static** — `make lint`: `ansible-playbook --syntax-check` on `site.yml` /
   `teardown.yml` across every profile, `ansible-lint`, `yamllint`. Catches
   typos, bad references, and drift. Trivial; should also run in CI.

2. **Template render** — pytest that renders `roles/vllm/templates/vllm.service.j2`
   (and peers) over sample `serving_topology` inputs and asserts on the output:
   the ADR-0011 marker directives are present, `rank` / head-vs-worker is
   computed correctly per node, multi-node vs single-node arg assembly, the
   fail-safe `ConditionPathExists` / `ExecStartPre` / `ExecStopPost` triple.
   No hardware; catches the class of bug that has bitten us most.

3. **Control-panel unit** — pytest + FastAPI `TestClient` with `_run` /
   `_marker_present` mocked: `gather()` fail-safe detection (marker-present +
   unit-down ⇒ `failsafe`), `_build_cmd`, profile validation / path-traversal
   guard, `/health.json`, and the recovery-banner rendering. Fast, no hardware.

4. **Synthetic infra** — scripted, run against a live cluster but without
   inducing real faults:
   - **Fail-safe / recovery** (ADR-0011): stop an engine, re-create its marker,
     assert the panel reports `failsafe`, assert `systemctl start` is skipped by
     the condition, then assert a Retry/empty deploy clears the marker and
     recovers. (A hard reset is *not* required — the marker is the signal.)
   - **Teardown idempotency**: teardown twice, assert clean + no error.
   - **Profile-switch reconciliation**: switch profiles, assert the desired unit
     set is running and pruned units are gone.

5. **Runtime quality** — the ADR-0009 smoke test (deploy gate) + full run
   (weekly), storing to SQLite (ADR-0010). Catches model corruption and
   throughput regressions that only appear at inference time.

## Consequences

- Layers 1–3 run in seconds with no hardware, so they belong in CI (a
  `.github/workflows/` or equivalent) and as a local `make test` — this is the
  first CI the project would have.
- Layer 4 needs a live cluster and is run on demand / around risky changes; it
  is scripted so it is reproducible rather than ad-hoc.
- The synthetic fail-safe test (4) means ADR-0011 can be validated without ever
  hanging a node — the marker file is the whole contract.
- The layers are independent: `make lint` + the two pytest suites alone cover a
  large fraction of the drift/logic failure modes and can land before any of the
  heavier layers.
- Implementation is deferred and incremental; this ADR is the inventory and
  priority, not a delivery. Each layer that ships updates its status here.
