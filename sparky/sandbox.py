"""Client for `vllm-sandbox` — run one benchmark answer under confinement (ADR-0024).

Thin on purpose, exactly like `suitectl`: the trigger owns the confinement, and a second
opinion about it here would be a second thing to keep right. This module's whole job is to
hand over a job and read a verdict.

It passes the toolchain through without interpreting it — the name comes from a set's
manifest and means something only to the trigger's fixed dict (ADR-0024 §2).

Injected into `coding.run` as its `execute`, which is what lets the scorer be tested with
no grant, no systemd and no cluster.
"""

from __future__ import annotations

import json
import os
import subprocess

SANDBOX_BIN = "/usr/local/sbin/vllm-sandbox"
# The trigger caps the run at 10s inside the unit; this is the outer bound on the whole
# round trip, including systemd starting and reaping the unit.
CALL_TIMEOUT = 60.0


def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def available() -> bool:
    """Is the grant deployed? Checked so a coding run can refuse with something useful
    rather than failing every problem identically for one environmental reason."""
    return os.path.exists(SANDBOX_BIN)


def execute(code: str, tests: str, *, toolchain: str, support: dict | None = None,
            seq: int = 0) -> tuple[str, str, list[dict]]:
    """`(verdict, detail, cases)` for one answer.

    `support` is whatever files the problem supplies alongside its tests; what they mean is
    the toolchain's business, and this module only forwards them.

    The third element is one row per test case the toolchain ran — `test`, `weight`, `ok`.
    A stage that ends before any case runs returns none, which is correct: there is nothing
    to weigh.

    Every failure path returns a verdict rather than raising: the caller is scoring a
    dozen problems, and one sandbox hiccup must cost one item, not the run.
    """
    job = json.dumps({"toolchain": toolchain, "code": code, "tests": tests,
                      "support": support or {}, "seq": seq})
    try:
        done = subprocess.run([*_sudo(), SANDBOX_BIN], input=job, capture_output=True,
                              text=True, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "timeout", f"sandbox call exceeded {CALL_TIMEOUT:g}s", []
    except OSError as exc:
        return "no_answer", f"cannot reach {SANDBOX_BIN}: {exc}", []

    cases, verdict = [], None
    for line in (done.stdout or "").strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("verdict"):
            verdict = row
        elif "test" in row:
            cases.append(row)
    if verdict:
        return str(verdict["verdict"]), str(verdict.get("detail", "")), cases
    # No verdict at all means the TRIGGER failed, not the answer — a missing grant, a
    # sudo refusal. Said plainly, because the alternative is a model scoring 0% for a
    # reason that has nothing to do with the model.
    stderr = (done.stderr or "").strip().splitlines()
    return "no_answer", (f"sandbox produced no verdict: "
                         f"{stderr[-1] if stderr else f'exit {done.returncode}'}"), cases
