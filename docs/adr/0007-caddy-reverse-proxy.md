# ADR-0007: Caddy as reverse proxy with wildcard DNS

**Date:** 2026-05-24
**Status:** Implemented

## Context

Multiple services (Open WebUI, Grafana, control panel, landing page) need to
be reachable from the LAN without exposing individual ports. A reverse proxy
on `:80` with hostname/path-based routing is the natural solution. The DNS
already has a single A record for sparky; a wildcard record was needed to
support per-service subdomains cleanly.

## Options considered

**A. Direct port exposure (no proxy)**
Each service gets its own port. Requires users to remember port numbers,
no shared TLS termination point, and adding a service means a new firewall
rule. Not scalable past 2-3 services.

**B. nginx**
Widely known and well-documented. Config is verbose for this use case;
`server {}` blocks for every virtual host, manual SSL management if needed
later.

**C. Traefik**
Docker-native, auto-discovers containers via labels. Powerful but adds
complexity (provider config, dashboard, middleware chains) that isn't needed
for a static set of services managed by Ansible, not Docker-native scheduling.

**D. Caddy**
Simple `Caddyfile` syntax, automatic HTTPS if a public domain is available,
handles the `*.sparky.flummoxed.net` wildcard pattern cleanly. Adding a new
service is one `handle` block in the Caddyfile — no DNS change required.

## Decision

Caddy (option D), with a wildcard DNS record (`*.sparky.flummoxed.net → sparky's IP`).

## Consequences

- Adding a new service requires only: a new `handle` block in the Caddyfile
  Ansible template and a new entry in `landing_services` (for the landing
  page link). No DNS change.
- Open WebUI sits at the root of its own subdomain (`chat.sparky.flummoxed.net`)
  because it doesn't support being served under a sub-path. This is covered
  by the wildcard record.
- The control panel is bound to `127.0.0.1` and only reachable via Caddy at
  `/admin` — the proxy is the sole ingress, which is also the natural place
  to add `basic_auth` later (one Caddyfile line + a bcrypt hash).
- Automatic HTTPS would work for a public domain; for the private
  `.flummoxed.net` LAN domain, HTTP on `:80` is sufficient.
