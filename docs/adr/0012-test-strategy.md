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
     set is running and pruned units are gone. Also cover the **GPU-teardown race**
     (found 2026-07-03): switching *off* a memory-heavy engine and immediately
     bringing up the next can race the old engine's GPU-context/memory release
     against the new engine's CUDA init. Concretely — `step-3.5-fp8` (holding
     ~97 GiB on the head) → `qwen3-coder-nvfp4-dual`: sparky's incoming engine
     **crashed on its first load attempt** (`cudaErrorIllegalInstruction` /
     `CUBLAS_STATUS_INTERNAL_ERROR`) and only succeeded after `Restart=on-failure`
     retried (~7 min lost); snoopy (pure worker, lighter teardown) loaded clean.
     **Fix — settle-before-start:** after pruning the old engine the `vllm` role
     should poll until GPU/unified memory is actually reclaimed (old `vllm-*`
     containers gone **and** memory dropped to baseline, with a timeout) *before*
     starting the new engine — turning a crash-recover into a clean handoff. The
     test: switch off a big engine and assert the next comes up with **`NRestarts=0`**
     (no transient crash). Fail-safe/`Restart=on-failure` is the safety net, not the
     intended path.
   - **First-deploy of a brand-new engine** — a real regression: on 2026-07-02 a
     first deploy of the new `step-3.7` engine *aborted the playbook* because the
     "reconnect stale worker" task (`roles/vllm/tasks/engine.yml`) fired when both
     units were newly-`changed` and raced the worker's daemon-reload ("service not
     found"). The fix only reconnects workers whose unit *didn't* change; a test
     should deploy a never-before-seen engine and assert the run completes. (This
     is also mostly a **template/logic** check — `engine.yml`'s `when` conditions
     over `engine_unit.changed` × `hostvars[worker].engine_unit.changed` are
     assertable without a full deploy.) A second manifestation (2026-07-03) exposed
     the deeper flaw and was **fixed by removing the task**: the cross-host
     `hostvars[worker].engine_unit` read is unreliable inside a looped
     `include_tasks` (undefined ⇒ crash when a worker didn't run the role; an
     `is defined` guard then made it silently *skip* ⇒ a head-only-flag change left
     the worker orphaned on a stale rendezvous, needing a manual worker restart).
     Reconnect now lives in the **unit template**: `vllm.service.j2` embeds an
     engine-spec hash in the worker unit, so any head-side change re-renders (⇒
     restarts) the worker too — head + workers restart as a matched pair. The test
     is now a **template-render** assertion (Layer 2): changing `head_extra_args`
     changes the *worker* unit's rendered content; a `worker_extra_args`-only change
     does *not* change the *head* unit.

5. **Runtime quality** — the ADR-0009 smoke test (deploy gate) + full run
   (weekly), storing to SQLite (ADR-0010). Catches model corruption and
   throughput regressions that only appear at inference time — e.g. an engine that
   *crash-loops* on startup (like `step-3.7`'s `Step3VLProcessor` bug, 2026-07-02)
   is exactly what the post-deploy readiness gate should catch and fail on.

   **Request-shape smoke (planned).** The gate must probe the *request shapes
   Open WebUI actually sends*, not just readiness — the next rung after *load ≠
   serve* is *serve ≠ serves-what-the-UI-sends*. Per API-node engine, assert HTTP
   200 on:
   - a plain chat completion with non-empty content (the load ≠ serve check); and
   - a chat completion carrying a dummy `tools` array + `tool_choice: "auto"` —
     Open WebUI sends this on ordinary chats, so an engine started without
     `--enable-auto-tool-choice` / `--tool-call-parser` passes readiness but 400s
     the moment a user types. This exact gap shipped on `minimax-m2.7-awq`
     (2026-07-03): *"'auto' tool choice requires --enable-auto-tool-choice and
     --tool-call-parser to be set"* — found only by hand in the chat UI, fixed by
     adding the `minimax_m2` parser. A request-shape probe against every API
     engine encodes the invariant "this engine can serve the UI it's wired to" and
     would have failed the deploy instead.
   Optionally also assert the reasoning parser emits clean, separable thinking
   tokens (the `step-3.5` multi-turn-corruption class).

   **Readiness gate as the marker + failure diagnostics (planned).** The head-API
   poll (`wait.yml`: `/v1/models` → 200) is the deploy-gate *marker* — the pass/fail
   that the engine came up, and the base the request-shape smoke builds on (only
   meaningful once ready). For TP=N, head-200 ⇒ all workers ready (workers are
   headless — no separate probe). Today a gate *timeout* is an opaque countdown;
   enhance it to **dump each node's last weight-load-progress line + unit state on
   failure**, so a stuck deploy self-reports *where* it stalled (a given load %,
   pre-NCCL, or a crash) instead of just "timed out." Progress comes from either a
   scoped `journalctl` read of vLLM's `Loading … shards n/N` line (coupled to the log
   string — a maintenance caveat) or the log-free **per-node memory-climb proxy** via
   the existing node-exporter/Grafana. Note: Ansible *gates*, it doesn't stream —
   live per-node progress belongs in the control panel (`docs/control-interface.md`),
   not the playbook.

