# ADR-0015: Sparky as the single operator entrypoint (make removed)

**Date:** 2026-07-13
**Status:** Accepted

## Context

The harness (ADR-0010) grew into a full toolkit — `topology` / `smoke` / `bench`
/ `report` — and the cluster ended up with **two operator surfaces**:

- **`make`** — cluster lifecycle: `deploy` / `check` / `teardown` / `status` /
  `logs` (thin wrappers over `ansible-playbook` + ssh), plus dev tasks
  (`test` / `lint` / `download`).
- **`sparky`** — the Python harness that talks to running models.

Nothing is duplicated, but it's a confusing partition: a newcomer has no cue for
"is it `make X` or `sparky X`?" ADR-0002 established ansible-driven-by-make;
ADR-0010 framed make as "the thin operator entrypoint [that] delegates … via
`uv run sparky`" — i.e. make was *meant* to be the front door. In practice the
harness verbs became sparky-native, so `sparky` quietly became a second front door.

More decisively: the north-star is a **fleet orchestrator** — one head that
verifies every profile, re-benchmarks every model-hosting profile, and updates
models (see the fleet-orchestrator direction). That requires a deploy to be a
**programmable Python primitive** you can loop and assert on
(`for p in profiles: deploy(p); smoke()`). `make` cannot be that; a task runner
can't be the programmable outer layer a sweep needs.

## Options considered

**A. Keep both surfaces, document the boundary.** Zero code, but preserves the
split, and make still can't be the programmable layer the orchestrator needs.
Rejected — doesn't unblock the north-star.

**B. Make as the front door, delegating to sparky.** `make deploy` →
`uv run sparky deploy`. Honors ADR-0010's framing, but inverts the wrong way:
make stays the outer *name* while sparky does the work, the call graph goes
circular (`make → ansible → sparky-smoke`, and now `sparky → make`), and make's
`VAR=value` args are worse than sparky's positional ones. Rejected.

**C. Sparky as the single head; make removed (chosen).** The *doer* is outer.
Deploy logic (the publish rsync + `ansible-playbook` invocation) moves into
`sparky/ansible.py`; every make verb becomes a sparky subcommand. Ansible stays
the execution engine sparky invokes — inner, not gone. `./sparky.sh` is the root
entrypoint (ADR-0010 / root-entry-point convention).

## Decision

Option C. **`sparky` is the single operator entrypoint; make is removed.**

- `sparky/ansible.py` owns `publish()` + `deploy()` / `check()` / `teardown()` /
  `status()` / `logs()` (same two phases make ran: publish as the caller, then
  `sudo -u deploy ansible-playbook`).
- CLI verbs: `deploy` / `check` / `teardown` / `status` / `logs`, plus the dev
  tasks `test` / `lint` / `download` (thin shells over pytest / ansible
  `--syntax-check` / the download script).
- Both `Makefile`s are deleted. `./sparky.sh <verb>` replaces `make <verb>`.

**No cutover bridge.** Supporting make and sparky in parallel is ceremony for no
real safety: `git` is the rollback, and `sparky deploy` runs the identical
`ansible-playbook` command. Validation is by deploying — and ultimately by the
all-profile sweep, which is a *requirement* of the new structure, so it validates
the deploy primitive by construction.

## Consequences

- **One programmable surface.** Deploys become Python — loopable, assertable — the
  primitive every fleet sweep (verify / benchmark / update) is built on. This is
  the unlock the orchestrator needs; the split that prompted this ADR is gone.
- **Muscle memory changes:** `make deploy PROFILE=x` → `./sparky.sh deploy x`. The
  living docs (README, CLAUDE, `docs/*.md`, the Accepted ADRs) are rewritten to
  match. **Prior ADRs keep their `make` references** as historical record
  (immutability — make *was* the interface then); this ADR is where the interface
  changed.
- **The non-interactive / unattended deploy-context is *not* solved here.**
  `sparky deploy` prompts for `sudo -u deploy` exactly as `make deploy` did —
  interactive parity now. The scoped unattended path (for the sweeps) is deferred,
  with `sparky/breadcrumb.py` (durable resume/quarantine), to the orchestrator work.
- **The control panel is unaffected** — it already invokes `ansible-playbook`
  directly, never make. Routing it through `sparky deploy` (so every deploy shares
  one gated path, and the smoke gate can lift out of `site.yml` back up to sparky)
  is future work, not this ADR.
- Ansible is untouched as the config/execution engine (ADR-0002 stands); only the
  operator *interface* over it changed.
