"""The suite runner (ADR-0016, ADR-0011 Layer 3) — no hardware, no models.

Every test here is a specific thing that went wrong during the 2026-08-09/10 suite,
when the suite was a bash script in /tmp. The runner exists because of them, so they are
the specification.
"""

from __future__ import annotations

import json

import pytest

from sparky.measure.loop import runner


def _jobs(*specs):
    return [runner.Job(profile=p, regiments=tuple(r)) for p, r in specs]


def _ok(_job):
    return "fine"


# --- the job list -----------------------------------------------------------

def test_defaults_are_merged_but_an_explicit_list_wins():
    """The one non-literal convenience. A job that names its regiments is read as-is —
    the long soak belongs to the risky profile, not to every profile."""
    jobs = runner.load_jobs({
        "defaults": {"regiments": ["smoke", "bench"]},
        "jobs": [{"profile": "a"}, {"profile": "b", "regiments": ["soak"]}],
    })
    assert jobs[0].regiments == ("smoke", "bench")
    assert jobs[1].regiments == ("soak",)


def test_a_job_with_no_regiments_anywhere_is_an_error():
    """Silently running nothing would look like a suite that passed."""
    with pytest.raises(ValueError):
        runner.load_jobs({"jobs": [{"profile": "a"}]})


def test_a_bare_string_job_is_allowed():
    jobs = runner.load_jobs({"defaults": {"regiments": ["smoke"]}, "jobs": ["a"]})
    assert jobs[0].profile == "a" and jobs[0].regiments == ("smoke",)


# --- resumption (the brownout) ----------------------------------------------

def test_completed_regiments_are_not_repeated_after_a_restart(tmp_path):
    """THE BROWNOUT, 2026-08-09: a suite died mid-run and a finished 25-minute quality
    eval was re-run from scratch, because nothing was recorded until the end."""
    crumbs = tmp_path / "state.json"
    ran = []
    regiments = {"quality": lambda j: ran.append(("quality", j.key)),
                 "bench": lambda j: ran.append(("bench", j.key))}

    state = runner.run(_jobs(("a", ["quality", "bench"])), activate=lambda p: None,
                      regiments=regiments, breadcrumbs=crumbs)
    assert len(ran) == 2

    # a fresh process resumes from disk
    resumed = runner.load_state(crumbs)
    runner.run(_jobs(("a", ["quality", "bench"])), activate=lambda p: None,
              regiments=regiments, state=resumed, breadcrumbs=crumbs)
    assert len(ran) == 2, "resumed run repeated finished work"
    assert state.done == {("a", "quality"), ("a", "bench")}


def test_state_is_written_after_every_regiment_not_every_job(tmp_path):
    """Per-JOB granularity would throw away a 45-minute soak because the bench after it
    failed. The unit of work is the regiment."""
    crumbs = tmp_path / "state.json"
    seen = []

    def second(_job):
        seen.append(json.loads(crumbs.read_text())["done"])
        return "ok"

    runner.run(_jobs(("a", ["first", "second"])), activate=lambda p: None,
              regiments={"first": _ok, "second": second}, breadcrumbs=crumbs)
    assert seen == [[["a", "first"]]], "first regiment was not durable before the second ran"


def test_a_partial_write_cannot_corrupt_the_resume_point(tmp_path):
    """State is written atomically. A suite interrupted during the write must not leave a
    truncated file — a resume that cannot parse its own breadcrumbs is a restart."""
    crumbs = tmp_path / "state.json"
    st = runner.Breadcrumbs(done={("a", "bench")})
    runner.save_state(st, crumbs)
    assert not crumbs.with_suffix(".tmp").exists()
    assert runner.load_state(crumbs).done == {("a", "bench")}


def test_unreadable_state_starts_clean_rather_than_raising(tmp_path):
    """Losing the breadcrumbs costs a re-run. Refusing to start costs the whole suite."""
    crumbs = tmp_path / "state.json"
    crumbs.write_text("{ not json")
    assert runner.load_state(crumbs).done == set()


# --- quarantine (the node-killer) -------------------------------------------

def test_a_profile_that_fails_to_activate_is_quarantined_and_skipped():
    """DEF-0004: MiniMax-AWQ exhausted host memory during weight load and FROZE sparky —
    recovered only by a power cycle. A runner that retries that model, or reaches it again
    later in the list, turns one bad model into a lost night."""
    tried = []

    def activate(profile):
        tried.append(profile)
        if profile == "killer":
            raise RuntimeError("host OOM during weight load")

    state = runner.run(_jobs(("killer", ["bench"]), ("killer", ["quality"]), ("good", ["bench"])),
                      activate=activate, regiments={"bench": _ok, "quality": _ok},
                      breadcrumbs=None)
    assert tried == ["killer", "good"], "quarantined profile was activated again"
    assert "killer" in state.quarantined
    assert "good" not in state.quarantined