## Consequences

- Layers 1–3 run in seconds with no hardware, so they belong in CI (a
  `.github/workflows/` or equivalent) and as a local `make test` — this is the
  first CI the project would have.
- Layer 4 needs a live cluster and is run on demand / around risky changes; it
  is scripted so it is reproducible rather than ad-hoc.
- **Layers 4–5 must run real deploys unattended — that needs a non-interactive
  privileged path (affordance to build).** `make deploy` today gates on
  `sudo -u deploy` prompting for geoff's password; a test runner can't answer that
  prompt. The affordance is the same one the planned dashboard uses (README
  identity model): execute in the `deploy` context directly — it already holds
  `NOPASSWD: ALL` on both nodes — via a CI/test-runner service running as
  `User=deploy`, or a narrowly-scoped `sudo -u deploy` rule for the test
  entrypoint, so profile-switch / teardown-idempotency / post-deploy smoke can
  drive `ansible-playbook` with no human in the loop. This is a **prerequisite**
  for Layers 4–5. It widens the trust surface (automated code gains deploy
  rights), so scope it to a known test entrypoint rather than a blanket capability,
  and keep it off the interactive `geoff` path.
- **Deploy-driving tests can hard-freeze a node — they need durable breadcrumbs to
  survive it (design in, don't bolt on).** A test that deploys a bad engine can
  hang the box (the 26.06 NVFP4 Marlin load froze both nodes to a hard reset,
  2026-07-02). When that happens the test process dies *with* the node — no clean
  failure report — and naively re-running the suite re-runs the freezer in a loop.
  So Layers 4–5 write **write-ahead breadcrumbs to persistent disk** (same class as
  ADR-0011's `vllm_state_dir` markers — NOT `/run` or `/tmp`): before each risky
  step (a deploy, an individual case) record intent `{test_id, phase, profile, cmd,
  started_at, host}` and advance/clear it on clean completion. On startup the
  harness reads any surviving breadcrumb: the step it names is a *suspected
  freezer* — **quarantine it** (mark failed-by-hang, skip unless
  `--retry-quarantined`) and continue the rest of the suite, so one node-killer
  doesn't wedge the whole regiment. Two granularities — **per-deploy and
  per-test-case** — because a case can hang outside a deploy too. Correlate the
  breadcrumb with ADR-0011's surviving per-engine `.running` marker to localize the
  freeze ("test T deployed profile P, engine E's marker survived ⇒ E's weight load
  hung it"). An append-only trail (intent → result) also reconstructs the *sequence*
  that led to a freeze across reboots, not just the last step. This is the
  test-harness analogue of ADR-0011: durable on-disk state so an *unclean* death
  degrades to skip-and-continue instead of re-freeze.
- The synthetic fail-safe test (4) means ADR-0011 can be validated without ever
  hanging a node — the marker file is the whole contract.
- The layers are independent: `make lint` + the two pytest suites alone cover a
  large fraction of the drift/logic failure modes and can land before any of the
  heavier layers.
- Implementation is deferred and incremental; this ADR is the inventory and
  priority, not a delivery. Each layer that ships updates its status here.
