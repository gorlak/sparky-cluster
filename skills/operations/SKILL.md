---
name: operations
description: Drive and verify the cluster the no-sudo way — activate a model, read live status, confirm it worked. Read when asked "is the cluster up / healthy", to switch which model is serving, to confirm a deploy or activation worked, or to check which profile is live. Covers `sparky activate`, `sparky status`, the control panel JSON surfaces, and the gate breadcrumb.
---

## The shape: you get `activate`, Geoff gets `deploy`

Two operations, two privilege levels (ADR-0018):

| | `deploy` | `activate` |
|---|---|---|
| Does | converge the whole fleet to the allowlist | make an already-deployed model the live one |
| Changes what's serving | **no** | **yes** |
| Privilege | root, **password-gated — Geoff only** | **none** |

**You can `activate`. You cannot `deploy`.** If a task needs a model that isn't
installed, a flag changed, or a container bumped, that is a deploy — surface it to
Geoff (see the 🔴 YOUR MOVE convention) rather than trying to work around it. There is
no path from here to root, by design; attempts to find one are wasted effort.

## The one rule: read status, don't shell sudo

`./sparky.sh status` reads the **control panel** (`127.0.0.1:8088`), which gathers
every node over its own bounded channel — so it needs **no password**, and it has **no
fallback that does**. Do NOT reach for `sudo -u deploy ansible … systemctl`: it prompts
for Geoff's password and hangs you.

If the panel is unreachable (exit 2), these work directly, still with no sudo — reading
status was never privileged:

```bash
/usr/local/sbin/vllm-activate --status
```

```bash
ssh snoopy /usr/local/sbin/vllm-activate --status
```

A panel that is genuinely down is a fault to report, not to work around; `./sparky.sh
deploy` repairs it.

## Switch which model is serving

```bash
./sparky.sh activate                    # what's live, and what's activatable
./sparky.sh activate qwen3-vl-235b-a22b-instruct-nvfp4        # make it live
./sparky.sh activate empty              # stop serving; free the hardware
```

`activate` is synchronous and does the whole job: writes the request, runs the
reconciler across every node, waits for the engines to answer (a big model loads for
minutes), then runs the smoke gate. **Exit code is the verdict** — non-zero means it
did not end up serving that model.

- It **refuses** a profile that isn't in the allowlist, and tells you what is. That's a
  deploy-shaped problem, not something to retry.
- On any node's failure it drives the fleet to **`empty`** rather than guessing —
  so a failed activation leaves nothing serving, not something half-serving.
- `--no-wait` / `--no-smoke` if you only want the switch requested (e.g. you'll poll
  yourself). Default is to wait and gate.
- `--force` re-activates the current profile, restarting its engines. This is the
  recovery gesture after a fail-safe boot, and how you apply a deploy that re-rendered
  the live engine's definition.

```bash
./sparky.sh fleet     # the allowlist: deployed / live / parked, per-node weights
```

## Verify a deploy / check live status

```bash
./sparky.sh status            # human table: per-engine, per-node systemd + API + model
./sparky.sh status --json     # same, machine-readable — parse this
```

**Exit code is the verdict:** `0` = healthy, `1` = something down / in fail-safe,
`2` = panel unreachable (and nothing was executed — it will never prompt). So gate on
it directly:

```bash
./sparky.sh status >/dev/null && echo "cluster healthy" || echo "cluster NOT healthy"
```

Gate on `ok`, but read `phase` before concluding anything is wrong:

```bash
./sparky.sh status --json | python3 -c 'import json,sys; s=json.load(sys.stdin)
print("go" if s["ok"] else ("wait — "+s["phase"] if s["phase"]=="loading" else "PROBLEM: "+s["phase"]))'
```

`--json` shape (`/admin/status.json` on the panel):

```json
{ "has_topology": true, "ok": true, "phase": "serving", "profile": "qwen3-vl-235b-a22b-instruct-nvfp4",
  "requested": "qwen3-vl-235b-a22b-instruct-nvfp4", "pending": [],
  "deployed_at": "2026-08-04T00:16:49Z", "failsafe": false,
  "engines": [ { "name": "qwen3-vl-235b", "kind": "vllm", "api_ok": true,
                 "model": "step-3.5-flash", "failsafe": false,
                 "nodes": [ { "node": "sparky", "state": "active", "failsafe": false },
                            { "node": "snoopy", "state": "active", "failsafe": false } ] } ],
  "services": [ { "name": "Open WebUI", "state": "running" }, … ] }
```

- **`ok`** — *serving right now*: no engine unhealthy and nothing in fail-safe. `all()`
  over zero engines is true, so an activated **`empty`** profile is `ok: true`.