def test_a_failing_regiment_does_not_quarantine_a_healthy_profile():
    """A bad measurement is not a bad model. Quarantine is for bring-up, so one flaky
    bench must not remove a working profile from the rest of its own suite."""
    def bad(_job):
        raise RuntimeError("bench blew up")

    state = runner.run(_jobs(("a", ["bench", "quality"])), activate=lambda p: None,
                      regiments={"bench": bad, "quality": _ok}, breadcrumbs=None)
    assert state.quarantined == {}
    assert [(o.regiment, o.ok) for o in state.outcomes] == [("bench", False), ("quality", True)]


def test_a_failed_regiment_is_recorded_as_done():
    """"It failed" is a verdict. Re-running it on resume would spend the cluster to learn
    the same thing twice."""
    def bad(_job):
        raise RuntimeError("nope")
    state = runner.run(_jobs(("a", ["bench"])), activate=lambda p: None,
                      regiments={"bench": bad}, breadcrumbs=None)
    assert ("a", "bench") in state.done


def test_an_unknown_regiment_is_reported_not_silently_skipped():
    state = runner.run(_jobs(("a", ["typo"])), activate=lambda p: None,
                      regiments={"bench": _ok}, breadcrumbs=None)
    assert state.outcomes[0].ok is False
    assert "no such regiment" in state.outcomes[0].detail


# --- exclusive ownership (the contention bug) -------------------------------

def test_a_second_run_refuses_to_start(tmp_path):
    """2026-08-10: a stray manual bench overlapped the suite's own bench on one engine and
    contaminated a TP=1 baseline. Two SWEEPS would be worse — each activating models the
    other is measuring, producing numbers that look fine and belong to no configuration."""
    lock = tmp_path / "runner.lock"
    runner.acquire(lock)
    with pytest.raises(runner.SuiteBusy):
        runner.acquire(lock)
    runner.release(lock)
    runner.acquire(lock)          # released, so it is available again


def test_a_stale_lock_from_a_killed_run_is_reclaimed(tmp_path):
    """A SIGKILLed runner's release() never runs, so its lock lingers naming a dead pid. The
    next run must reclaim it AT ONCE — the ADR-0009 idea, the run lock's version: the holder's
    DEATH makes the lock stale, not a clock. (The footgun that had me hand-`rm` a 2-minute-old
    lock, because the age threshold still thought it was live.)"""
    import os, subprocess
    lock = tmp_path / "runner.lock"
    dead = subprocess.Popen(["true"]); dead.wait()       # a real pid that is now gone
    lock.write_text(f"pid={dead.pid} started=1\n")        # a YOUNG lock, but the holder is dead
    runner.acquire(lock)                                  # must NOT raise — reclaim the stale lock
    assert f"pid={os.getpid()}" in lock.read_text()       # ...and this process now owns it


def test_a_live_run_is_never_reclaimed_by_age(tmp_path):
    """The mirror image — and the latent bug the old clock-based check carried. The `all`
    suite runs ~7h, so ANY age threshold would let a second suite steal its lock mid-run and
    interleave two measurement passes. A live holder is refused no matter how old it looks."""
    import os, time
    lock = tmp_path / "runner.lock"
    lock.write_text(f"pid={os.getpid()} started=1\n")     # this process is alive
    old = time.time() - 30 * 3600
    os.utime(lock, (old, old))                             # ...and the lock looks 30h old
    with pytest.raises(runner.SuiteBusy):
        runner.acquire(lock)


def test_release_is_safe_when_the_lock_is_already_gone(tmp_path):
    runner.release(tmp_path / "never-existed.lock")


# --- reporting --------------------------------------------------------------

def test_summary_names_what_failed_and_what_was_quarantined():
    state = runner.Breadcrumbs(
        outcomes=[runner.Outcome("a", "bench", True, 60.0),
                  runner.Outcome("b", "quality", False, 30.0, "timeout")],
        quarantined={"c": "host OOM"})
    out = runner.summary(state)
    assert "1 passed, 1 failed" in out
    assert "b / quality" in out and "timeout" in out
    assert "QUARANTINED c" in out


# --- detached operation (a dropped ssh session must not kill a 2-hour suite) --

def test_progress_is_readable_from_the_breadcrumbs_alone(tmp_path):
    """A detached suite outlives the shell that started it, so "how far along is it?" has
    to be answerable from a fresh session with the original process gone. It reads the
    same file the runner writes, so there is no second source to drift."""
    crumbs = tmp_path / "state.json"
    runner.run(_jobs(("a", ["bench"]), ("b", ["bench"])), activate=lambda p: None,
              regiments={"bench": _ok}, breadcrumbs=crumbs)
    fresh = runner.load_state(crumbs)          # a different process would see exactly this
    out = runner.progress(fresh, _jobs(("a", ["bench"]), ("b", ["bench"])))
    assert "2/2 regiments complete" in out
    assert "ok   a / bench" in out


def test_holder_reports_whether_a_run_owns_the_cluster(tmp_path):
    lock = tmp_path / "runner.lock"
    assert runner.holder(lock) is None
    runner.acquire(lock)
    assert "pid=" in (runner.holder(lock) or "")
    runner.release(lock)
    assert runner.holder(lock) is None
