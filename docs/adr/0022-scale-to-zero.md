# ADR-0022: The fleet puts itself away — scale to zero, and the front door that makes it possible

**Date:** 2026-08-13
**Status:** Accepted

## Context

A loaded model costs real resources while answering nothing. Measured 2026-08-12 on an
idle fleet with `qwen3.6-35b-a3b-nvfp4` resident and zero requests in flight:

| | idle, model loaded | `empty` |
|---|---|---|
| GPU power | 11.8 W / 12.9 W | 4.0 W / 5.3 W |
| SM clock | **2401 MHz / 2412 MHz** | 208 MHz |
| GPU utilisation | 0.5% | — |
| host RAM | 115 / 121 GiB | 5 / 121 GiB |

**~15.5 W fleet-wide and ~97 GiB per node, for nothing.** The RAM is the larger cost and
the more surprising one: `gpu_memory_utilization: 0.80` is claimed at load time and held
for the engine's lifetime, so a 10.31 GiB model occupies 96.8 GiB. It is not the model
that is fat — vLLM fills whatever it was told it could have with KV cache, and at a single
user's concurrency almost none of that cache is ever used.

That cost is only worth paying while someone is using the cluster. This box is shared with
other work — a CPU build that wants the RAM, an operator who wants the box quiet overnight
— so "the cluster is on" should not mean "the cluster is consumed".

### What made this awkward

**ADR-0018 assumes a human or an agent decides what serves.** Provisioning and selection
were split precisely so that nothing facing the network could change the fleet, and the
activation grant was bounded to one fixed, input-validating program. An automatic unload is
the first thing in this system that changes what serves **with nobody asking**, so it needs
an argument that ADR-0018's does not already cover.

