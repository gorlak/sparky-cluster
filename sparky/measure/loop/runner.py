"""The continuous-evaluation outer loop (ADR-0016) — the runner, at last.

Everything else in ADR-0016 shipped months before this did: `bench` rebuilt HTTP-native,
`quality`, `vision`. What kept getting deferred was the thing that *drives* them, and the
cost of deferring it was paid in a specific, enumerable way over 2026-08-09/10, when a
three-model suite ran from a bash script in `/tmp`:

  1. the script was lost to a brownout and rewritten from scratch;
  2. rewritten twice more — three scripts for one suite;
  3. hand-patched mid-flight twice (a readiness wait, an in-flight-activation guard);
  4. a readiness check hardcoded to the head would have finished **green** having measured
     three TP=2 profiles with no TP=1 baselines — a useless dataset that looked complete;
  5. a stray manual bench overlapped the suite's own bench on the same engine, silently
     contaminating the baseline, because nothing owned the cluster exclusively;
  6. a `trap 'kill 0' EXIT` killed the operator's shell, twice.

Each of those is one of this module's four features. That is why it is data + Python
control flow rather than a DSL: the interesting parts are resumption, exclusion and
failure policy, which are miserable in YAML and ordinary in code.

**Serial by physics, not by preference.** One model can be live fleet-wide (ADR-0018), so
the job list is a serial loop: activate, run regiments against the live engine, move on.
If the fleet ever grows past two nodes, the upgrade is a small scheduler — control flow,
already expressible here.

**Two things it deliberately does NOT do.**

*Variants.* ADR-0016 sketched `profile × variant × regiment`, where a variant was a flag
set (`fp8-kv+prefix`). ADR-0018 made serve flags deploy-rendered, so an agent cannot vary
them — a variant IS a profile. The axis is dropped rather than faked, and the paired
profiles we already keep (`…-mtp3-single` beside its sibling) are how an A/B is expressed.

*Its own measurement.* Every regiment is injected. The runner decides ORDER, RESUMPTION
and FAILURE POLICY; it must not grow opinions about what a good bench looks like, and
keeping the seam explicit is what lets the whole thing be tested with no hardware.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from sparky.foundation import fleetlock

# Breadcrumbs live beside the trend store — literally, since 2026-08-11. Same lifetime,
# same "survives a reboot" requirement, and NOT /tmp, which is precisely where the last
# runner's state went.
#
# They were directly in /opt/cluster until the first real detached run died on the first
# breadcrumb write: `save_state` is atomic (write `.tmp`, rename over the target), and
# ADR-0021 put the **sticky bit** on /opt/cluster so the activation identity's new write
# access could not be used to replace `ansible/`. Sticky also forbids renaming over a file
# you do not own — and the target was geoff's while the run is `activator`. A directory
# the activation identity owns has neither problem, and `benchmark/` already is one.
BENCHMARK_DIR = Path("/opt/cluster/benchmark")
DEFAULT_BREADCRUMBS = BENCHMARK_DIR / "breadcrumbs.json"
DEFAULT_LOCK = BENCHMARK_DIR / "runner.lock"

# The deploy↔run mutex lives in `fleetlock`, taken from both sides (ADR-0027). Re-exported
# so a suite's own callers keep raising and catching `runner.SuiteBusy` — the exception is
# raised here (another run holds `runner.lock`) and there (a deploy holds the fleet lock),
# and it is one thing: the cluster is taken.
SuiteBusy = fleetlock.SuiteBusy


@dataclass
class Job:
    profile: str
    regiments: tuple[str, ...]
    label: str | None = None       # store label; defaults to the profile name

    @property
    def key(self) -> str:
        return self.label or self.profile


@dataclass
class Outcome:
    job: str
    regiment: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class Breadcrumbs:
    """What survived the last run. Written after EVERY regiment, not every job.

    Per-regiment granularity is the whole point: a 45-minute soak must not be re-run
    because the bench that followed it failed. The 2026-08-09 brownout landed mid-suite
    and cost a completed quality run purely because nothing was recorded until the end.
    """

    done: set[tuple[str, str]] = field(default_factory=set)   # (job key, regiment)
    quarantined: dict[str, str] = field(default_factory=dict)  # profile -> why
    outcomes: list[Outcome] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "done": sorted([list(x) for x in self.done]),
            "quarantined": self.quarantined,
            "outcomes": [vars(o) for o in self.outcomes],
        }

    @classmethod
    def from_json(cls, d: dict) -> Breadcrumbs:
        return cls(
            done={tuple(x) for x in d.get("done", [])},
            quarantined=dict(d.get("quarantined", {})),
            outcomes=[Outcome(**o) for o in d.get("outcomes", [])],
        )


def load_jobs(spec: dict) -> list[Job]:
    """Flat, enumerated, reviewed as-is — what you read is what runs.

    The single non-literal convenience is `defaults.regiments`, merged into any job that
    does not name its own. Deliberately not a matrix DSL: a suite is a thing a human
    approves before it commandeers the cluster for two hours, and expansion rules are how
    an approved list stops matching what executes.
    """
    default_regiments = tuple((spec.get("defaults") or {}).get("regiments") or ())
    jobs: list[Job] = []
    for raw in spec.get("jobs") or []:
        if isinstance(raw, str):
            raw = {"profile": raw}
        regiments = tuple(raw.get("regiments") or default_regiments)
        if not regiments:
            raise ValueError(f"job {raw.get('profile')!r} has no regiments and no defaults")
        jobs.append(Job(profile=raw["profile"], regiments=regiments, label=raw.get("label")))
    if not jobs:
        raise ValueError("suite has no jobs")
    return jobs


def acquire(lock_path: Path = DEFAULT_LOCK, *, stale_after: float = 6 * 3600) -> Path:
    """Take exclusive ownership of the cluster, or refuse.

    A suite activates models; two of them interleaving would each measure whatever the
    other last activated. On 2026-08-10 a stray manual bench overlapped a suite's own
    bench on one engine and contaminated a TP=1 baseline — the mild version of this, and
    it still cost a re-run.

    A lock left by a killed process would block every future suite, so it expires. The
    window is generous because a legitimate suite with a 45-minute soak is genuinely long.
    """
    now = time.time()
    if lock_path.exists():
        try:
            age = now - lock_path.stat().st_mtime
            holder = lock_path.read_text().strip()
        except OSError:
            age, holder = 0.0, "unreadable"
        if age < stale_after:
            raise SuiteBusy(
                f"a suite is already running ({holder}, {age / 60:.0f} min ago). "
                f"Wait, or remove {lock_path} if you know it is dead.")
    fleetlock.hold()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"pid={os.getpid()} started={int(now)}\n")
    return lock_path


def release(lock_path: Path = DEFAULT_LOCK) -> None:
    fleetlock.release()
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def load_state(path: Path = DEFAULT_BREADCRUMBS) -> Breadcrumbs:
    try:
        return Breadcrumbs.from_json(json.loads(path.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return Breadcrumbs()


def save_state(state: Breadcrumbs, path: Path = DEFAULT_BREADCRUMBS) -> None:
    """Atomic where the filesystem allows it, in place where it does not.

    Atomic first: a suite interrupted *during* the write must not leave state
    unreadable, or the resume this exists for degrades into starting over.

    But the rename can be forbidden while the write is fine — a sticky directory only
    lets you rename over a file you own, and this file is shared by everyone in the
    activation group. `activate.py` hit the same wall with `desired-profile` and resolved
    it the same way, for the same reason. Falling back matters because of what the
    alternative costs: on 2026-08-11 an EPERM here killed a suite at its first
    regiment, and losing the ability to *resume* is not worth losing the run.

    A write that fails outright still raises. Silence would leave a suite running for
    hours with no resumable state, which is worse than either.
    """
    payload = json.dumps(state.to_json(), indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(payload)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        path.write_text(payload)


def run(jobs: list[Job], *, activate, regiments: dict, state: Breadcrumbs | None = None,
        on_event=None, breadcrumbs: Path | None = DEFAULT_BREADCRUMBS,
        clock=time.monotonic) -> Breadcrumbs:
    """Drive the job list. `activate(profile) -> None` raises to fail; each regiment is
    `fn(job) -> str` and raises to fail.

    **Quarantine is per PROFILE, not per regiment.** The hazard it exists for is a model
    that takes a node down (DEF-0004 froze sparky during weight load, recovered only by a
    power cycle). Once a profile has proved it cannot be brought up, every remaining job
    for it is skipped — re-activating it is how a suite turns one bad model into a whole
    lost night. A regiment that fails on a healthy engine is just a failed measurement:
    recorded, and the suite continues.
    """
    state = state or Breadcrumbs()

    def emit(msg: str) -> None:
        if on_event:
            on_event(msg)

    for job in jobs:
        if job.profile in state.quarantined:
            emit(f"skip {job.key}: quarantined ({state.quarantined[job.profile]})")
            continue

        todo = [r for r in job.regiments if (job.key, r) not in state.done]
        if not todo:
            emit(f"skip {job.key}: already complete")
            continue

        emit(f"=== {job.key} ({', '.join(todo)})")
        try:
            activate(job.profile)
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
            state.quarantined[job.profile] = why
            state.outcomes.append(Outcome(job.key, "activate", False, 0.0, why))
            emit(f"  QUARANTINED {job.profile} — {why}")
            if breadcrumbs:
                save_state(state, breadcrumbs)
            continue

        for name in todo:
            fn = regiments.get(name)
            if fn is None:
                state.outcomes.append(
                    Outcome(job.key, name, False, 0.0, "no such regiment"))
                emit(f"  {name}: NO SUCH REGIMENT")
                continue
            t0 = clock()
            try:
                detail = fn(job) or ""
                ok = True
            except Exception as exc:
                detail, ok = f"{type(exc).__name__}: {exc}", False
            elapsed = clock() - t0
            state.outcomes.append(Outcome(job.key, name, ok, elapsed, str(detail)))
            # Recorded whether it passed or failed: re-running a regiment that already
            # produced a verdict wastes the cluster, and "it failed" is a verdict.
            state.done.add((job.key, name))
            emit(f"  {name}: {'ok' if ok else 'FAILED'} ({elapsed / 60:.1f} min) {detail}"[:300])
            if breadcrumbs:
                save_state(state, breadcrumbs)

    return state


def summary(state: Breadcrumbs) -> str:
    ok = sum(1 for o in state.outcomes if o.ok)
    bad = [o for o in state.outcomes if not o.ok]
    lines = [f"{ok} passed, {len(bad)} failed, "
             f"{len(state.quarantined)} profile(s) quarantined"]
    for o in bad:
        lines.append(f"  FAILED {o.job} / {o.regiment}: {o.detail[:120]}")
    for prof, why in state.quarantined.items():
        lines.append(f"  QUARANTINED {prof}: {why[:120]}")
    return "\n".join(lines)


def progress(state: Breadcrumbs, jobs: list[Job] | None = None) -> str:
    """What a detached run has got through — readable without attaching to anything.

    Reads the same breadcrumbs the runner writes, so it is accurate even if the process
    that wrote them is gone. That is the point: "is it still going, and how far?" must be
    answerable from a fresh shell after a dropped connection.
    """
    lines = []
    if jobs:
        total = sum(len(j.regiments) for j in jobs)
        lines.append(f"{len(state.done)}/{total} regiments complete")
    else:
        lines.append(f"{len(state.done)} regiments complete")
    for outcome in state.outcomes[-8:]:
        mark = "ok  " if outcome.ok else "FAIL"
        lines.append(f"  {mark} {outcome.job} / {outcome.regiment} "
                     f"({outcome.seconds / 60:.1f} min) {outcome.detail[:80]}")
    for prof, why in state.quarantined.items():
        lines.append(f"  QUARANTINED {prof}: {why[:80]}")
    return "\n".join(lines)


def holder(lock_path: Path = DEFAULT_LOCK) -> str | None:
    """Who holds the cluster, if anyone. `None` means no suite is running."""
    try:
        return lock_path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