- **`phase`** — what a not-`ok` *means*, which `ok` alone cannot tell you:
  `serving` · `switching` · `loading` · `stalled` · `down` · `failsafe` · `idle`.
  **This is the one to branch on after an activation.** A big model spends ten to
  twenty minutes `loading` — units up, API down — and that is the expected state, not a
  failure. Retrying or declaring the activation broken during it is wrong; **wait**.
  `switching` means an activation is in flight and the topology still describes the
  outgoing profile — also wait. `stalled` means it has been that way longer than a
  weight load should take, and *is* a fault. (`sparky activate` already waits for readiness for you — this matters when
  you are polling status yourself, or looking at a switch someone else started.)
- **`profile`** vs **`requested`** — what came up vs what was asked for. They differ
  when an activation failed and fell to `empty`; `requested` still names the intent.
- **`pending`** — a deploy re-rendered these engines while they were serving. Deploy is
  selection-neutral and won't drop a healthy engine, so the new definition is installed
  but *not running*: `./sparky.sh activate <profile>` applies it.
- **`has_topology: false`** — nothing has been activated yet. `ok` is `false` here.
- **`failsafe: true`** — the ADR-0009 recovery state: an unclean shutdown left a marker,
  so a node booted empty on purpose. **Recovery is `./sparky.sh activate <profile>`**
  (it clears the marker and starts the engines) — not a deploy.

Curl them directly if the CLI isn't handy — note `/admin` is behind basic_auth, but the
panel itself on localhost is not: `curl -s 127.0.0.1:8088/status.json`.

## Did the last activation pass its gate?

```bash
cat /opt/cluster/last-smoke.json        # per-engine ready/tool-shape/quality + ok + ran_at
cat /opt/cluster/current-topology.json  # what IS running  (reconciler-written)
cat /opt/cluster/fleet.json             # what MAY run     (deploy-written)
```

`last-smoke.json` is written **pass or fail**, and the reconciler **deletes** it at the
start of every activation — so if it's present it is about the currently-live model, and
if it's absent the current model hasn't been gated yet. That's the guarantee to rely on;
don't treat a stale-looking file as evidence.

## Re-probe live vs. read the breadcrumb

- "Is it healthy *right now*?" → `./sparky.sh status` (live, every node).
- "Did the last activation's gate pass?" → read `last-smoke.json` (no cluster load).
- "Re-run the full quality gate now" (readiness + tool-shape + multiturn) →
  `./sparky.sh smoke` (~2 min/engine — actually exercises the model; use when you need
  a fresh quality verdict, not just liveness).
- "What could we run?" → `./sparky.sh fleet`.

## Run a long sweep without your session holding it up

A sweep commandeers the cluster for hours — it activates one profile after another, so
serving is whatever it is currently measuring. Two consequences.

**Detach it.** Unlike `deploy`, a sweep needs no TTY (it is unprivileged — no sudo
password to type), so it can simply be backgrounded. A dropped SSH session sends SIGHUP
and would otherwise kill it mid-regiment:

```bash
setsid nohup ./sparky.sh sweep sweeps/nemotron-family.yml > /opt/cluster/sweep.log 2>&1 &
```

**Then check on it from anywhere**, including a fresh shell after the connection dropped.
This reads the breadcrumbs on disk, so it is accurate even if the process that wrote them
is gone:

```bash
python3 -c "from sparky import sweep; print(sweep.holder() or 'no sweep running'); print(sweep.progress(sweep.load_state()))"
```

```bash
tail -f /opt/cluster/sweep.log
```

**Being killed is safe, and that is deliberate.** Breadcrumbs are written after every
*regiment*, so re-running resumes rather than restarts — a 45-minute soak is never repeated
because the bench after it died. The exclusive lock expires after six hours, so a killed
run does not block the next one forever; if you know it is dead sooner, delete
`/opt/cluster/sweep.lock`.

**Do not start a second sweep.** It will refuse, and that refusal is the point: two sweeps
interleaving activations would each measure whatever the other last activated, and the
numbers would look fine while belonging to no configuration.

## See also

- [`docs/control-interface.md`](../../docs/control-interface.md) — the panel's surfaces + the status contract.
- [`docs/adr/0018-provision-select-split.md`](../../docs/adr/0018-provision-select-split.md) — why the split, and the safety invariants.
- [`docs/updating.md`](../../docs/updating.md) — change pathways (the deploy-shaped work to hand to Geoff).
- [`docs/defects.md`](../../docs/defects.md) — if status shows a known-bad state, check the register.
