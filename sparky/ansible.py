"""Drive ansible from sparky — the operator entrypoint (ADR-0015).

Since ADR-0018, ansible is the **`deploy`** engine only: human-initiated,
password-gated, convergent, whole-fleet — never the activation engine, and never
reachable from a web API. Two phases, same as the retired `make deploy`: **publish**
the repo to the deploy-owned runtime tree under `/opt/cluster`, then run
`ansible-playbook` there **as the deploy user** (its NOPASSWD sudo is the automation
gate — `sudo -u deploy` prompts for geoff's password interactively).

Choosing what serves is `sparky.activate`, which touches none of this and needs no
password: **the agent gets `activate`; humans get `deploy`.**
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"
LIVE = Path("/opt/cluster/ansible")            # deploy-owned runtime copy ansible runs from
HARNESS_LIVE = Path("/opt/cluster/sparky")     # published harness source (the `harness` role installs it)
# The runbooks that may be INSTANCED (ADR-0021). Published, not merely readable in the
# repo: both callers that can start a run — the CLI and the panel — name a member of this
# set, and a caller reachable from the network must not be able to run a file that merely
# happens to be in a git checkout. Adding a runbook is therefore a deploy.
RUNBOOKS_LIVE = Path("/opt/cluster/runbooks")
# The worker, as geoff over his own key — for read-only things (the journal) that need
# no privilege at all once he's in `adm`. `~/.ssh/config` scopes his key to cluster
# hosts. The old `deploy@10.0.200.13` + deploy-key constants went with the sudo that
# used to be needed here; nothing in the harness holds a privileged path to a node now.
WORKER_HOST = "snoopy"
# `deploy` and an in-flight campaign must not interleave — one is reshaping the boundary
# while the other walks it. Both take this lock: `flock(1)` here, `fcntl.flock` in
# sweep.py, which is the only mechanism a shell and a Python process can share.
#
# Until 2026-08-11 this comment was a claim rather than a fact. `deploy` took it and the
# sweep took a DIFFERENT file (`sweep.lock`), so nothing was excluded — a deploy could
# re-render engine files, pull an image or evict weights in the middle of a measurement.
# tests/test_ansible.py pins the two constants together now.
FLEET_LOCK = Path("/opt/cluster/fleet.lock")
# The control panel is the ONLY live-status surface, and it needs no sudo: it queries
# every node over the bounded status channel. There is deliberately no ansible/systemd
# fallback any more — reading status is not privileged (plain `systemctl is-active`, or
# the reconciler's `--status` verb, work as geoff on every node), so a password-gated
# alternative bought nothing and fired at the worst possible moment.
# Bound to 127.0.0.1:{control_panel_port} (group_vars, default 8088).
CONTROL_PANEL_URL = "http://127.0.0.1:8088"
# Generous on purpose. The panel probes every node, so a node that is DOWN — rebooting,
# wedged — makes it slow rather than absent, and that is exactly when status gets read.
# A budget tighter than the panel's worst case turns "slow" into "unreachable" and used
# to send this straight to the password-prompting fallback.
PANEL_TIMEOUT = 20.0


def _as_deploy() -> list[str]:
    """`sudo -u deploy` prefix — empty when we're already the deploy user."""
    return [] if getpass.getuser() == "deploy" else ["sudo", "-u", "deploy"]


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    """Run a command, streaming its output; returns the exit code (never raises)."""
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd).returncode


def campaign_holding_the_fleet() -> bool:
    """Is a measurement campaign holding the fleet lock right now?

    Asked BEFORE invoking the playbook so the refusal can explain itself. `flock` alone
    would be correct and awful: a deploy would sit silent for however long the campaign
    has left, which for `all` is most of a night, and the operator would reasonably
    conclude it had hung.
    """
    import fcntl
    try:
        fd = os.open(FLEET_LOCK, os.O_RDWR | os.O_CREAT, 0o664)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _refuse_during_campaign() -> bool:
    """Print why a deploy is being declined, if it is. True means: do not proceed."""
    if not campaign_holding_the_fleet():
        return False
    from sparky import runbookctl
    running = runbookctl.running()
    print(f"REFUSING: a measurement campaign holds the fleet"
          f"{f' ({running})' if running else ''}.")
    print("A deploy re-renders engine files, pulls images and can evict weights — doing")
    print("that underneath a running campaign produces numbers belonging to no")
    print("configuration, and the damage is invisible afterwards.")
    print()
    print("  ./sparky.sh run            # what is running, and how far along")
    print("  ./sparky.sh run --stop     # stop it; it resumes where it left off")
    return True


def _playbook_cmd(playbook: str, extra: list[str]) -> list[str]:
    """A playbook invocation, serialized against any in-flight campaign.

    `flock` stays even though the caller pre-checks: the pre-check is for the message,
    this is for the race between checking and starting.
    """
    return [*_as_deploy(), "flock", str(FLEET_LOCK), "ansible-playbook", playbook, *extra]


