"""Runbooks — named, reviewable procedures (ADR-0020).

A runbook is to a *procedure* what a profile is to a *model*: a declarative artifact in
the repo, installed by `deploy`, instanced by name. `./sparky.sh run <name>`.

**The safety property, and the reason it is derived rather than written down.** A runbook
step names a `sparky` subcommand, and that subcommand must be in the **Operate** scope —
no privilege, agent-drivable. That set is not a list maintained here; it is read from the
CLI's own scope declarations, which `tests/test_cli_surface.py` requires every command to
carry. So `deploy` and `admin-password` are excluded by construction, and a new Operate
command is usable the day it lands without anyone remembering to add it.

**argv, never a shell string.** This is the load-bearing constraint, not defensive style.
Anything that can instance a runbook runs it as `activator` — an identity holding two
single-command sudoers entries (ADR-0018/0019). A step that could carry `sh -c` would make
that process a remote shell running as that identity, which is exactly the "no web-API
path to root" property the whole split exists to protect.

The runner itself is `sweep.py`. This module owns naming, discovery and validation — what
may be run, and whether this file describes it correctly — and deliberately not how.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Two directories, two jobs — and keeping them apart is the point (ADR-0021).
#
# The REPO is where a runbook is authored and reviewed, in a diff, like `ansible/profiles/`.
# `lint` validates this one. Nothing runs from it.
#
# The INSTALLED set is what may be *instanced*. `deploy` writes it, root owns it, and both
# callers that can start a run — the CLI and the control panel — name a member of it. A
# caller reachable from the network must not be able to run a file that merely happens to
# be in a git checkout, and the moment two callers consult different lists, "which runbooks
# exist" has two answers.
#
# So adding a runbook is a deploy, the same bargain profiles make. Iterating on one that
# has not earned that yet is `sparky sweep <path>`, in the foreground.
REPO_DIR = Path(__file__).resolve().parent.parent / "runbooks"
INSTALLED_DIR = Path("/opt/cluster/runbooks")


class UnknownRunbook(LookupError):
    """No such runbook. Raised with the list, because a typo and a missing deploy look
    identical from the outside and the fix differs."""


class BadRunbook(ValueError):
    """The file exists and describes something we will not run."""


def names_in(directory: Path) -> list[str]:
    try:
        return sorted(p.stem for p in directory.glob("*.yml"))
    except OSError:
        return []


def available() -> list[str]:
    """What may be instanced — the installed set, and only that.

    Deliberately not "installed plus whatever is in the repo". A list that answers
    differently depending on which caller asks is not an allowlist.
    """
    return names_in(INSTALLED_DIR)


def authored() -> list[str]:
    """What the repo declares — the input to a deploy, and what `lint` checks."""
    return names_in(REPO_DIR)


def describe(directory: Path | None = None) -> list[dict]:
    """`[{name, description, estimate, jobs, order}]` for everything startable.

    For the surfaces that OFFER a runbook rather than run one. A bare name is a poor
    thing to put next to a button that commandeers the cluster for an evening — the
    person pressing it is not reading the YAML, which is the whole reason `description`
    and `estimate` are required fields rather than comments.
    """
    directory = directory or INSTALLED_DIR
    out = []
    for name in names_in(directory):
        try:
            spec = load(name, directory=directory)
        except (UnknownRunbook, BadRunbook, Exception):  # noqa: B014 - any bad file
            spec = {}
        out.append({
            "name": name,
            "description": (spec.get("description") or "").strip(),
            "estimate": (spec.get("estimate") or "").strip(),
            "jobs": len(spec.get("jobs") or []),
            "order": _order(spec),
        })
    # Presentation order, not alphabetical: the list is a menu, and alphabetical puts
    # whatever happens to start with 'a' in front of whatever you actually reach for.
    # Declared per file rather than centrally — a central list is one more thing to
    # forget when a runbook is added, and it would live nowhere in particular.
    return sorted(out, key=lambda r: (r["order"], r["name"]))


# Unordered runbooks land between the deliberate ones and nothing in particular, which is
# where a file that has not thought about it belongs.
DEFAULT_ORDER = 50


def _order(spec: dict) -> int:
    try:
        return int(spec.get("order", DEFAULT_ORDER))
    except (TypeError, ValueError):
        return DEFAULT_ORDER


def path_for(name: str, *, directory: Path | None = None) -> Path:
    """Resolve a NAME to a file. Never a path from the caller.

    Taking a name rather than a path is what makes the allowlist mean anything: a path
    argument would let any YAML on the box be run, and the installed set would be
    decoration. It also removes traversal as a concern rather than sanitising for it.
    """
    # Every `directory=` here resolves at CALL time rather than as a default argument. A
    # default binds the constant at import and then quietly ignores any later
    # reassignment — which reads as "the patch did not work" and is really "the patch was
    # never consulted". The lock code had the identical bug on the same day.
    directory = directory or INSTALLED_DIR
    if "/" in name or name.startswith("."):
        raise BadRunbook(f"{name!r} is a name, not a path")
    candidate = directory / f"{name}.yml"
    if candidate.is_file():
        return candidate
    # A typo and a missing deploy look identical from here, and the fix differs — so say
    # which of the two it is rather than making the reader guess.
    known = names_in(directory)
    hint = (f"Installed: {', '.join(known)}." if known
            else f"Nothing is installed at {directory}.")
    if name in authored():
        hint += f" {name!r} exists in the repo but has not been deployed — ./sparky.sh deploy."
    raise UnknownRunbook(f"no runbook {name!r}. {hint}")


def load(name: str, *, directory: Path | None = None) -> dict:
    spec = yaml.safe_load(path_for(name, directory=directory).read_text()) or {}
    if not isinstance(spec, dict):
        raise BadRunbook(f"{name}: expected a mapping, got {type(spec).__name__}")
    return spec


def operate_commands() -> set[str]:
    """The commands a runbook may invoke — sparky's own Operate scope.

    Read from the CLI at call time rather than duplicated, so the two cannot disagree.
    Imported late because `cli` imports plenty and this module is also used by `lint`.
    """
    from sparky.cli import app
    out = set()
    for command in app.registered_commands:
        scope = getattr(command, "rich_help_panel", "") or ""
        if scope.startswith("Operate"):
            out.add(command.name or command.callback.__name__)
    return out


def _coverage_problems(name: str, spec: dict, jobs: list) -> list[str]:
    """`covers: allowlist` — the job list must name every activatable profile.

    ADR-0020 keeps job lists LITERAL: what you read is what runs, because a matrix that
    expands at runtime is how an approved list stops matching what executes. The cost of
    that is drift — add a profile, forget the runbook, and the gap shows up much later as
    a missing scoreboard row.

    This is the cheap half of the fix. The list stays literal and reviewed; declaring what
    it is *supposed* to cover turns the drift into a lint failure, which is a thing you
    find before a deploy rather than after a seven-hour campaign.
    """
    if spec.get("covers") != "allowlist":
        if "covers" in spec:
            return [f"{name}: unknown `covers: {spec['covers']!r}` — only 'allowlist'"]
        return []

    from sparky import topology
    try:
        # Activatable: not `empty` (nothing to measure) and not parked — a `blocked`
        # profile keeps its weights precisely so it cannot be activated.
        expected = {p.name for p in topology.all_profiles()
                    if not p.is_empty and not p.blocked}
    except Exception as exc:  # noqa: BLE001 - a missing profiles dir is not this file's fault
        return [f"{name}: cannot read the allowlist to check coverage ({exc})"]

    listed = {(job if isinstance(job, str) else (job or {}).get("profile")) for job in jobs}
    problems = []
    if missing := expected - listed:
        problems.append(f"{name}: declares `covers: allowlist` but omits "
                        f"{', '.join(sorted(missing))}. Add them, or drop the declaration.")
    if extra := listed - expected - {None}:
        problems.append(f"{name}: names {', '.join(sorted(extra))}, which are not "
                        f"activatable (retired, parked, or misspelled).")
    return problems


def validate(name: str, spec: dict | None = None, *,
             directory: Path | None = None) -> list[str]:
    """Problems with a runbook, as a list. Empty means it is runnable.

    Defaults to the REPO copy: validation is a pre-deploy gate, so it must fail on the
    file you are about to install rather than on the one already installed.

    Returns rather than raises so `lint` can report every fault in every runbook at once —
    the same reason `Fleet.validate` collects instead of failing on the first.
    """
    spec = spec if spec is not None else load(name, directory=directory or REPO_DIR)
    problems: list[str] = []
    allowed = operate_commands()

    jobs = spec.get("jobs")
    if not jobs:
        problems.append(f"{name}: no jobs")
        return problems

    # The FILE basename is what everything addresses: the trigger's allowlist check, the
    # log path, `sparky run <name>`. A `name:` inside that says something else is a label
    # nothing honours, and the first person to trust it goes looking for a log that is
    # not there. Same rule profiles already follow — one name everywhere.
    if (declared := spec.get("name")) and declared != name:
        problems.append(f"{name}: declares `name: {declared}` but the file is {name}.yml. "
                        f"The filename is what addresses it — rename one of them.")

    problems += _coverage_problems(name, spec, jobs)

    default_regiments = ((spec.get("defaults") or {}).get("regiments")) or []
    for i, raw in enumerate(jobs):
        job = {"profile": raw} if isinstance(raw, str) else dict(raw or {})
        if not job.get("profile"):
            problems.append(f"{name}: job {i} has no profile")
        for step in job.get("regiments") or default_regiments:
            # A step is a bare regiment name, or {cmd: <subcommand>, args: [...]}.
            if isinstance(step, dict) and "cmd" in step:
                cmd = step["cmd"]
                if cmd not in allowed:
                    problems.append(
                        f"{name}: job {i} invokes {cmd!r}, which is not an Operate-scope "
                        f"command. Allowed: {', '.join(sorted(allowed))}")
                if not isinstance(step.get("args", []), list):
                    problems.append(
                        f"{name}: job {i} step {cmd!r} — args must be a LIST (argv). A "
                        f"string would be a shell command, which a runbook may never be.")
            elif not isinstance(step, str):
                problems.append(f"{name}: job {i} has a step that is neither a regiment "
                                f"name nor {{cmd, args}}: {step!r}")

    # LAST, so a real fault reads first. These two are required all the same: the button
    # that starts a runbook is in a web page, and whoever presses it is not reading this
    # YAML — something that takes the cluster for an evening has to say so where it is
    # STARTED.
    if not str(spec.get("description") or "").strip():
        problems.append(f"{name}: no `description:` — it labels the panel button and "
                        f"`sparky run`, and a multi-hour campaign has to say what it does "
                        f"before someone presses it.")
    if not str(spec.get("estimate") or "").strip():
        problems.append(f'{name}: no `estimate:` (e.g. "~4 h") — the difference between 90 '
                        f"minutes and a whole night decides whether you start it now.")
    return problems
