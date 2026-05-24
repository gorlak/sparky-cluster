# Control interface (admin panel + metrics) — design

**Status:** P1 (control panel — read-only status at `/admin`) and P2 (metrics:
node + GPU exporters, Prometheus, Grafana at `metrics.{{ web_domain }}`) are
**built and deployed**. P3 (control actions via `ansible-runner`) is the
remaining phase.

This is the "Dashboard" item from the README's Future Work. It has two halves: a
small custom **control panel** that drives Ansible, and a **Grafana metrics**
stack. Kept here for reference and the inevitable design iterations.

## Goals

- Drive cluster operations (deploy a profile, teardown, restart, see state) from a
  browser anywhere on the LAN/VPN — the machines are headless.
- See cluster metrics (GPU, throughput, latency, system).
- Stay reproducible: everything is Ansible roles in this repo, deployed via
  `make deploy`, like the rest of the cluster.

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
- **"Current profile" detection** for the status view — write a small state file at
  deploy time (the panel records the profile it deployed).
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
