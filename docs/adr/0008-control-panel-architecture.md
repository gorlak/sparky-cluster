# ADR-0008: Control panel architecture (FastAPI/HTMX on-host as deploy user)

**Date:** 2026-05-24
**Status:** Accepted

## Context

Cluster operations (deploy a profile, teardown, restart an engine, check
status) needed to be driveable from a browser on the LAN without SSH access.
The control app must invoke Ansible and read cluster state — operations that
require the `deploy` identity, the deploy SSH key, and access to
`/opt/cluster/ansible`. See `docs/control-interface.md` for the full design.

## Options considered

**A. Rundeck / AWX / similar**
Purpose-built for job scheduling and Ansible execution. Heavy: requires its
own database, web server, and agent model. Overkill for a two-node homelab
and adds a large non-Ansible dependency to manage.

**B. Custom app in Docker**
Containerizing something that orchestrates the host (invokes `ansible-playbook`,
reads `current-topology.json`, uses the deploy SSH key) requires mounting the
host's entire `/opt/cluster` tree and the SSH key into the container. Awkward
layering: a container that exists to manage what's outside it.

**C. Custom app on-host as `deploy` user (systemd service)**
A `User=deploy` systemd service in a Python venv, bound to `127.0.0.1`. Has
native access to `ansible-core`, the deploy SSH key, and `/opt/cluster`.
Caddy proxies `/admin*` to it. Uses `ansible-runner` for detached execution
so a deploy that restarts the panel itself doesn't abort the in-flight run.

## Decision

On-host Python service as `deploy` user (option C): FastAPI + Jinja2 + HTMX,
server-rendered, bound to `127.0.0.1`.

## Consequences

- Full native access to Ansible and deploy credentials without container
  volume gymnastics.
- HTMX keeps the UI server-rendered with no SPA build step. Suitable for a
  homelab admin panel; not a trade-off that would matter at scale.
- Caddy is the sole HTTP ingress (`/admin*` → `127.0.0.1:<port>`). The
  `basic_auth` seam is a commented block in the Caddyfile — turning on auth
  is a ~5-minute change.
- Destructive actions (deploy, teardown) require a confirmation modal that
  shows current state → target state + estimated downtime. No type-to-confirm
  (single admin, accidental clicks are the realistic risk, not attackers).
- `ansible-runner` runs detached and single-run-locked: a deploy can restart
  the panel service mid-run without aborting the in-flight Ansible job.
- Phase delivery: P1 (status-only) and P2 (metrics: exporters + Prometheus +
  Grafana) are deployed. P3 (control actions: deploy/teardown/restart) is
  implemented and deployed.
