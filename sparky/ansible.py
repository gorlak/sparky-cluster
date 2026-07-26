"""Drive ansible from sparky — the operator entrypoint (ADR-0015).

sparky is the outer layer; ansible is the config/execution engine it invokes. Two
phases, same as the retired `make deploy`: **publish** the repo to the deploy-owned
runtime tree under `/opt/cluster`, then run `ansible-playbook` there **as the deploy
user** (its NOPASSWD sudo is the automation gate — `sudo -u deploy` prompts for
geoff's password interactively, exactly as make did).

Run from the repo (via `./sparky.sh`); the published harness copy only runs `smoke`.
"""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"
LIVE = Path("/opt/cluster/ansible")            # deploy-owned runtime copy ansible runs from
HARNESS_LIVE = Path("/opt/cluster/sparky")     # published harness (the smoke gate's venv source)
DEPLOY_KEY = "/home/deploy/.ssh/id_ed25519"
WORKER_SSH = "deploy@10.0.200.13"
DEFAULT_PROFILE = "step-3.5-fp8"
# The control panel (User=deploy) is the no-sudo live-status surface: it already
# queries systemd on both nodes with deploy's SSH key. `sparky status` reads its
# /status.json instead of shelling `sudo -u deploy ansible … systemctl` (which needs
# geoff's password). Bound to 127.0.0.1:{control_panel_port} (group_vars, default 8088).
CONTROL_PANEL_URL = "http://127.0.0.1:8088"


def _as_deploy() -> list[str]:
    """`sudo -u deploy` prefix — empty when we're already the deploy user."""
    return [] if getpass.getuser() == "deploy" else ["sudo", "-u", "deploy"]


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    """Run a command, streaming its output; returns the exit code (never raises)."""
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd).returncode


def _playbook_cmd(playbook: str, profile: str | None, extra: list[str]) -> list[str]:
    cmd = [*_as_deploy(), "ansible-playbook", playbook]
    if profile:
        cmd += ["-e", f"@profiles/{profile}.yml"]
    return cmd + extra


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


def deploy(profile: str = DEFAULT_PROFILE, *, dry_run: bool = False) -> int:
    """Publish, then apply `profile` (site.yml). `dry_run` → `--check --diff`, no changes."""
    publish()
    extra = ["--check", "--diff"] if dry_run else []
    return _run(_playbook_cmd("site.yml", profile, extra), cwd=LIVE)


def teardown(profile: str = DEFAULT_PROFILE, *, include_webui: bool = False) -> int:
    """Publish, then stop + disable vLLM engines on both nodes (`include_webui` also stops Open WebUI)."""
    publish()
    extra = ["--tags", "all,webui"] if include_webui else []
    return _run(_playbook_cmd("teardown.yml", profile, extra), cwd=LIVE)


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
           "systemctl --no-pager --lines=0 status 'vllm-*.service' 2>/dev/null; true"]
    return _run(cmd, cwd=LIVE)


def logs(node: str = "head") -> int:
    """Follow the vLLM journal on a node (`head`/`sparky` local, else the worker over ssh)."""
    if node in ("head", "sparky"):
        return _run(["sudo", "journalctl", "-u", "vllm-*", "-f"])
    return _run([*_as_deploy(), "ssh", "-i", DEPLOY_KEY, WORKER_SSH,
                 'sudo journalctl -u "vllm-*" -f'])


def lint() -> int:
    """`ansible-playbook --syntax-check` on site.yml across every profile + teardown (ADR-0011 Layer 1)."""
    profiles = sorted((ANSIBLE_DIR / "profiles").glob("*.yml"))
    for p in profiles:
        rc = subprocess.run(
            ["ansible-playbook", "site.yml", "-e", f"@profiles/{p.name}", "--syntax-check"],
            cwd=ANSIBLE_DIR, stdout=subprocess.DEVNULL,
        ).returncode
        if rc:
            return rc
    rc = subprocess.run(
        ["ansible-playbook", "teardown.yml", "--syntax-check"],
        cwd=ANSIBLE_DIR, stdout=subprocess.DEVNULL,
    ).returncode
    if rc:
        return rc
    print(f"lint OK — site.yml across {len(profiles)} profiles + teardown.yml parse cleanly")
    return 0