def publish() -> None:
    """Mirror the repo → the deploy-owned runtime tree: ansible/, the harness, the runbooks."""
    _run([
        "rsync", "-rlc", "--delete", "--no-perms", "--no-owner", "--no-group",
        "--exclude=.git", "--exclude=__pycache__", "--exclude=*.retry", "--exclude=.ansible",
        f"{ANSIBLE_DIR}/", f"{LIVE}/",
    ])
    HARNESS_LIVE.mkdir(parents=True, exist_ok=True)
    _run([
        "rsync", "-rlc", "--no-perms", "--no-owner", "--no-group",
        "--exclude=__pycache__", "--exclude=.venv", "--exclude=*.egg-info",
        f"{REPO_ROOT}/sparky", f"{REPO_ROOT}/pyproject.toml", f"{REPO_ROOT}/uv.lock", f"{HARNESS_LIVE}/",
    ])
    # `--delete`, like the ansible tree: deleting a runbook's .yml must actually retire it,
    # or the installed set becomes an ever-growing record of everything ever authored.
    RUNBOOKS_LIVE.mkdir(parents=True, exist_ok=True)
    _run([
        "rsync", "-rlc", "--delete", "--no-perms", "--no-owner", "--no-group",
        f"{REPO_ROOT}/runbooks/", f"{RUNBOOKS_LIVE}/",
    ])


def deploy(*, dry_run: bool = False, evict: bool = False, tags: str | None = None) -> int:
    """Publish, then converge the FLEET to the allowlist (site.yml).

    No profile argument — `deploy` means *deploy the fleet*. It is selection-neutral:
    it preserves whatever is currently active if that profile is still allowlisted,
    and otherwise falls to `empty`. `evict` turns the weight-eviction PLAN into an
    apply; without it a de-allowlisted model is only reported, never silently lost.

    Refuses while a campaign holds the fleet — checked BEFORE publishing, because
    publishing already changes what a running campaign would read next.
    """
    if _refuse_during_campaign():
        return 2
    publish()
    extra: list[str] = []
    if dry_run:
        extra += ["--check", "--diff"]
    if evict:
        extra += ["-e", "evict_weights=true"]
    if tags:
        extra += ["--tags", tags]
    return _run(_playbook_cmd("site.yml", extra), cwd=LIVE)


def teardown(*, include_webui: bool = False) -> int:
    """Break-glass stop of every engine on every node, as `deploy`.

    The normal way to stop serving is `sparky activate empty` — unprivileged and
    marker-transactional. This is the privileged fallback for when the reconciler
    itself is broken or missing.
    """
    if _refuse_during_campaign():
        return 2
    publish()
    extra = ["--tags", "all,webui"] if include_webui else []
    return _run(_playbook_cmd("teardown.yml", extra), cwd=LIVE)


def panel_status() -> dict | None:
    """Live status from the control panel's /status.json (no sudo) — or None if the
    panel is unreachable (down / not deployed). See CONTROL_PANEL_URL."""
    try:
        r = httpx.get(f"{CONTROL_PANEL_URL}/status.json", timeout=PANEL_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def logs(node: str = "head") -> int:
    """Follow the vLLM journal on a node — with **no privilege at all**.

    Reading a system unit's journal needs only `adm` (or `systemd-journal`) group
    membership, which the activate role grants geoff. The old `sudo journalctl` was
    both unnecessary and a passwordless root shell waiting to happen (journalctl pages
    through less; `!sh` is a root shell), so ADR-0018 dropped the grant along with it.
    The worker is reached over geoff's own SSH key, as geoff.
    """
    cmd = ["journalctl", "-u", "vllm@*", "-f"]
    if node in ("head", "sparky"):
        return _run(cmd)
    return _run(["ssh", WORKER_HOST, *cmd])


def lint() -> int:
    """ADR-0011 Layer 1: the playbooks parse, and the fleet they'd be given is legal.

    Since ADR-0018 the playbooks take no profile argument, so syntax-checking is one
    run each — and the per-profile half of this check moved up a level, to validating
    the allowlist itself (unique engine names fleet-wide, the one front port, flags
    that survive the env-file round trip).
    """
    from sparky.fleet import FleetError, load_fleet

    for playbook in ("site.yml", "teardown.yml"):
        rc = subprocess.run(
            ["ansible-playbook", playbook, "--syntax-check"],
            cwd=ANSIBLE_DIR, stdout=subprocess.DEVNULL,
        ).returncode
        if rc:
            return rc

    fleet = load_fleet()
    try:
        fleet.validate()
    except FleetError as exc:
        print(f"fleet is not deployable:\n{exc}")
        return 1
    print(f"lint OK — site.yml + teardown.yml parse cleanly; "
          f"{len(fleet.profiles)} profiles ({len(fleet.allowlist)} activatable), "
          f"{len(fleet.placements)} engines, {len(fleet.models)} models validate")
    return 0
