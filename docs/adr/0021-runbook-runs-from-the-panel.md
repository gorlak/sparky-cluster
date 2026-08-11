# ADR-0021: Runbook runs get a detached, logged home — the panel starts them

**Date:** 2026-08-11
**Status:** Accepted (implemented 2026-08-11)

## Context

ADR-0020 made a runbook a named, reviewable artifact started by `./sparky.sh run <name>`.
What it did not give it is somewhere to *live*. A runbook run is hours long, and today it
runs wherever it was typed: in an SSH session that reaps on a dropped connection, writing
to a terminal that scrolls away.

That is the whole gap, and it is worth stating plainly because it bounds the work: **a
runbook run needs a detached and logged environment. Nothing more.** Not a scheduler, not
a job queue, not a progress dashboard — a place to run that outlives the thing that
started it, and a log you can read afterwards.

**The constraint is lifetime, not privilege.** A runbook activates — already granted to
`activator` through the reconciler (ADR-0018) — and then drives the endpoint over HTTP.
Nothing in one needs root that the panel identity does not already hold. But a process the
panel spawns is a child in `control-panel.service`'s cgroup, and every deploy restarts that
service; a three-hour campaign would die at the ninety-minute mark, silently, and turn up
as a half-finished scoreboard. `setsid` does not help — a new session is the same cgroup.
To outlive its parent the run must be its own systemd unit, and starting a system unit is
privileged.

So this needs a bounded trigger for a reason neither of its siblings had: not "this needs
root", but "this must not be a child of a web server".

## Decision

**A runbook run is always a transient systemd unit, started through one fixed program —
`/usr/local/sbin/vllm-runbook` — and appending its output to a per-runbook log.**

Detached and logged, and both come from the same mechanism: a unit has its own lifetime,
and `StandardOutput=append:` gives it a log without a wrapper, a pipe, or a pid file.

**There is exactly one way to start one, and both callers are thin.** `./sparky.sh run
<name>` no longer runs the campaign in the foreground — it kicks the unit and returns.
`POST /runbook/{name}` on the panel does the same thing. Neither is a special case, and
"was it started from a terminal?" stops being a thing that can make a run fragile.

This mirrors `activate` exactly (ADR-0018): the CLI and the panel are two callers of the
*same fixed program*, not of each other. The CLI deliberately does **not** post to the
panel to get there — it already holds the grant (geoff is in `activate`), so the HTTP hop
would add a dependency without adding a check, and would make a three-hour measurement
impossible whenever the web server is wedged. What the two share is the bounded program;
sharing a web API instead would put a status surface on the critical path of the work.

The unit executes `sparky sweep <installed path>`, not `sparky run` — `run` is the
launcher, `sweep` is the foreground runner it launches. Keeping those distinct is what
stops the trigger from recursing into itself, and it leaves `sweep` as the escape hatch
for a job list that is not (yet) an installed runbook.

Bounded exactly like `vllm-activate` and `vllm-probe`:

- **The name is checked against an installed allowlist** — `/opt/cluster/runbooks/`,
  published by `deploy` and not writable by the activation identity. **This is what
  re-earns the installed copy** that ADR-0020 removed for having no consumer. The repo is
  where a runbook is authored and
  reviewed; the installed set is what may be *instanced*, because a caller reachable from
  the network must not be able to run a file that merely happens to be in a git checkout.
  Adding a runbook is therefore a deploy — the same bargain profiles already make, and the
  reason `sparky sweep <path>` stays available for a job list that has not earned one yet.
- **The argument is a bare identifier.** No path, no flags, nothing from the request
  reaches a shell. Every path — the log, the unit, the harness — is composed by the
  program from constants.
- **The unit is fixed**, so one run holds the cluster at a time. That was already true (the
  sweep lock, ADR-0016); this makes it true at the systemd layer too, where it is visible.

`stop` is included, and matters more here than for an activation: an activation finishes or
fails within minutes, while a runbook holds the whole fleet for hours and the operator may
simply want it back. Stopping is safe — breadcrumbs are written after every regiment, so a
stopped run resumes rather than restarts.

