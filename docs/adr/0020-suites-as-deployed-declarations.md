# ADR-0020: Suites — named, reviewable procedures (`sparky run <name>`)

**Date:** 2026-08-10
**Status:** Accepted (implemented 2026-08-11)
**Errata 2026-08-11:** [ADR-0021](0021-suite-runs-from-the-panel.md) revised two things
below — the installed copy now exists, and `sparky run` starts a detached unit rather than
running the campaign in the foreground. Both are marked in place. The reasoning here is
left as written: it is what "no consumer yet" looked like from inside, and 0021 is the
consumer arriving.

## Context

ADR-0016 built the measurement machinery: regiments (`bench`, `quality`, `tools`,
`soak`, `vision`), a runner with breadcrumbs and quarantine, a label-keyed trend store,
and a scoreboard that reads it. What it did **not** settle is how a *procedure* gets
named, reviewed, kept, and started.

The answer so far has been bash in `/tmp`. Over 2026-08-09/10 that produced **five**
throwaway scripts for one campaign — `sweep.sh`, `twins.sh`, `final.sh`, `rebench.sh`,
and a YAML spec — of which four are gone. The costs were not hypothetical:

- one script was **lost to a brownout** mid-campaign and rewritten from scratch;
- two were **hand-patched while running** (a readiness wait, an in-flight-activation
  guard), so what executed was not what was reviewed;
- one polled the head's IP for readiness while single-node profiles serve on snoopy, and
  would have finished **green having measured half the comparison**;
- a stray manual bench **overlapped the sweep's own** and contaminated a baseline;
- every one of them died with the SSH session that started it.

ADR-0016's runner fixes the *execution* failures. It does not give a procedure an
identity. A sweep spec today lives in the repo at `sweeps/*.yml`, is not installed
anywhere, is not validated by anything, and is started by typing a path.

There is a shape already in the codebase that solves exactly this, twice. **Profiles**
are declarative artifacts in the repo; `deploy` installs them and writes an allowlist;
`activate <name>` instances one; the reconciler re-validates the name against the
allowlist on every node. **Images** work the same way (ADR-0013), which is what makes
ADR-0019's probe bounded — "probing something new is a deploy". Suites are the third
instance of that pattern and currently the only one done by hand.

## Decision

**A suite is a declarative artifact, deployed like a profile, instanced by name.**

```
suites/<name>.yml        in the repo — the allowlist, reviewed in a diff
  → ./sparky.sh run <name>      instances one (Operate scope: no privilege)
```

**The repo is the allowlist; there is no installed copy.** An earlier draft had `deploy`
install suites to `/opt/cluster/suites/` on the profile pattern. Nothing needs it:
`sparky run` executes on the control node, where the repo is present. An installed copy
exists to serve a *network-facing* consumer, and that consumer is deliberately not in this
ADR — so shipping the directory now would mean code reading a path nothing writes.

> **Superseded by ADR-0021 (2026-08-11).** The consumer arrived, and it arrived for both
> callers rather than one: `/opt/cluster/suites/` is now the installed allowlist and
> `sparky run` names a member of it too. The prediction above was right about *why* the
> directory would exist and wrong about it being the panel's alone.

### Steps are argv over sparky's **Operate** scope — never a shell string

A suite step names a `sparky` subcommand and its arguments as a list:

```yaml
name: nemotron-family
jobs:
  - profile: nvidia-nemotron-3-super-120b-a12b-nvfp4
    regiments: [tools, bench, quality]
```

...where a regiment resolves to a sparky command invoked as **argv, without a shell**,
and the subcommand is validated at run time against the commands whose
`rich_help_panel` scope is *Operate*. That set is machine-readable and enforced by
`tests/test_cli_surface.py`, which fails if any command omits a scope:

> **Operate:** `activate bench eval fleet logs probe report scoreboard smoke status sweep teardown topology`
> **excluded by construction:** `deploy admin-password`

This is the whole safety argument, and it is why the vocabulary is *derived* rather than
hand-maintained. Two properties follow for free:

