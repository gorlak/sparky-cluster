# Control interface (admin panel + metrics) — design

**Status:** P1 (read-only status at `/admin`) and P2 (metrics: node + GPU
exporters, Prometheus, Grafana at `metrics.{{ web_domain }}`) are **built and
deployed**. P3 (control actions — deploy / dry-run / teardown / per-engine
restart) is **built** — implemented with detached subprocesses that write results
to disk (not `ansible-runner`; simpler, same crash-survival). The panel is now
topology-aware via `current-topology.json` (see serving-topology.md T5).

This is the "Dashboard" item from the README's Future Work. It has two halves: a
small custom **control panel** that drives Ansible, and a **Grafana metrics**
stack. Kept here for reference and the inevitable design iterations.

## Goals

- Drive cluster operations (deploy a profile, teardown, restart, see state) from a
  browser anywhere on the LAN/VPN — the machines are headless.
- See cluster metrics (GPU, throughput, latency, system).
- Stay reproducible: everything is Ansible roles in this repo, deployed via
  `./sparky.sh deploy`, like the rest of the cluster.

## Topology

| URL | Serves | Auth |
|---|---|---|
| `sparky.flummoxed.net/` | landing page (links) | none |
| `sparky.flummoxed.net/admin` | control panel | none now; seam built in |
| `chat.sparky.flummoxed.net` | Open WebUI | Open WebUI login |
| `metrics.sparky.flummoxed.net` | Grafana | anonymous view |

All fronted by Caddy on `:80`. The `*.sparky.flummoxed.net` wildcard already
covers the new hosts — no new DNS.

## Key decisions (and why)

- **Split control from metrics.** Control = a small custom app; metrics = Grafana
  + Prometheus + exporters. Don't hand-build charts — keeps custom code minimal.
- **Control app runs on the host, not in Docker.** It must use the `deploy`
  identity natively (`ansible-core`, `/opt/cluster/ansible`, the deploy SSH key,
  sudo). Containerizing something that orchestrates the host is awkward. So: a
  `User=deploy` systemd service in a Python venv.
- **FastAPI + Jinja + HTMX**, server-rendered. No SPA — it's a homelab panel.
- **Bound to `127.0.0.1`.** Caddy is the sole ingress (clean routing; also the
  natural place auth would sit). You still reach it remotely *via Caddy* at
  `/admin` — localhost-binding only prevents bypassing the proxy, not remote use.
- **No auth, for now.** The whole cluster is behind a firewall, single admin; the
  realistic risk is accidental clicks, not attackers. Guard destructive actions
  with a **confirmation modal** that reiterates "current `<state>` → new
  `<target>` (restarts X, ~Ys downtime)", Confirm/Cancel — no type-to-confirm.
- **Auth seam built in.** The Caddy `/admin` handle has a ready insertion point:
  later add `basic_auth` (one line + a bcrypt hash from a gitignored var). Turning
  auth on is a ~5-minute change; nothing else moves.
- **`ansible-runner` for control actions**, detached + single-run lock, so a
  deploy that happens to restart the panel itself doesn't abort the in-flight run.

## Architecture

### Control panel — `control-panel` role
- FastAPI/uvicorn + Jinja + HTMX in a venv (e.g. `/opt/cluster/control-panel`).
- `User=deploy` systemd service, bound `127.0.0.1:<port>`.
- Uses `ansible-runner` to launch `site.yml` / `teardown.yml`; captures status +
  logs; single-run lock.
- Confirmation modal on each action (current → target).

### Status surfaces — the no-sudo contract (`/status.json` + the gate breadcrumb)

The panel is the cluster's **no-sudo live-status surface**, and this is deliberate:
it runs as `User=deploy` with the deploy SSH key, so its `gather()` already queries
systemd on *both* nodes without a password. Everything else should read *it* rather
than shell `sudo -u deploy ansible … systemctl` (which prompts for Geoff's password
and hangs a non-interactive agent).

