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
import subprocess
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"
LIVE = Path("/opt/cluster/ansible")            # deploy-owned runtime copy ansible runs from
HARNESS_LIVE = Path("/opt/cluster/sparky")     # published harness (agents + the sweep runner)
# The worker, as geoff over his own key — for read-only things (the journal) that need
# no privilege at all once he's in `adm`. `~/.ssh/config` scopes his key to cluster
# hosts. The old `deploy@10.0.200.13` + deploy-key constants went with the sudo that
# used to be needed here; nothing in the harness holds a privileged path to a node now.
WORKER_HOST = "snoopy"
# `deploy` and an in-flight evaluation sweep must not interleave — one is reshaping
# the boundary while the other walks it. Both take this lock.
FLEET_LOCK = Path("/opt/cluster/fleet.lock")
# The control panel is the no-sudo live-status surface: it queries every node over
# the bounded status channel. `sparky status` reads its /status.json instead of
# shelling `sudo -u deploy ansible … systemctl` (which needs geoff's password).
# Bound to 127.0.0.1:{control_panel_port} (group_vars, default 8088).
CONTROL_PANEL_URL = "http://127.0.0.1:8088"


def _as_deploy() -> list[str]:
    """`sudo -u deploy` prefix — empty when we're already the deploy user."""
    return [] if getpass.getuser() == "deploy" else ["sudo", "-u", "deploy"]


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    """Run a command, streaming its output; returns the exit code (never raises)."""
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd).returncode


def _playbook_cmd(playbook: str, extra: list[str]) -> list[str]:
    """A playbook invocation, serialized against any in-flight sweep."""
    return [*_as_deploy(), "flock", str(FLEET_LOCK), "ansible-playbook", playbook, *extra]


def publish() -> None:
    """Mirror the repo → the deploy-owned runtime tree: ansible/ → LIVE, harness → HARNESS_LIVE."""
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


def deploy(*, dry_run: bool = False, evict: bool = False, tags: str | None = None) -> int:
    """Publish, then converge the FLEET to the allowlist (site.yml).

    No profile argument — `deploy` means *deploy the fleet*. It is selection-neutral:
    it preserves whatever is currently active if that profile is still allowlisted,
    and otherwise falls to `empty`. `evict` turns the weight-eviction PLAN into an
    apply; without it a de-allowlisted model is only reported, never silently lost.
    """
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
    publish()
    extra = ["--tags", "all,webui"] if include_webui else []
    return _run(_playbook_cmd("teardown.yml", extra), cwd=LIVE)


def panel_status() -> dict | None:
    """Live status from the control panel's /status.json (no sudo) — or None if the
    panel is unreachable (down / not deployed). See CONTROL_PANEL_URL."""
    try:
        r = httpx.get(f"{CONTROL_PANEL_URL}/status.json", timeout=5.0)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def status() -> int:
    """systemd state of the vLLM units on both nodes, via ansible (the fallback path
    when the control panel is down — shells `sudo -u deploy`, needs geoff's password)."""
    cmd = [*_as_deploy(), "ansible", "all", "-m", "shell", "-a",
           "systemctl --no-pager --lines=0 status 'vllm@*.service' 2>/dev/null; true"]
    return _run(cmd, cwd=LIVE)


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