**Status needs nothing new.** `systemctl show` reports whether the unit is running and how
it exited; reading unit state has never been privileged. The panel tails the log file. No
state is computed twice and no pid is tracked.

### Two things the run needs that it did not have

1. **Something to execute.** `/opt/cluster/sparky` is published source with no interpreter
   — `sparky run` today works only from the repo through `uv`, which `activator` cannot
   reach. `deploy` now installs the published harness into a venv beside it, the same way
   the control panel is installed. The install is **editable**, so the node holds one copy
   of the harness rather than two: a regular install snapshots it into site-packages, and
   since the version string never changes `pip` would decline to replace that snapshot —
   a deploy would silently ship stale code. This way the rsync that publishes *is* the
   update. One consequence follows: an installed harness cannot find `../ansible/profiles`,
   so `topology` falls back to the published `/opt/cluster/ansible` — same content, same
   deploy.
2. **Somewhere to write.** `/opt/cluster` is `deploy:cluster`, and `activator` is
   deliberately not in `cluster` — that group owns the tree ansible later runs as root, and
   handing a network-facing identity write access to it would be an escalation path
   dressed as a convenience. Instead, the artifacts a *measurement* produces —
   `sweep-state.json`, `sweep.lock`, `last-smoke.json`, `scoreboard.json`, the trend store
   — become writable by the `activate` group, which already contains both the operator and
   the panel identity. `/opt/cluster` gets the sticky bit so that write access cannot be
   used to replace `ansible/` or `sparky/` with something a later deploy would execute.

   The resulting rule is worth stating: **`/opt/cluster` is `deploy`'s to provision and
   `activate`'s to record.**

## Consequences

- **A measurement no longer depends on a terminal staying open**, which was the original
  ask behind ADR-0020 and the half that was missing.
- **`activator` holds a third single-command grant.** The deploy's assertion that geoff has
  no unexpected passwordless sudo must learn about it, as it did for the probe.
- **The harness is now installed, not just published.** Its dependencies resolve at deploy
  time on the node, so a broken dependency set becomes a deploy failure rather than a
  runtime one — but it is one more thing a deploy can fail on.
- **Adding a runbook becomes a deploy**, and `sparky run` no longer runs anything from the
  repo. That is a real cost paid for a real property — the same list, whoever asks. The
  iteration loop for an unfinished job list is `sparky sweep <path>`, in the foreground,
  where it belongs while you are still changing it.
- **`sparky run` returns immediately**, so a script that ran a campaign and then read the
  results would now read them too early. Nothing does that today, and `run --follow` is
  there for a human who wants the old feel.
- **Anyone who can reach `/admin` can commandeer the cluster for hours.** It is basic_auth'd
  and that identity already holds the activation grant, so the blast radius is unchanged in
  kind — but an unwanted activation costs minutes and an unwanted runbook costs an evening.
  That is what `stop` is for.

## Alternatives rejected

**Run it as a child of the panel.** Needs no grant at all, and dies on the next deploy. A
measurement system whose runs are killed by unrelated maintenance is worse than one that
requires a terminal, because the failure is silent and arrives late.

**`systemd-run --user` with lingering.** Avoids the sudoers entry, but requires a one-time
`loginctl enable-linger` — precisely the class of undocumented bootstrap step whose absence
shows up at the worst moment.

**Import the harness into the panel's venv and run it in-process.** No subprocess, no
trigger — and a three-hour job inside a uvicorn worker, the panel's pinned dependency set
(DEF-0005 is the standing reminder) fused to the harness's, and it still dies with the
service.

**Add `activator` to the `cluster` group.** One line, and it would work. It also gives an
identity reachable from the network write access to `/opt/cluster/ansible`, the tree a
later `deploy` executes as root. That is a web-API path to root by a slower route, which is
the one property this whole split exists to deny.

**A general "run any sparky command" endpoint.** The panel would become a remote shell
running as an identity with sudoers entries. Naming runbooks was what made the invocable
set finite and reviewable; this would trade that away for convenience.
