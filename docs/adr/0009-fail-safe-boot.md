# ADR-0009: Fail-safe boot (sentinel-guarded auto-start)

**Date:** 2026-07-02
**Status:** Accepted

## Context

Deploying a profile leaves each vLLM unit `enabled`
(`WantedBy=multi-user.target`), so every boot unconditionally re-attempts that
profile's serving load. For a big-shared TP=2 profile that means a ~121 GiB
checkpoint load plus a two-node NCCL rendezvous, unattended, at boot.

On 2026-07-02 a container bump to `26.06-py3` (vLLM 0.22.1, CUDA 13.3,
forward-compat driver 610.43.02 over the 580 host kernel driver) **hung both
nodes hard** during the multi-node bring-up — SSH-unresponsive, requiring a
hard reset of both machines. Because the units are enabled, each reboot
immediately re-entered the same load and re-hung the node: a modal,
self-perpetuating failure with no operator entry point. This is the same class
as the documented "Qwen3.5-122B froze sparky during load" lockup.

The boot path needs to be **fail-safe**: a startup that previously wedged the
host must not silently re-run on the next boot.

## Options considered

**A. `StartLimitIntervalSec` / `StartLimitBurst` (+ `StartLimitAction`)**
systemd's native crash-loop breaker: stop retrying after N failed starts in a
window. Idiomatic — but it only counts *failed* starts. A process that never
exits (wedges the kernel) is never counted, so it does nothing for the actual
failure mode here. Useful as a complementary layer, not a solution.

**B. Service watchdog (`WatchdogSec` / `sd_notify`)**
Detects a hung *service* and restarts it. Doesn't apply: vLLM-in-docker isn't
wired for `sd_notify`, and more fundamentally a kernel-level hang is beyond any
userspace watchdog — the thing that would act is also hung.

**C. Pure empty-boot (don't enable the units)**
Units never auto-start; serving is only ever brought up by an explicit deploy.
Simplest and safest — boot can never hang the host. Cost: no auto-restore after
a legitimate unattended power event; every reboot needs an operator deploy.

**D. Sentinel-guarded auto-start (chosen)**
Units stay enabled, but a persistent per-engine marker gates auto-start via
`ConditionPathExists`. A clean reboot auto-restores serving; an unclean
shutdown (hang / hard-reset / power cut) leaves the marker behind, so the next
boot skips the unit and the node comes up empty and reachable.

## Decision

Option D, with option A layered in as defense-in-depth.

The distinguishing question a boot must answer is "did the previous run shut
down cleanly?" — and the only lever for a kernel hang is at boot, not runtime.
`ConditionPathExists` on a persistent marker is systemd's idiomatic primitive
for exactly that (the same shape first-boot provisioning uses).

## Mechanism

Per-engine marker at `{{ vllm_state_dir }}/vllm-<engine>.running`
(`/opt/vllm/state/…`, on persistent disk — **not** `/run` or `/tmp`, which are
cleared on boot). On the engine unit:

```ini
[Unit]
ConditionPathExists=!/opt/vllm/state/vllm-<engine>.running
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
ExecStartPre=/usr/bin/touch /opt/vllm/state/vllm-<engine>.running   # arm
ExecStopPost=/usr/bin/rm -f /opt/vllm/state/vllm-<engine>.running   # disarm on clean stop
```

The lifecycle is what makes it work:

- **`ExecStopPost` runs on every stop systemd controls** — a `systemctl stop`, a
  deploy/teardown, a crash-exit, or a clean `reboot`. It does **not** run on a
  hard reset / kernel hang / power cut. So the marker survives to the next boot
  **only** after an unclean shutdown.
- **Clean reboot** → marker was removed at shutdown → `ConditionPathExists`
  passes → serving auto-restores.
- **Hang / hard-reset** → marker persists → condition fails → unit is *skipped*
  (cleanly, not failed; `Restart=` does not fire) → node boots empty and
  reachable.
- **`systemctl restart`** works normally: the stop phase's `ExecStopPost` clears
  the marker before the start phase re-checks the condition.
- The **only** blocked start is a bare start of a *stopped* unit whose marker
  survived — exactly the guard we want. A recovery therefore requires clearing
  the marker, which an operator-initiated **deploy does explicitly** (a task in
  `roles/vllm/tasks/engine.yml` removes the marker before the `started` step);
  otherwise that step would silently no-op.

## Consequences

- Boot can no longer silently re-run a load that hung the host. The failure that
  required two hard resets on 2026-07-02 would instead leave both nodes empty
  and reachable after the first reboot.
- The common case is preserved: a clean reboot (power blip with a clean
  shutdown, planned reboot) auto-restores serving with no operator action.
- A deploy is the "operator is present, proceed" signal — it always clears the
  marker first, so recovery after a hang is just `make deploy`. The control
  panel's per-engine restart also works (restart self-clears via `ExecStopPost`).
- Trade-off: after an unclean shutdown, a *bare* `systemctl start` of the stopped
  unit is skipped until the marker is cleared. This is intentional; the recovery
  path is a deploy (or `rm` the marker). Documented in the unit file and README.
- The marker is per-engine and per-node, so a multi-engine or partially-deployed
  host fails safe independently per engine.
- `StartLimitBurst=5` / `StartLimitIntervalSec=900` stops a pure crash-loop
  (unit exits repeatedly without hanging the host) from restarting forever; it
  leaves the unit failed-but-reachable. This does not overlap with the marker —
  it covers the case the marker cannot (clean exits) and vice versa.

---

## Verification — 2026-08-08

Added after acceptance. This is the *result* of the test this ADR's own plan called for,
not a revision of the decision; it lived in the README only because that is where it got
written down first.

**Both gates, tested independently, without a hard reset.** A clean reboot of snoopy left
five of six enabled instances skipped for want of a *desired* marker, and started the sixth
on its own — no reconciler involved, which is the property that matters: the markers carry
the decision, and the script need not even exist for recovery to work. A planted `.running`
marker then made that same engine skip on the *negated* gate, leaving the node up,
reachable and serving nothing. systemd names the failing condition in the journal either
way, so the reason is visible without instrumentation.

**Then the negated gate fired for real, the same day**, when a bad model exhausted host
memory during weight load and froze the machine. The `.running` marker survived the power
cycle, so on boot systemd refused to re-attempt the load that had just killed it, and the
node came back empty and reachable in four minutes. The incident, its error signature and
its pre-flight check are catalogued in [`docs/bring-up-failures.md`](../bring-up-failures.md)
and tracked as DEF-0004 in [`docs/defects.md`](../defects.md).

The synthetic test showed the gates work. The real firing showed the design was aimed at
the right hazard — which is the part a test cannot demonstrate.
