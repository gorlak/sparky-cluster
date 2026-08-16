"""Test-suite guardrails: nothing here may touch the live cluster.

The regiment is meant to run in seconds with no hardware (ADR-0011), and every test that
quietly reads real host state has failed for a reason unrelated to the code under test —
twice on 2026-08-11 alone. Both were found the same way: something real was happening on
the box at the time, and the suite reported it as a code failure.

  * a control-panel test read the host's `systemctl`, so it passed or failed depending on
    whether a suite was running (fixed in the test itself);
  * `runner.acquire()` took the REAL `/opt/cluster/fleet.lock`, so the whole suite module's
    tests failed while a deploy held it — which is exactly when you are most likely to be
    running the suite.

The second is fixed here rather than test by test, because the hazard is a module-level
constant that any future test would inherit by default. Since ADR-0027 that constant has
one home — `fleetlock` — so this points a single name at a temp file.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _never_touch_the_real_fleet_lock(tmp_path_factory, monkeypatch):
    """Point the deploy↔run mutex at a temp file for every test.

    Taking the real one is not merely flaky — a test run would BLOCK a deploy, and a test
    that leaves it held would block every deploy until the process exits.
    """
    from sparky.foundation import fleetlock

    monkeypatch.setattr(
        fleetlock, "FLEET_LOCK", tmp_path_factory.mktemp("fleet") / "fleet.lock")
    monkeypatch.setattr(fleetlock, "_fleet_fd", None)
    yield
    # The lock is held on a module global, so a test that acquires without releasing would
    # leak the descriptor into the next one.
    if fleetlock._fleet_fd is not None:
        fleetlock.release()
