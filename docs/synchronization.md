# Cluster synchronization — who may touch the fleet, and when

Three kinds of actor change this cluster, and they must not interleave:

| actor | what it does | how long | privilege |
|---|---|---|---|
| **deploy** | reshapes the boundary — re-renders engine files, pulls images, evicts weights | minutes | root, password-gated |
| **activate** | changes which allowlisted profile serves | seconds to ~10 min (weight load) | none |
| **a suite run** | walks the boundary — `suite`, a suite run, `bench`, `eval` | up to a night | none |
| **idle unloader** | drops the fleet to `empty` after a long quiet period | seconds | none |

The hazard is not concurrency in the abstract. It is that **one actor reshapes what
another is standing on**: a deploy that re-renders an engine file mid-measurement produces
numbers belonging to no configuration that ever existed, and an activation fired into a
deploy collides with `fleet-state`, the deploy's last role, which converges the selection.

## The locks

Four, deliberately, because they guard different things at different scopes.

| lock | held by | scope | mechanism |
|---|---|---|---|
| `/opt/cluster/fleet.lock` | **deploy** and **a suite run** | fleet-wide | `flock(1)` from the shell (`ansible.py`), `fcntl.flock` from Python (`runner.py`) |
| `/run/vllm-activate.lock` | **the reconciler** | **per node** | `fcntl.flock`, non-blocking |
| `/opt/cluster/benchmark/runner.lock` | **a suite run** | one run at a time | `fcntl.flock`, `stale_after` 6 h |
| `/opt/cluster/desired-profile` | — | the request itself | not a lock; a group-writable file |

`fleet.lock` is the important one, and it is shared between a **shell** and a **Python
process** — which is exactly why it is an advisory `flock` and not a marker file. That is
the only mechanism both can speak.

> **This was a claim before it was a fact.** Until 2026-08-11 `deploy` took `fleet.lock`
> and the run took `runner.lock` — *different files* — so nothing was excluded and a
> deploy could evict weights mid-measurement. `tests/test_ansible.py` now pins the two
> constants together.

## The rule: wait or refuse, decided by the holder's duration

Both are correct; picking the wrong one produces something that looks broken.

- **Refuse, with an explanation, when the holder may run for hours.** A deploy declined
  during a run says so and exits. Blocking would leave it silent for most of a night
  and the operator would reasonably conclude it had hung. `ansible.py` makes this call.
- **Wait, visibly, when the holder is bounded and short.** A deploy is minutes. An
  activation that simply waits for it costs nothing and removes a whole class of
  collision.

*Duration is the discriminator — not politeness, and not which actor is "more important".*

## Detecting a deploy: the lock, never the process name

**Ask the lock.** `flock -n /opt/cluster/fleet.lock -c true` succeeds iff no deploy holds
it.

Do **not** scan for a process. On 2026-08-12 a guard used
`pgrep -af "ansible-playbook"` and reported a deploy in flight when there was none: the
search string appeared in the command line of the shell running the search, so it matched
itself. A process-name check is also wrong in the other direction — it cannot see a
run holding the lock from Python, because no `ansible-playbook` is running at all.

## What is guarded today, and what is not

| direction | guarded? | by what |
|---|---|---|
| deploy → during a run | ✅ | `fleetlock.held()`, refuses with a message |
| a run → during a deploy | ✅ | `fleetlock.hold()`, raises `SuiteBusy` |
| activation → during an activation | ✅ | the reconciler's per-node lock: *"another activation is in flight"* |
| deploy → during an activation | ⚠️ partial | `fleet-state` fails at the **last role**; nothing is corrupted, but the deploy dies one task from the end |
| activation → during a deploy | ✅ | `activate.wait_for_deploy()`, called by `bring_up()` — **waits** rather than refusing |

All four directions are guarded as of 2026-08-12. The last row was the gap that closed it.

> **2026-08-12.** An activation was started while a deploy ran. `fleet-state` converged the
> selection underneath it; the engine came up on one node, went `deactivating` on the
> other, and the fleet fell to `empty`. Nothing was corrupted — the mutual exclusion that
> *does* exist contained it — but the model was down and the activation exited 143.