**And unloading is only half of it.** Once the fleet is at zero, the next request finds
nothing. Cold start to *serving* was measured at **295.8 s** for the fastest model in the
fleet (`qwen3-vl-235b`'s weight load alone is 494 s), so "the model will be along shortly"
is not a rounding error — it is five to ten minutes during which a caller must be told
something true.

## Decision

**Four parts, and the first one carries the safety argument for all of them.**

### 1. The autonomous action can only ever move toward the fail-safe state

A systemd timer running as the `activate` identity unloads the fleet after a long quiet
period. **Its single possible action is `activate empty`** — the fail-safe target, which is
where the cluster already goes on any failure (ADR-0009, ADR-0018), and which is always
activatable by construction.

It cannot choose a model, cannot start one, and cannot be steered by a request. That is
what makes an unattended actor acceptable here: *the only direction it can move the system
is the direction failure already moves it.* Anything an attacker or a bug could achieve
through it, a crashed engine achieves anyway.

It holds **no privilege of its own** — it writes the request file and calls the same
bounded reconciler `./sparky.sh activate` calls, through the sudoers entry the `activate`
group already has. It is not a fourth bounded program and needs no new grant.

It refuses to act when the fleet lock is held (a deploy is reshaping the boundary; a
campaign is measuring the very model it would remove), when requests are in flight, when
the token counter moved since the last check, and **when the engines are unreachable** —
*unreachable is not idle*, and treating a network blip as silence would evict a model
somebody is using.

### 2. A request that arrives at a sleeping fleet is HELD, not refused

Caddy holds inference requests while no upstream is healthy (`lb_try_duration`), so the
caller sees its ordinary waiting UI and then receives **the model's real answer**. Verified
end to end 2026-08-13: a request sent to an empty fleet was held **295.8 s** and returned
HTTP 200 with a genuine completion.

This is independently valuable — it makes *any* activation invisible to a waiting client,
including a manual one — and it is why the operator-facing story is "it is slow the first
time", not "it errors the first time".

### 3. Only inference is held; the control plane fails fast

`/health`, `/metrics` and `/v1/models` answer immediately. Machine callers — Prometheus
scraping the endpoint, the panel probing health, `is_ready()` polling — want the truth now,
and holding them turns "the engine is down" into a timeout. Measured 2026-08-13: with
holding applied to every path, `sparky status` went from an instant answer to 4.3 s, and
Prometheus' scrape pool would have been permanently saturated.

**The endpoint also advertises its stable alias even when nothing is loaded.** Open WebUI
resolves a model *before* it can compose a request: it GETs `/v1/models`, picks an id, and
only then posts a completion. With the fleet at zero that GET failed, the picker was empty,
and it sent `model: ""`. So the proxy synthesises the list. This is **not** the same as
fabricating a completion: a model list states what the endpoint *offers*, and it genuinely
offers `sparky` — it is asleep, not absent, which is precisely what scale-to-zero means.

### 4. Model-bound traffic gets its own front door

The model endpoint listens on its own address, separate from the web UI, dashboards and
panel that share `:80`.

This is the structural half of the decision, and it exists because **observability requires
it**. Caddy labels its HTTP metrics by *server*, and a server is a set of listen addresses
— so sites sharing `:80` share one counter. `caddy_http_requests_in_flight` carries
`{handler, server}` and no host or path dimension, which means a shared front door cannot
answer "is anyone waiting for a model?": measured 2026-08-13 with nothing waiting at all,
it read **3** — Open WebUI's long-lived websockets, a scrape, and the request doing the
reading.

With its own listener the question becomes exact **by construction** rather than by
filtering. Precisely: **only the held inference paths are routed inward.** The control
plane (`/health`, `/metrics`, `/v1/models`) is answered by the outer vhost and never
reaches the model listener, so it cannot contaminate the count — otherwise a scrape in
flight would read as a caller waiting, and "exact" would be a slogan rather than a
property. Anything in flight on that listener is a held inference request. No thresholds,
no heuristics, no label archaeology.

## Options considered and rejected

**Return a canned 200 explaining the wait.** Rejected: that is the data plane lying. A
programmatic client cannot distinguish it from the model's own output, so an agent would
act on it, a benchmark would score it, and it would be logged as a completion. Better a
slow truth than a fast fiction. (The synthesised *model list* in part 3 is not this — a
list is a statement about what is offered, and nothing downstream treats it as content.)

**An unauthenticated `/wake` endpoint.** Rejected on the principle, and the rejection is
the reason part 4 exists at all: no request may cause an activation through a route that
exists to be invoked. Waking by *observing demand* through a listener whose traffic is
model-bound by construction is a different thing — there is nothing to call.

**Freezing the engine process (SIGSTOP / cgroup freezer).** Rejected, and it would not even
work: the GPU clock is held by a resident CUDA context, not by CPU activity — median CPU is
1.9%. Beyond that, Caddy marks a frozen upstream dead in ~7 s, so the request that would
thaw it gets a 502; and under TP=2 a frozen peer is indistinguishable from a hung one, so
NCCL tears the communicator down.

**`nvidia-smi --drain`.** Rejected: draining requires no processes on the GPU, so it cannot
be done while a model is resident. To drain you must stop vLLM — which is unloading, with
extra steps.

**Heuristics on the shared in-flight counter** (thresholds, baselines, sustained-across-two-
checks). Rejected: every variant is a guess about which of several unrelated workloads is
responsible for a number, and it fails in the direction of waking the cluster for nobody.
The separate listener makes the guess unnecessary.

**Right-sizing `gpu_memory_utilization` instead.** Considered and dropped as the wrong lever
for this problem: it was costed (a fleet-wide concurrency budget with per-profile derived
`gmu`) and it would reduce the RAM cost but not the power, would not help when the cluster
is genuinely unused, and trades directly against the prefix cache. Scale-to-zero recovers
*all* of the cost when nobody is there, which is the case that matters.

## Consequences

**The first request after a quiet period is slow** — five to ten minutes depending on the
model. Held rather than refused, but slow. The idle threshold is therefore measured in
hours, not minutes: it exists to survive a lunch, an afternoon of meetings and a weekend,
not to fire between chat turns.

**Off by default.** A deploy must never start unloading a fleet by surprise; enabling it is
a deliberate edit. `idle_unload_after: 0` disables it too — read literally a zero threshold
would fire on the first check, which is the most destructive possible reading of the most
natural way to ask for the least.

**A third autonomous actor joins the synchronization model** ([`synchronization.md`](../synchronization.md)),
and it is the only one that acts unattended. Its guards are part of the decision, not an
implementation detail.

**The model endpoint gains a hop** — the `:80` vhost proxies to the model listener. On
loopback that is microseconds, and it buys the exactness in part 4.

**All four parts were verified live on 2026-08-13 before this was Accepted.** A request
sent to an empty fleet was held while the manager restored the model from its own marker,
and returned HTTP 200 with a real completion after 307.4 s — no human involved. The signal
read `srv0 = 1` (the caller) against `srv1 = 4` (browser tabs on `:80`), which is the whole
argument for part 4 in one line of output: on the shared counter those were
indistinguishable, and the manager woke for phantom demand every minute.

Two measurements bracket the cold start at **~296–307 s for the FASTEST model**, which is
why `lb_try_duration` is 600 s and not the 300 s first chosen from weight-load time — the
307 s run would have failed by seven seconds under the original value.

**The hold only helps a patient client.** If a caller's own timeout is shorter than the
cold start it sees an error regardless, and the courtesy is wasted. Open WebUI was measured
holding for the full ~5 minutes on 2026-08-13, but that is evidence about one client, not a
property of the design. Anything with a 30-second timeout gets the same experience it would
have had without any of this.

**With implicit wake, "only toward the fail-safe state" stops being literally true.** An
unauthenticated request can then cause the cluster to spend several minutes of GPU time
loading a model. It still cannot choose *which* — the profile comes from a marker the
system wrote when it unloaded — so the invariant weakens from "can only move toward safety"
to "can only restore what was already chosen". That is a real reduction and is accepted
deliberately: on a LAN-only endpoint the cost of the abuse case is a cold start, and the
alternative is a callable route, which is worse.

**A failed restore leaves the caller waiting.** If the marker names something no longer
allowlisted — a profile deleted between unload and wake — the reconciler refuses and falls
to `empty`, and the held request waits out its window and fails. Correct, but silent; the
refusal is visible only in the journal.

**Several held requests do not cause several activations.** The reconciler holds a
per-node lock and refuses to interleave, so concurrent wake attempts serialise into one
activation and the rest are no-ops.
