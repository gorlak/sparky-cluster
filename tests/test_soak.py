"""The soak regiment (ADR-0016, ADR-0011 Layer 3) — no hardware, a fake clock.

DEF-0002's clears-when is the spec: *"soak-test TP=2 for hours under concurrency —
duration alone is not the gate: a 90-min light-load soak passed 2026-08-06 and proved
nothing."* These tests pin the two things that made that 90 minutes worthless: sustained
pressure, and noticing a stall rather than only a crash.
"""

from __future__ import annotations

import itertools
import threading

from sparky.measure.instruments import soak


class _Clock:
    """Monotonic time we control, so a 45-minute soak takes milliseconds."""

    def __init__(self, step=10.0):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class _Engine:
    """Answers every request. Optionally goes silent after `stall_after` calls.

    `hold` makes each request block on a barrier until `hold` of them have arrived, which
    is how saturation is observed at all: an engine that returns instantly completes each
    request before the next is submitted, so nothing ever overlaps and `peak_inflight`
    measures a race rather than the runner's behaviour.
    """

    def __init__(self, stall_after=None, fail_after=None, hold=0):
        self.calls = 0
        self.stall_after, self.fail_after = stall_after, fail_after
        self._barrier = threading.Barrier(hold) if hold else None
        self.peak_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def chat(self, messages, model, **kw):
        with self._lock:
            self.calls += 1
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
            n = self.calls
        try:
            if self._barrier is not None:
                try:
                    self._barrier.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
            if self.stall_after and n > self.stall_after:
                threading.Event().wait(30)      # never returns within the test
            if self.fail_after and n > self.fail_after:
                raise RuntimeError("engine returned 500")
            return type("R", (), {"content": "a write-ahead log " * 5,
                                  "reasoning_content": None})()
        finally:
            with self._lock:
                self._inflight -= 1


def test_pressure_is_sustained_not_trickled():
    """The 2026-08-06 soak ran 90 minutes at light load and proved nothing, because a
    scheduler deadlock needs contention to reach. Concurrency must stay saturated."""
    engine = _Engine(hold=6)          # each request waits until 6 have arrived
    soak.run(engine, "m", minutes=0.2, concurrency=6,
             clock=_Clock(step=1.0), sleep=lambda _s: None)
    assert engine.peak_inflight >= 6, f"only {engine.peak_inflight} concurrent"


def test_a_stall_is_detected_rather_than_waited_out():
    """The failure this regiment exists for is the engine going QUIET with requests
    outstanding — invisible to any check that only looks at the end."""
    engine = _Engine(stall_after=3)
    result = soak.run(engine, "m", minutes=60, concurrency=4,
                      stall_seconds=120, clock=_Clock(step=10.0), sleep=lambda _s: None)
    assert result.stalled
    assert not result.ok
    assert result.minutes < 60, "should abandon the window once stuck, not serve it out"


def test_a_clean_run_passes_and_reports_a_rate():
    engine = _Engine()
    result = soak.run(engine, "m", minutes=0.2, concurrency=4, clock=_Clock(step=1.0), sleep=lambda _s: None)
    assert result.ok and result.completed > 0 and result.failed == 0
    assert "completed" in result.summary() and "STALLED" not in result.summary()


def test_errors_fail_the_soak_even_without_a_stall():
    """A model answering 500s is not soaking successfully, however promptly it does it."""
    engine = _Engine(fail_after=2)
    result = soak.run(engine, "m", minutes=0.2, concurrency=4, clock=_Clock(step=1.0), sleep=lambda _s: None)
    assert result.failed > 0 and not result.ok


def test_progress_is_reported_during_the_window():
    """A regiment silent for 45 minutes is indistinguishable from a hung one — which is
    exactly how the early suites felt, where the only way to tell was to watch the GPU."""
    seen = []
    soak.run(_Engine(), "m", minutes=5, concurrency=4,
             on_progress=seen.append, clock=_Clock(step=10.0), sleep=lambda _s: None)
    assert seen, "no progress reported"
    assert "min" in seen[0]


def test_the_default_window_is_short_enough_to_actually_get_run():
    """ADR-0016 wants 35-55 min to catch the DEF-0002 class, but a default that long is a
    default that gets skipped. Long soaks are opt-in per job."""
    assert soak.DEFAULT_MINUTES <= 15
    assert soak.DEFAULT_CONCURRENCY >= 4


def test_detecting_a_stall_returns_promptly_rather_than_hanging_on_it():
    """The bug this pins: `with ThreadPoolExecutor(...)` calls shutdown(wait=True) on exit,
    which blocks on the very request that is stuck. The regiment built to detect a hang
    would hang while reporting one — and hold the whole suite behind it."""
    import time as _t
    engine = _Engine(stall_after=2)
    t0 = _t.monotonic()
    result = soak.run(engine, "m", minutes=60, concurrency=4, stall_seconds=60,
                      clock=_Clock(step=10.0), sleep=lambda _s: None)
    elapsed = _t.monotonic() - t0
    assert result.stalled
    assert elapsed < 5, f"took {elapsed:.1f}s — shutdown waited on the stuck request"
