"""The `soak` regiment (ADR-0016) — sustained concurrency, watching for a stall.

**DEF-0002 wrote this spec, including the part that is easy to get wrong.** Its
*clears-when* says, in as many words: *"soak-test TP=2 for hours **under concurrency** —
duration alone is not the gate: a 90-min light-load soak passed 2026-08-06 and proved
nothing."* That 90 minutes was spent, and bought nothing, because a deadlock in the
scheduler needs contention to reach. So this regiment holds a fixed number of requests
**in flight** for the whole window rather than trickling them.

What it is looking for is not a bad answer — that is `quality`'s job — but the engine
going **quiet**: throughput collapsing to zero while requests remain outstanding, which is
the shape of the DEF-0002 deadlock class and of the 35–55-minute stalls ADR-0016 names.
A crash would be caught by anything; a hang is invisible to everything that only checks
at the end.

**It reports progress as it goes.** A regiment that says nothing for 45 minutes is
indistinguishable from a hung one — which was the exact confusion during the first suites,
where the only way to tell a working soak from a dead script was to go and look at the
GPU.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# Long enough to reach the window where the DEF-0002 class shows up (35-55 min in), and
# the default is deliberately shorter than that: a soak you must opt into for the full
# duration is one that gets run, where a 45-minute default is one that gets skipped.
DEFAULT_MINUTES = 10
DEFAULT_CONCURRENCY = 8
PROMPT = ("Explain, step by step, how a write-ahead log guarantees durability across a "
          "crash. Be specific about ordering.")
MAX_TOKENS = 256
# If nothing completes for this long while requests are outstanding, the engine is not
# slow — it is stuck. Generous: a big model under concurrency can legitimately take a
# couple of minutes per response.
STALL_SECONDS = 300.0


@dataclass
class SoakResult:
    minutes: float = 0.0
    completed: int = 0
    failed: int = 0
    stalled: bool = False
    longest_gap: float = 0.0
    tokens: int = 0
    samples: list[tuple[float, int]] = field(default_factory=list)   # (minute, completed)

    @property
    def ok(self) -> bool:
        """A soak passes by NOT stalling and NOT erroring. Throughput is bench's
        question; this one is about whether the engine is still answering at minute 45."""
        return not self.stalled and self.failed == 0 and self.completed > 0

    def summary(self) -> str:
        rate = self.completed / self.minutes if self.minutes else 0.0
        return (f"{self.minutes:.0f} min · {self.completed} completed "
                f"({rate:.1f}/min) · {self.failed} failed · "
                f"longest gap {self.longest_gap:.0f}s"
                + (" · STALLED" if self.stalled else ""))


def run(client, model: str, *, minutes: float = DEFAULT_MINUTES,
        concurrency: int = DEFAULT_CONCURRENCY, on_progress=None,
        stall_seconds: float = STALL_SECONDS, clock=time.monotonic,
        sleep=time.sleep) -> SoakResult:
    """Hold `concurrency` requests in flight for `minutes`, watching for a stall."""
    result = SoakResult()
    started = clock()
    deadline = started + minutes * 60
    last_completion = started
    next_report = started + 60

    def one() -> int:
        reply = client.chat([{"role": "user", "content": PROMPT}], model=model,
                            max_tokens=MAX_TOKENS, temperature=0.0)
        return len((reply.content or reply.reasoning_content or "").split())

    # NOT a `with` block. Its exit calls shutdown(wait=True), which blocks on every
    # outstanding request — including the one that is stuck. A regiment built to DETECT a
    # hang would itself hang on detecting one, holding the whole suite, which is the worst
    # possible place for this bug to live. Found by a test that took 30 s instead of
    # milliseconds (2026-08-10).
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        pending = {pool.submit(one) for _ in range(concurrency)}
        while clock() < deadline:
            done = {f for f in pending if f.done()}
            for f in done:
                pending.discard(f)
                try:
                    result.tokens += f.result()
                    result.completed += 1
                except Exception:
                    result.failed += 1
                last_completion = clock()
                # Refill immediately: the point is SUSTAINED pressure. Draining and
                # re-filling in waves would leave idle gaps, which is how the 2026-08-06
                # light-load soak managed to prove nothing.
                if clock() < deadline:
                    pending.add(pool.submit(one))

            now = clock()
            gap = now - last_completion
            result.longest_gap = max(result.longest_gap, gap)
            if gap > stall_seconds and pending:
                result.stalled = True
                break
            if now >= next_report:
                elapsed = (now - started) / 60
                result.samples.append((round(elapsed, 1), result.completed))
                if on_progress:
                    on_progress(f"{elapsed:.0f} min · {result.completed} done · "
                                f"{result.failed} failed · gap {gap:.0f}s")
                next_report = now + 60
            sleep(0.25)
        result.minutes = (clock() - started) / 60
    finally:
        # Queued futures are cancelled; ones already in flight cannot be, so they are
        # abandoned. That is safe because the client carries a timeout — a genuinely stuck
        # request errors out on its own and its thread exits. Waiting for it here would
        # trade a reported stall for a silent one.
        pool.shutdown(wait=False, cancel_futures=True)
    return result
