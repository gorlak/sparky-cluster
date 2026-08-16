"""The deploy↔run mutex — one lock, one home (ADR-0027).

`deploy` re-renders engine files, pulls images and can evict weights; a measurement run
activates models and reads them. The two must never overlap, or a run produces numbers
belonging to no configuration and the damage is invisible afterwards. The lock that
enforces it is taken from both sides — `flock(1)` in the deploy shell, an advisory hold in
the run — so it belongs to neither stack. It lived as two copies (`ansible.py` and the
runner) that a test had to pin together, after they had already drifted apart once and
silently let a deploy run under a measurement. A duplicated constant guarded by a test is a
workaround for having no shared home; this is the home.

Distinct from the run's own `runner.lock` (one run at a time), which stays with the runner
— only runs care about that one.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

# The lock `deploy` takes (sparky/ansible.py, via `flock`) and a run holds for its
# duration, so a deploy cannot re-render engine files or evict weights mid-measurement.
FLEET_LOCK = Path("/opt/cluster/fleet.lock")

_fleet_fd: int | None = None


class SuiteBusy(RuntimeError):
    """The cluster is taken — a deploy holds the lock, or another run does. Raised rather
    than queued: two runs interleaving activations would each measure whatever the other
    last activated, and the failure would look like data. Re-exported by `runner` so its
    own callers keep raising and catching `runner.SuiteBusy`."""


def hold(path: Path | None = None) -> None:
    """Take `deploy`'s lock for the duration of a run, or refuse.

    An advisory flock rather than a marker file, because the other holder is `flock(1)` in
    a shell — the only mechanism both sides can speak. Non-blocking: waiting would mean a
    run that silently stalls for however long a deploy takes, and refusing says which of
    the two is in progress.

    Held on a module global. There is at most one run per process by construction, and the
    alternative — threading a file descriptor through `run()` — buys nothing.
    """
    global _fleet_fd
    # Resolved at CALL time, not bound as a default argument: a default would capture the
    # path at import and quietly ignore any later reassignment — which is how a test can
    # point both sides at a tmp file and still watch the real one.
    path = path or FLEET_LOCK
    if _fleet_fd is not None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    except OSError:
        return          # no /opt/cluster (a dev checkout) — nothing to serialize against
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SuiteBusy(
            f"a deploy is in progress — it holds {path}. A deploy re-renders engine "
            f"files and can evict weights, so measuring across one produces numbers that "
            f"belong to no configuration. Wait for it to finish, then start again "
            f"(breadcrumbs mean nothing is repeated).") from None
    _fleet_fd = fd


def release() -> None:
    """Drop the fleet lock. The run's own `runner.lock` is released separately."""
    global _fleet_fd
    if _fleet_fd is not None:
        try:
            fcntl.flock(_fleet_fd, fcntl.LOCK_UN)
            os.close(_fleet_fd)
        except OSError:
            pass
        _fleet_fd = None


def held_by_us() -> bool:
    """True when THIS process holds the lock.

    flock is per open-file-description, so a second acquire from the same process blocks
    against itself. A suite takes the lock for its whole run and then activates once per
    job — activation must NOT wait on a lock the suite itself is holding, or every run
    hangs on its first job. `activate` asks this to tell "we hold it" from "someone else
    does".
    """
    return _fleet_fd is not None


def held(path: Path | None = None) -> bool:
    """Is the fleet lock held right now? A non-blocking probe.

    Asked by `deploy` BEFORE invoking the playbook so a refusal can explain itself. `flock`
    alone would be correct and awful: a deploy would sit silent for however long a run has
    left — most of a night for `all` — and the operator would reasonably conclude it hung.
    """
    path = path or FLEET_LOCK
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
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
