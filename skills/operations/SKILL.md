---
name: operations
description: Verify a deploy and read live cluster status the no-sudo way. Read when asked "is the cluster up / healthy", to confirm a deploy or restart worked, to check which profile is live, or before/after a deploy. Covers `sparky status`, the control panel JSON surfaces, and the deploy-gate breadcrumb.
---

## The one rule: read status, don't shell sudo

`./sparky.sh status` reads the **control panel** (`User=deploy`, `127.0.0.1:8088`),
which already queries systemd on **both** nodes with deploy's SSH key — so it needs
**no password**. Do NOT reach for `sudo -u deploy ansible … systemctl`: that prompts
for Geoff's password and hangs a non-interactive agent. (`sparky status` falls back to
that path only if the panel is down, and says so.)

## Verify a deploy / check live status

```bash
./sparky.sh status            # human table: per-engine, per-node systemd + API + model
./sparky.sh status --json     # same, machine-readable — parse this
```

**Exit code is the verdict:** `0` = healthy, `1` = something down / in fail-safe,
`2` = panel unreachable. So an agent can gate on it directly:

```bash
./sparky.sh status >/dev/null && echo "cluster healthy" || echo "cluster NOT healthy"
```

`--json` shape (`/admin/status.json` on the panel):

```json
{ "has_topology": true, "ok": true, "profile": "step-3.5-fp8",
  "deployed_at": "2026-07-25T00:16:49Z", "failsafe": false,
  "engines": [ { "name": "step-3.5-fp8", "kind": "vllm", "api_ok": true,
                 "model": "step-3.5-flash", "failsafe": false,
                 "nodes": [ { "node": "sparky", "state": "active", "failsafe": false },
                            { "node": "snoopy", "state": "active", "failsafe": false } ] } ],
  "services": [ { "name": "Open WebUI", "state": "running" }, … ] }
```

- **`ok`** — top-line health: no engine unhealthy and nothing in fail-safe. `all()`
  over zero engines is true, so a deployed **`empty`** profile is `ok: true`.
- **`has_topology: false`** — nothing deployed, or a deploy is mid-flight (topology
  isn't recorded until the end). `ok` is `false` here.
- **`failsafe: true`** — the ADR-0009 recovery state: an unclean shutdown left a
  marker, so a node booted empty on purpose. Recovery is a redeploy.

The panel also serves `/admin/health.json` (thin: `failsafe`/`profile`/`has_topology`)
and `/admin/status.json` (full). Curl them directly if the CLI isn't handy:
`curl -s 127.0.0.1:8088/status.json`.

## Did the last deploy pass its gate?

The deploy's smoke gate (ADR-0012) writes a **durable breadcrumb** — read it instead
of re-running anything:

```bash
cat /opt/cluster/last-smoke.json      # per-engine ready/tool-shape/quality + overall ok + ran_at
cat /opt/cluster/current-topology.json  # which profile/engines the last deploy recorded
```

`last-smoke.json` is written **pass or fail** (the gate runs before the topology is
recorded, so a *failed* deploy still leaves its result here — that's how you see what
broke). `current-topology.json` only updates on a **passing** deploy, so the two
together tell you "last attempt's gate result" vs "last good live topology."

## Re-probe live vs. read the breadcrumb

- "Is it healthy *right now*?" → `./sparky.sh status` (live, both nodes).
- "Did the last deploy's gate pass?" → read `last-smoke.json` (no cluster load).
- "Re-run the full quality gate now" (readiness + tool-shape + multiturn) →
  `./sparky.sh smoke` (~2 min/engine — actually exercises the model; use when you
  need a fresh quality verdict, not just liveness).

## See also

- [`docs/control-interface.md`](../../docs/control-interface.md) — the panel's surfaces + the status contract.
- [`docs/updating.md`](../../docs/updating.md) — change pathways (each ends by verifying with the above).
- [`docs/defects.md`](../../docs/defects.md) — if status shows a known-bad state, check the register.
