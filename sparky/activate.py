"""`activate` — choose the live model (ADR-0018).

The one operation that changes what's serving, and it needs **no root**. Two steps,
both trivial by design:

  1. **the request** — write the desired profile to a group-writable file
     (`/opt/cluster/desired-profile`). No sudo at all.
  2. **the trigger** — run the fixed reconciler through the single-command sudoers
     entry. It re-validates the request against the installed env files, reconciles
     this node, invokes each involved worker over the forced-command SSH channel, and
     collects per-node results. Synchronous: its exit code and stdout are the answer.

A one-line statement of the whole model: **the agent gets `activate`; humans get
`deploy`.** Nothing here can install a model, change a flag, or grant itself
anything — the allowlist and the installed env files are the policy, and only
`deploy` writes them.
"""

from __future__ import annotations

import getpass
import grp
import json
import os
import subprocess
import time
from pathlib import Path

from sparky import topology
from sparky.api import VllmClient

ACTIVATE_BIN = "/usr/local/sbin/vllm-activate"
DESIRED_PROFILE = Path("/opt/cluster/desired-profile")
ALLOWLIST_FILE = Path("/opt/vllm/engines/allowlist")
ACTIVATE_GROUP = "activate"
FLEET_STATE = Path("/opt/cluster/fleet.json")
EMPTY = "empty"


def read_allowlist(path: Path = ALLOWLIST_FILE) -> list[str]:
    """What this cluster will actually accept — the deploy-written file the
    reconciler re-validates against, not the repo's profiles. They differ exactly
    when the repo has moved ahead of the last deploy, which is the case worth
    catching early rather than at the reconciler."""
    names = [EMPTY]
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in names:
                names.append(line)
    except OSError:
        return []
    return names


def requested(path: Path = DESIRED_PROFILE) -> str | None:
    """The last profile asked for, whatever came of it."""
    try:
        return path.read_text().strip().splitlines()[0].strip() or None
    except (OSError, IndexError):
        return None


def live_profile() -> str | None:
    """The last profile that actually came up (reconciler-written)."""
    current = topology.load_current_topology()
    return (current or {}).get("profile")


def write_request(profile: str, profile_path: Path = DESIRED_PROFILE) -> None:
    """Write the activation request — group-writable, no sudo.

    Deliberately truncated **in place** rather than written-and-renamed: the file is
    shared by every member of the activation group (geoff, the panel service, the
    agent), and it is the *inode's* `activator:activate 0664` that lets them all write
    it. A rename would hand ownership to whoever activated last and lock the others
    out. Atomicity buys nothing here — a torn read is not a valid profile name, and
    the reconciler treats anything it can't parse as `empty`.
    """
    with open(profile_path, "w") as handle:
        handle.write(profile + "\n")


def group_diagnosis(group: str = ACTIVATE_GROUP) -> str:
    """Why can't this process write the request? Almost always a stale session.

    Group membership is granted at LOGIN, so the deploy that first creates the
    `activate` group cannot retrofit it onto shells that were already open — and the
    resulting `PermissionError` looks like a broken cluster rather than a stale
    session. Say which of the two it is instead of guessing.
    """
    try:
        members = grp.getgrnam(group).gr_mem
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return (f"the '{group}' group does not exist on this host — this cluster has "
                f"not been deployed since ADR-0018. Run: ./sparky.sh deploy")
    user = getpass.getuser()
    in_session = gid in os.getgroups()
    if in_session:
        return (f"you ARE in '{group}' and the file is group-writable, so this is not a "
                f"group problem — check: ls -l {DESIRED_PROFILE}")
    if user in members:
        return (f"you are a member of '{group}' but THIS SESSION predates it — group "
                f"membership is granted at login. Fix it in one of two ways:\n"
                f"    newgrp {group}          (this shell, right now)\n"
                f"    sg {group} -c '<cmd>'   (one command, no re-login)\n"
                f"  Logging out and back in makes it permanent.")
    return (f"{user} is not a member of '{group}'. A deploy adds them: ./sparky.sh deploy")


def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def reconcile(*, force: bool = False, preserve: bool = False,
              bin_path: str = ACTIVATE_BIN) -> subprocess.CompletedProcess:
    """Trigger the reconciler through the single-command sudoers entry. `sudo -n`
    on purpose: the grant is passwordless for the activation group, so a password
    prompt means the grant is missing — better to fail loudly than to hang an agent."""
    cmd = [*_sudo(), bin_path]
    if force:
        cmd.append("--force")
    if preserve:
        cmd.append("--preserve")
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, text=True)