- **`/admin/health.json`** — thin: `{failsafe, profile, has_topology}` (the landing
  page's fail-safe check).
- **`/admin/status.json`** — full machine-readable status: per-engine, per-node
  systemd state + API readiness + fail-safe, plus a derived top-line **`ok`** (nothing
  the deploy intended is unhealthy and nothing is in the ADR-0009 recovery state; `ok`
  is vacuously true for a deployed `empty` profile). The JSON twin of the `/status`
  HTML view.
- **`sparky status [--json]`** reads `/status.json` (no sudo) and **exits `0` healthy /
  `1` degraded / `2` panel-unreachable**, so a deploy can be gated on it. It falls back
  to the ansible/systemd path only when the panel is down.

**Deploy-gate breadcrumb — `/opt/cluster/last-smoke.json`** (`cluster_smoke_report`).
The smoke gate (ADR-0012) runs `sparky smoke --report`, recording the per-engine
result + overall `ok` + timestamp. Because the gate runs *before* the topology is
recorded, a **failed** deploy still leaves this file — so "what did the last deploy's
gate find?" is answerable without re-running anything, even on a deploy that aborted.
Contrast `current-topology.json`, which only updates on a *passing* deploy: the two
together separate "last attempt's gate result" from "last good live topology." Teardown
clears both. Agent-facing usage is in [`skills/operations/SKILL.md`](../skills/operations/SKILL.md).

### Metrics — exporter role on all nodes; prometheus + grafana on the head
- `node_exporter` + a GPU exporter on sparky & snoopy; vLLM `/metrics` already
  exists.
- Prometheus (scrape targets derived from inventory) + Grafana (provisioned
  dashboards: vLLM, node, GPU; anonymous view) on sparky.

### Caddy — extend the existing role
- Root host: `handle /admin* → reverse_proxy 127.0.0.1:<port>`; otherwise
  `file_server` (landing). Commented `basic_auth` seam inside the `/admin` handle.
- New `metrics.` host → Grafana.
- Landing page (`landing_services`) gains **Admin** + **Metrics** links.

## Phases (read-only → destructive last)

1. **P1 — status only (DONE):** `control-panel` role + service + `/admin` route +
   landing link + a status view (services up/down, API health). No actions.
   Lowest risk; proves the `User=deploy` service + routing.
2. **P2 — metrics (DONE):** exporters + Prometheus + Grafana + dashboards +
   `metrics.`. Grafana lands on the cluster dashboard by default; anonymous
   Viewer; GB10 GPU-exporter `--query-field-names` workaround in place.
3. **P3 — control actions:** deploy / teardown / restart via `ansible-runner`
   (detached, locked) + confirm modals + live run log.

## Open questions / to verify

- **GPU exporter on GB10 (sm_121):** does `dcgm-exporter` work on Blackwell GB10?
  Fallback: an `nvidia-smi`-based exporter.
- **"Current profile" detection** for the status view — resolved by the
  `current-topology.json` state file in
  [`docs/serving-topology.md`](serving-topology.md): a deploy writes the resolved
  topology + profile name, and the panel reads it for both status and per-engine
  P3 actions.
- **Self-restart safety** — confirm an `ansible-runner` job survives the panel
  restarting mid-run (it's a detached process, not a child of the HTTP request).
- **Port assignments** — avoid the in-use ones: 8000 (vLLM), 8080 (Open WebUI),
  3000 (Grafana), 9090 (Prometheus), 9100 (node_exporter), 9400 (dcgm).

## Decisions log

- **2026-05-24** — initial design: split control/metrics; control app on-host as
  `User=deploy`; FastAPI+HTMX; auth-free behind the firewall with a built-in seam;
  confirmation modals (no type-to-confirm); phased P1 → P3.
- **2026-05-24** — P1 (status panel) + P2 (exporters + Prometheus + Grafana)
  built and deployed. GB10 GPU exporter needed a pinned `--query-field-names`
  set (its `clocks_event_reasons_* [us]` fields become invalid Prometheus metric
  names and crash-loop the container). Grafana set to land on the cluster
  dashboard via `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`. P3 remains.
- **2026-07-25** — Made the panel the **no-sudo status surface for agents**: added
  `/status.json` (full `gather()` as JSON + derived `ok`), routed `sparky status
  [--json]` through it with a health-reflecting exit code (ansible/systemd path is now
  only the fallback), and added the durable deploy-gate breadcrumb `last-smoke.json`
  (`sparky smoke --report`, written pass-or-fail). Documented the workflow in the new
  `skills/operations` skill. No ADR — extends the P1 status panel (ADR-0008) and the
  smoke gate (ADR-0012) within their patterns.