1. **A suite cannot provision.** `deploy` is out of scope, and would fail anyway — it
   needs a TTY for the sudo password that no unattended runner has.
2. **The vocabulary tracks the tool.** A new Operate command is usable by suites the
   day it lands; a new privileged one is excluded without anyone remembering to exclude
   it. A hand-written regiment list would drift, and drift silently.

**A shell string would give away the entire ADR-0018 boundary.** The panel runs as
`activator`, an identity holding two single-command sudoers entries. A suite that could
carry `sh -c` would make any process that instances one a remote shell running as that
identity — which is precisely the "no web-API path to root" property ADR-0018 exists to
protect. argv-only is not defensive style; it is the load-bearing constraint.

### Not in this decision: instancing one over HTTP

The obvious next step is `POST /suites/<name>/run` on the control panel, so a multi-hour
measurement never depends on an SSH session. **That is a separate ADR, not a later phase of
this one.** It is a *mechanism and authorization* question — a new deploy pathway, an
installed allowlist, and a new thing a network-facing service can initiate — and ADR-0016
handed that class of decision to ADR-0018 by name.

> **That ADR is [0021](0021-suite-runs-from-the-panel.md) (2026-08-11).** It landed the
> day after this one, and answered the question differently than framed: the constraint
> turned out to be *lifetime*, not authorization — a suite run needs no privilege the
> panel identity lacks, it simply must not be a child of a web server. Splitting it out was
> still right, because that is not a conclusion this ADR would have reached.

Keeping it out is also what lets this ADR be Accepted honestly: everything described here
exists. A staged phase inside an accepted ADR is a tracker wearing an ADR's clothes, which
is the failure ADR-0016 spent a week demonstrating.

## Consequences

- **A procedure becomes reviewable.** A suite is read and approved before it takes the
  cluster for two hours, in a diff, rather than being typed at 1am and patched at 2am.
- **`docs/updating.md` gains a pathway.** Adding a suite is a change like any other and
  gets the same fan-out discipline as adding a profile — it just needs no deploy.
- **Results land where they already land.** Regiments record to the trend store, and
  `_refresh_panel_snapshot()` regenerates `/opt/cluster/scoreboard.json` after every
  recorded measurement — so a suite updates the scoreboard with no new plumbing. That
  pipeline exists today; this ADR only gives the front of it a name.
- **`sweeps/` becomes `suites/`.** A sweep is one *kind* of suite, and naming the
  directory after the first kind is how abstractions end up shaped like their first use.
- **Two wrinkles to handle in implementation**, both consequences of using real commands:
  `logs` follows indefinitely and needs bounding, and `sweep` invoking itself would
  deadlock on its own exclusive lock. Both are validation, not redesign.

## Alternatives rejected

**Keep writing bespoke scripts.** The status quo, and the thing this replaces. Five in
two days, four gone, one lost to a power cut, two edited mid-flight. The recurring cost
is not the typing — it is that a script in `/tmp` cannot be reviewed, cannot be resumed
by anyone else, and has no name to refer to afterwards.

**A `--detach` flag plus a suite page in the docs.** Cheaper, and it *was* the right
call for the immediate problem — it is in `skills/operations/SKILL.md` now. But it makes
long jobs survivable without making procedures reusable, and prose in a skill file is not
an artifact: nothing installs it, validates it, or can start it by name.

**A fixed regiment vocabulary maintained by hand** (the first draft of this ADR). Rejected
because it is a second vocabulary shadowing sparky's own commands, and the two would
drift — with the failure mode being a suite that silently does nothing.

**Arbitrary shell in a suite step.** Rejected on the grounds above: it converts anything
that can instance a suite into a remote shell running as an identity with sudoers
entries. The convenience is real and the trade is not close.

**A general workflow engine.** Overkill for a two-node cluster with one operator, and the
interesting parts here — resumption, exclusion, quarantine — are already built and are
control flow, which is why ADR-0016 chose Python over a DSL in the first place.
