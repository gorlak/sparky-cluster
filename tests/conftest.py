"""Test-suite guardrails: nothing here may touch the live cluster.

The regiment is meant to run in seconds with no hardware (ADR-0011), and every test that
quietly reads real host state has failed for a reason unrelated to the code under test —
twice on 2026-08-11 alone. Both were found the same way: something real was happening on
the box at the time, and the suite reported it as a code failure.

  * a control-panel test read the host's `systemctl`, so it passed or failed depending on
    whether a campaign was running (fixed in the test itself);
  * `sweep.acquire()` took the REAL `/opt/cluster/fleet.lock`, so the whole sweep module's
    tests failed while a deploy held it — which is exactly when you are most likely to be
    running the suite.

The second is fixed here rather than test by test, because the hazard is a module-level
constant that any future test would inherit by default.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _never_touch_the_real_fleet_lock(request, tmp_path_factory, monkeypatch):
    """Point the campaign/deploy mutex at a temp file for every test.

    Taking the real one is not merely flaky — a test run would BLOCK a deploy, and a test
    that leaves it held would block every deploy until the process exits.

    `@pytest.mark.real_lock_paths` opts out, for the one test whose subject IS that the
    two modules name the same file.
    """
    from sparky import sweep

    if "real_lock_paths" in request.keywords:
        yield
        return

    monkeypatch.setattr(
        sweep, "FLEET_LOCK", tmp_path_factory.mktemp("fleet") / "fleet.lock")
    monkeypatch.setattr(sweep, "_fleet_fd", None)
    yield
    # The lock is held on a module global, so a test that acquires without releasing would
    # leak the descriptor into the next one.
    if sweep._fleet_fd is not None:
        sweep.release(tmp_path_factory.mktemp("unused") / "sweep.lock")