**The fix:** `bring_up()` calls `wait_for_deploy()` before requesting anything. It polls
`fleet.lock`, reports that it is waiting and why, and raises `NotLive` after 30 minutes
rather than stalling silently on a wedged deploy.

⚠️ **It must not simply take the lock, and this is the subtle part.** A run already
holds `fleet.lock` for its whole run (`fleetlock.hold`) and then activates once per
job — `runner.run()`'s `activate` callback is `cli.activate_profile`, which calls
`bring_up()`. flock is per **open file description**, so a second acquire from the *same
process* blocks against itself: an unconditional acquire would deadlock every run, and an
unconditional *wait* would hang one just as dead. So the wait returns immediately when
`fleetlock.held_by_us()` — i.e. when we are the holder.

That skip is the whole reason the obvious one-line fix is wrong, so it has its own test
(`test_wait_for_deploy_skips_when_this_process_holds_the_lock`).

## The idle unloader (scale to zero)

`vllm-idle`, a systemd timer on the head node, unloads the fleet after `idle_unload_after`
of no traffic. It is the only actor here that acts **unattended**, so it is bounded twice:

- **Its single possible action is `activate empty`** — the fail-safe target, where the
  cluster already goes on any failure. It cannot choose a model, cannot start one, and
  cannot be steered by a request, so the only direction it can move the system is toward
  the safe state. A test asserts `unload()` mentions no profile at all.
- **It holds no privilege of its own.** It runs as the `activate` identity and calls the
  same bounded reconciler `./sparky.sh activate` calls, through the sudoers entry that
  group already has. It is not a fourth bounded program.

It refuses to unload when the fleet lock is held (a deploy is reshaping the boundary, or a
run is measuring the very model it would remove), when requests are in flight, when
the token counter moved since the last check, and — the one worth stating plainly — **when
the engines are unreachable.** *Unreachable is not idle*: treating a network blip as
silence would evict a model somebody is using.

**Waking is implicit, but there is nothing to call.** Caddy HOLDS an inference request
arriving while nothing serves (`lb_try_duration`), so the client shows its ordinary wait UI
rather than an error; the manager sees that held request as demand and restores what it
unloaded. **Restore, never select** — the profile comes from a marker the manager itself
wrote, so a request can resume what the cluster was already doing and can never choose a
model. There is still no endpoint: ADR-0018's "no web-API path to root" survives because
nothing is invocable.

That only works because **model-bound traffic has its own listener** (ADR-0022 part 4).
Caddy labels metrics by *server* = a set of listen addresses, so everything on `:80` shares
one counter with no host or path dimension — it read 3 with nothing waiting. Only the
control plane stays on `:80` and fails fast, because Prometheus scrapes the same endpoint
and a scraper wants the truth now, not a courteous wait.

Off by default (`idle_unload_enabled`). A deploy must never start unloading a fleet by
surprise.

## Rules of thumb

- **A deploy and an activation must never overlap.** If you drive both, sequence them.
- **A deploy is selection-neutral**, so it is safe to run while a model *serves* — it
  installs changes and reports them pending. The hazard is only during the *activation
  itself*.
- **`deployed_at` in `/opt/cluster/fleet.json` is the only completion signal.** It is
  stamped by `fleet-state`, which runs last. A model directory or an engine env file
  appearing proves nothing: those roles run 4th and 6th of 15.
- **Reading is never privileged and never needs a lock** — `status`, `fleet`, the panel and
  the reconciler's `--status` verb are all safe at any moment.

## See also

- [ADR-0018](adr/0018-provision-select-split.md) — why provisioning and selection are separate privileges
- [ADR-0021](adr/0021-suite-runs-from-the-panel.md) — why a run is a systemd unit with its own lifetime
- [`skills/development/SKILL.md`](../skills/development/SKILL.md) — the operator handover: who deploys, and how to know it finished
