# ADR-0023: Wake latency — poll fast, because a demand *trigger* cannot coexist with holding

**Date:** 2026-08-13
**Status:** Accepted

## Context

[ADR-0022](0022-scale-to-zero.md) shipped scale-to-zero: the fleet unloads when idle, a
request arriving at a sleeping fleet is **held** rather than refused, and an idle manager
notices the held request and restores what it unloaded. It works — measured end to end, a
request was held and answered in 307.4 s with no human involved.

It left one number chosen by default rather than on purpose: the manager polled **once a
minute**, so a caller could wait up to 60 s before anything even looked, on top of a ~300 s
cold start. Roughly 20% of the wait, spent doing nothing.

Polling was never the goal. It is what ADR-0022's security choice leaves behind: *nothing is
callable*, so the manager can only discover demand by looking. The question this ADR answers
is whether that is actually forced, or whether a trigger exists that does not reintroduce a
callable surface.

## Decision

**Poll every 5 seconds. There is no viable trigger.**

`idle_check_seconds: 5`, pegged to `health_interval 5s` already in the Caddyfile — the
manager looks exactly as often as Caddy re-evaluates upstream health, so the interval is
justified by a constant already in the design rather than by taste. A tick costs ~20 ms
measured, so the cost is journal volume, not CPU.

Three things make it work, and two of them are non-obvious enough to be part of the decision
rather than implementation detail:

**`AccuracySec=1s` is load-bearing.** systemd's default is **one minute** — it batches timer
wake-ups to save power. Verified on this host before the change: `AccuracyUSec=1min`. Without
pinning it, `OnUnitActiveSec=5s` produces a unit file that *says* 5 s and fires whenever it
likes within the minute. It applies to `OnActiveSec` and `OnBootSec` too, so the first tick
after a deploy is affected as much as the steady state. The failure is silent, which is why a
test pins it.

**The journal cost is mostly systemd's, not the program's.** PID 1 logs `Starting`/`Finished`
per oneshot activation — 2 lines per tick, 34,560/day at 5 s — and no program-side rate limit
can touch them. `LogLevelMax=notice` filters them in the manager before journald.
⚠️ **`SyslogLevel=notice` is required alongside it**: the unit's stdout and stderr are logged
at `info`, so `LogLevelMax` alone would silently swallow every `print()` *and every
traceback* — a dead-man's switch that dies without saying so, which is the exact failure
[ADR-0009](0009-fail-safe-boot.md) exists to prevent. Both, or neither.

**Observations are sampled; actions and failures are not.** A `narrate()` helper thins
routine per-tick lines to one a minute. It is stateless (`time.time() % 60 < CHECK_EVERY`)
because this is a oneshot with no memory between runs, and it self-disables at an interval of
60 s — so reverting the cadence reverts the thinning, with one number to move. Measured after
deployment: 120 ticks produced 45 journal lines, against ~360 unthinned.

## The rejected alternative, and why it cannot be fixed

**A last-resort upstream as a demand signal.** Caddy's `lb_policy first` selects the first
*healthy* upstream. List the engines first and a loopback "waker" last, and Caddy dials the
waker only when no engine is healthy **and** a request actually exists — demand-gated by
construction, loopback-bound, silent on an idle cluster, and with nothing callable from
off-box. It was the most promising idea available and it was prototyped rather than argued
about.

**The trigger half worked, and beautifully: Caddy dialled the waker 16 ms after the request
arrived** (21:05:47.589 → 21:05:47.605), against up to 60 s for the poll.

**The hold half died at the same instant.** The client received **HTTP 502 after 3 ms**.

`lb_try_duration` — the directive that makes Caddy *wait* for an engine instead of failing —
applies only while **no** upstream is available. A waker healthy enough to be dialled **is**
an available upstream. So Caddy stopped waiting, dialled the waker, got a closed connection,
and returned 502. The poll-based wake died with it: the request never stayed in flight, so
`caddy_http_requests_in_flight` read 0 and the manager saw no demand at all.

> **Holding requires no healthy upstream. Triggering requires one.** The two properties are
> mutually exclusive by construction, and no tuning reconciles them.

Anything that resolves it must **hold the connection itself and proxy onward** — a bespoke,
HTTP-aware proxy in the serving path. ADR-0022 rejected that, and nothing here changes the
reasons: it would re-implement what Caddy already does well (holding, health checks,
timeouts), and its failure mode is not "slow wake" but "no serving at all".

**Also rejected, for the record:**

- **systemd socket activation** (`systemd-socket-proxyd`, which is installed). Triggers on
  TCP *connect*, so a port scan or a monitoring probe would start a model — *more* callable
  than the metrics counter ADR-0022 settled on, not less. It would also take `:8090` from
  Caddy, moving the hold behind a byte-copying proxy on the hot path for **all** traffic
  forever, to optimise a rare cold one.
- **A `.path` unit on a Caddy access log.** Caddy writes an access log on request
  *completion*, so a held request is invisible exactly while it matters. It would also mean
  adding an access log purely as an IPC channel, firing a unit activation on every request
  while serving — worse than polling.
- **A Caddy `exec`/events handler.** Not in the standard build, so a custom Caddy image and a
  new supply chain for a five-second saving.

## Consequences

**Wake latency is now bounded by the tick, and the tick is verified.** ≤5 s plus the accuracy
window, against ≤60 s before. On a ~300 s cold start that is ~2%, and it is the floor: ~0 s is
not reachable without putting a proxy in the serving path.

**Nobody should keep shaving this number.** Total wake time is
`idle_check_seconds` + accuracy window + the reconciler's return + the cold start. At 5 s the
tick is already the smallest term by two orders of magnitude.

**A failed restore now retries 12× as often** — ~120 attempts across a 600 s hold rather than
~10. Deliberately unchanged: a latency change should not smuggle in a policy change, a
transient failure still self-heals, and the realistic case (a marker naming a profile that
left the allowlist) is refused by `vllm-activate`'s allowlist check before it touches systemd.
Pinned by a test so it stays a choice.

**The prototype's wiring is gone but its result is here.** The Caddyfile carries a short
pointer at the exact line where someone would try it again; the measurements and the argument
live in this ADR rather than in a comment block, so the code stays readable and the reasoning
stays findable.

## References

- [ADR-0022](0022-scale-to-zero.md) — scale-to-zero; this refines its polling decision without
  superseding it
- [ADR-0018](0018-provision-select-split.md) — why nothing callable may cause an activation
- [ADR-0009](0009-fail-safe-boot.md) — why a component that fails silently is the worst failure
- `ansible/roles/caddy/templates/Caddyfile.j2` — the `:8090` model listener, where the waker
  would go
- `ansible/roles/activate/templates/vllm-idle.timer.j2` — the interval and `AccuracySec`
- `docs/synchronization.md` — the idle manager as the third autonomous actor
