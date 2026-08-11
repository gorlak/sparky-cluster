"""Start, stop and read a detached runbook run (ADR-0021).

The client half of `/usr/local/sbin/vllm-runbook`, and deliberately thin — every decision
about *what may run* lives in the trigger, which is the copy a network caller also reaches.
Duplicating the allowlist check here would produce a second answer to "which runbooks
exist", and the second answer is always the one that is wrong.

This mirrors `sparky.activate`'s relationship to the reconciler: the CLI and the control
panel are two callers of the same fixed program, not of each other.

**Reading is not privileged.** Only `start` and `stop` go through sudo; unit state comes
from plain `systemctl show` and the log is a file. That asymmetry is deliberate — "is it
still going, and what has it done?" must be answerable when nothing else is.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# These four must agree with the trigger, which composes the same paths from its own
# constants. tests/test_runbookctl.py asserts they do — a drift here would fail at the
# worst moment, with a run that starts and a log nobody can find.
RUNBOOK_BIN = "/usr/local/sbin/vllm-runbook"
UNIT = "sparky-runbook.service"
LOG_DIR = Path("/opt/cluster/runbook-logs")
INSTALLED_DIR = Path("/opt/cluster/runbooks")


def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def log_path(name: str) -> Path:
    return LOG_DIR / f"{name}.log"


def running() -> str | None:
    """The runbook currently running, or None.

    Read from the unit's own description rather than tracked anywhere: systemd already
    knows, and a second record of it would be a second thing that can be stale.
    """
    out = subprocess.run(
        ["systemctl", "show", UNIT, "-p", "ActiveState", "-p", "Description"],
        capture_output=True, text=True)
    fields = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    if fields.get("ActiveState") not in ("active", "activating", "deactivating"):
        return None
    return fields.get("Description", "").removeprefix("sparky runbook: ") or UNIT


def last_exit() -> tuple[str, int | None]:
    """`(ActiveState, exit status)` of the last run — how a finished run reports itself.

    The trigger deliberately does not `--collect` the unit, so this survives the run: "did
    last night's campaign finish clean?" is most of why any of this is logged.
    """
    out = subprocess.run(
        ["systemctl", "show", UNIT, "-p", "ActiveState", "-p", "ExecMainStatus"],
        capture_output=True, text=True)
    fields = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    try:
        code = int(fields.get("ExecMainStatus", ""))
    except ValueError:
        code = None
    return fields.get("ActiveState", "unknown"), code


def start(name: str) -> int:
    """Ask the trigger to start it. The trigger validates the name; we do not."""
    return subprocess.run([*_sudo(), RUNBOOK_BIN, "start", name]).returncode


def stop() -> int:
    return subprocess.run([*_sudo(), RUNBOOK_BIN, "stop"]).returncode


def follow(name: str, *, once: bool = False) -> int:
    """Tail the log. `once` prints what is there and returns.

    `tail -f` rather than `journalctl -f`: the unit writes a file, and the file is the
    whole history of that runbook's runs — a runbook resumes, so its runs are episodes of
    one campaign and reading them together is the point.
    """
    path = log_path(name)
    if not path.exists():
        print(f"no log at {path} — {name} has not run here yet")
        return 1
    args = ["tail", "-n", "200"] + ([] if once else ["-f"]) + [str(path)]
    try:
        return subprocess.run(args).returncode
    except KeyboardInterrupt:
        # Ctrl-C detaches the reader, never the run. That distinction is the feature.
        print(f"\n[detached — {name} is still running; ./sparky.sh run --stop to end it]")
        return 0