def activate(profile: str, *, force: bool = False) -> int:
    """Request `profile` and reconcile the fleet to it."""
    allowed = read_allowlist()
    if allowed and profile not in allowed:
        print(f"'{profile}' is not activatable on this cluster.\n"
              f"  activatable: {', '.join(allowed)}\n"
              f"  A profile becomes activatable when a `./sparky.sh deploy` installs it "
              f"and it is not marked `blocked: true`.")
        return 2
    if not allowed:
        print(f"No allowlist at {ALLOWLIST_FILE} — has this cluster ever been deployed? "
              f"Run `./sparky.sh deploy` first.")
        return 2
    try:
        write_request(profile)
    except PermissionError:
        print(f"cannot write the activation request at {DESIRED_PROFILE}.\n"
              f"  {group_diagnosis()}")
        return 2
    return reconcile(force=force).returncode


def wait_for_ready(timeout: float = 1800.0, poll: float = 10.0) -> bool:
    """Block until every engine of the live topology answers, or `timeout`.

    The reconciler returns as soon as systemd has accepted the start — a big model
    then spends many minutes loading weights — so "activated" and "serving" are
    genuinely different moments and the caller usually wants the second one.
    """
    current = topology.load_current_topology()
    engines = (current or {}).get("engines", [])
    if not engines:
        return True
    deadline = time.monotonic() + timeout
    pending = {e["name"]: e for e in engines}
    while pending and time.monotonic() < deadline:
        for name, engine in list(pending.items()):
            with VllmClient(engine["api_url"], timeout=10.0) as client:
                if client.is_ready():
                    print(f"  {name}: ready at {engine['api_url']}")
                    del pending[name]
        if pending:
            time.sleep(poll)
    return not pending


def fleet_state(path: Path = FLEET_STATE) -> dict | None:
    """What the last `deploy` provisioned — the allowlist and per-node placement."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def whoami() -> str:
    return getpass.getuser()


class NotLive(RuntimeError):
    """A profile did not reach "serving". Carries WHICH stage failed, and the exit code.

    The code is preserved rather than flattened because callers distinguish them: 2 means
    the reconciler REFUSED (unknown profile, no allowlist — a request that was never going
    to work), 1 means it was accepted and then failed to come up. Collapsing them turned
    "you asked for a profile that does not exist" into a generic failure.
    """

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def bring_up(profile: str, *, force: bool = False, wait: bool = True,
             smoke: bool = None, on_event=None) -> None:
    """Make `profile` live, or raise. **This is what "activate" should have meant.**

    `activate()` returns `reconcile().returncode`, which means *systemd accepted the
    start* — a big model then spends ten minutes loading weights. "Activated" and
    "serving" are different moments, and every caller was expected to know that and call
    `wait_for_ready()` itself. The CLI remembered. The sweep runner did not, so on
    2026-08-10 it fired three regiments at an engine still reading 75 GiB off disk: the
    tools regiment reported four ConnectErrors, and bench and quality then reported
    SUCCESS in under three seconds each, having measured nothing.

    A return code that means something weaker than the caller assumes is a contract bug,
    not a caller bug. This is the contract: it returns only when the profile is live, and
    raises `NotLive` otherwise — so forgetting to wait is no longer expressible.

    `smoke` defaults to True for real profiles and is skipped for `empty`, which has no
    engines to gate.
    """
    def emit(msg: str) -> None:
        if on_event:
            on_event(msg)

    rc = activate(profile, force=force)
    if rc != 0:
        raise NotLive(f"activate({profile}) exited {rc} — the reconciler refused or a "
                      f"node failed; the fleet was driven to empty rather than guessing",
                      code=rc)
    if profile == EMPTY or not wait:
        return

    emit(f"{profile}: waiting for engines to answer")
    if not wait_for_ready():
        raise NotLive(f"{profile} never answered — activated, but no engine reached "
                      f"readiness. Check `sparky logs head`")

    if smoke is False:
        return
    # The gate is what distinguishes "the API answers" from "it serves the shapes callers
    # send". A profile with no tool parser answers /v1/models and 400s the moment Open
    # WebUI attaches tools (that shipped once); measuring it would produce numbers for a
    # configuration nobody can use.
    #
    # Imported at CALL time, not module scope: the gate lives in `cli`, and `cli` imports
    # this module. Injecting it as a parameter would be tidier but reintroduces the bug
    # this function exists to remove — a caller who forgets the gate gets an ungated
    # bring-up and no complaint.
    from sparky.cli import SMOKE_REPORT, _smoke
    if _smoke(None, str(SMOKE_REPORT)) != 0:
        raise NotLive(f"{profile} came up but FAILED the smoke gate — it answers, but not "
                      f"the shapes callers send. See {SMOKE_REPORT}")
    emit(f"{profile}: live and gated")
