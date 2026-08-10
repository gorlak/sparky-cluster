# Control interface (admin panel + metrics) — design

**Status:** P1 (read-only status at `/admin`) and P2 (metrics: node + GPU exporters,
Prometheus, Grafana at `metrics.{{ web_domain }}`) are **built and deployed**. P3
(control actions) is **built**, then **re-scoped by
[ADR-0018](adr/0018-provision-select-split.md)**: the panel's deploy / dry-run /
teardown actions are **gone**, and its one privileged action is now **activate**.
Implemented with detached subprocesses that write results to disk (not
`ansible-runner`; simpler, same crash-survival). The panel is topology-aware via
`current-topology.json` (reconciler-written since ADR-0018).

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
| `sparky.flummoxed.net/admin` | control panel | **basic_auth** (ADR-0018) |
| `sparky.flummoxed.net/cluster-health.json` | fail-safe banner probe | none (read-only sliver) |
| `api.sparky.flummoxed.net` | the fixed model endpoint | none (LAN) |
| `chat.sparky.flummoxed.net` | Open WebUI | Open WebUI login |
| `metrics.sparky.flummoxed.net` | Grafana | anonymous view |

All fronted by Caddy on `:80`. The `*.sparky.flummoxed.net` wildcard already
covers the new hosts — no new DNS.

## Key decisions (and why)

- **Split control from metrics.** Control = a small custom app; metrics = Grafana
  + Prometheus + exporters. Don't hand-build charts — keeps custom code minimal.
- **Control app runs on the host, not in Docker.** It drives systemd and reaches the
  other nodes over SSH; containerizing something that orchestrates the host is awkward.
  So: a systemd service in a Python venv.
- **It does NOT run as `deploy` (ADR-0018).** It runs as `activator`, the
  low-privilege activation identity. The original design gave it the `deploy` identity
  so it could run ansible — which meant *a web API with no auth could invoke
  `NOPASSWD: ALL`*. That is the hole ADR-0018 closes. The panel now holds exactly:
  write access to `/opt/cluster/desired-profile`, a **single-command** sudoers entry
  for `/usr/local/sbin/vllm-activate`, and an SSH key whose forced command on each
  worker is that same reconciler. It is deliberately **not** in the `docker` group —
  docker group membership is root-equivalent — so service liveness is checked over
  HTTP rather than with `docker inspect`.
- **The cost, taken deliberately:** no panel-triggered infra deploys. Adding a model,
  changing a flag, bumping a container — all password-gated CLI. What the panel can do
  is *choose among what a deploy already installed*, which is the operation you
  actually want from a phone.
- **FastAPI + Jinja + HTMX**, server-rendered. No SPA — it's a homelab panel.
- **Bound to `127.0.0.1`.** Caddy is the sole ingress (clean routing; also the
  natural place auth would sit). You still reach it remotely *via Caddy* at
  `/admin` — localhost-binding only prevents bypassing the proxy, not remote use.
- **Auth: deferred, then turned on.** A human-only panel behind the firewall could
  defer auth — the realistic risk was accidental clicks, handled by **confirmation
  modals** (current → target, Confirm/Cancel, no type-to-confirm). A panel that is
  deliberately agent-drivable and holds the activation grant cannot: ADR-0018 enables
  the `basic_auth` seam this design built for exactly that moment. The hash is a
  **runtime secret** (`/opt/cluster/admin-basic-auth.hash`, set once via
  `./sparky.sh admin-password`), never in git; a deploy refuses to serve the panel
  without one. One route stays open — `/cluster-health.json`, the landing page's
  three-boolean fail-safe probe.
- **Detached subprocesses for control actions**, single-run lock, so a run survives the
  panel restarting mid-flight.

## Architecture

### Control panel — `control-panel` role
- FastAPI/uvicorn + Jinja + HTMX in a venv (`/opt/cluster/control-panel`).
- `User=activator` systemd service, bound `127.0.0.1:<port>`. Installed by `deploy`,
  which also means the app tree is *not* writable by the identity that runs it.
- Two actions, both the same fixed program: **Activate** (write the request, trigger
  the reconciler) and **Restart engines** (`vllm-activate --force` — the recovery
  gesture after a fail-safe boot, and how you apply a deploy that re-rendered the live
  engine). Detached, captured to disk, single-run lock.
- The activate dropdown reads `/opt/vllm/engines/allowlist` — the same file the
  reconciler re-validates against — so it can never offer something activation would
  then refuse.
- Worker state comes back over the **read-only verb of the forced-command channel**
  (`ssh activator@worker status`). The panel holds no general-purpose remote command.
- Confirmation modal on each action (current → target).

### Status surfaces — the no-sudo contract (`/status.json` + the gate breadcrumb)

The panel is the cluster's **no-sudo live-status surface**, and this is deliberate:
it runs as `User=deploy` with the deploy SSH key, so its `gather()` already queries
systemd on *both* nodes without a password. Everything else should read *it* rather
than shell `sudo -u deploy ansible … systemctl` (which prompts for the user's password
and hangs a non-interactive agent).

- **`/admin/health.json`** — thin: `{failsafe, profile, has_topology}` (the landing
  page's fail-safe check).
- **`/admin/status.json`** — full machine-readable status: per-engine, per-node systemd
  state + API readiness + fail-safe, plus two derived top-line fields. **`ok`** means
  *serving right now* (nothing unhealthy, nothing in the ADR-0009 recovery state;
  vacuously true for an activated `empty`). **`phase`** says what a not-`ok` means —
  `serving` · `loading` · `stalled` · `down` · `failsafe` · `idle`. They are separate on
  purpose: an agent gating on `ok` must not start work against a model that is still
  loading, but a *human* needs to know the difference between "wait" and "broken".
- **`sparky status [--json]`** reads `/status.json` (no sudo) and **exits `0` healthy /
  `1` degraded / `2` panel-unreachable**, so a deploy can be gated on it. There is **no
  fallback**: reading status is not privileged (plain `systemctl is-active`, or the
  reconciler's `--status` verb, work as geoff on every node), so the old
  `sudo -u deploy ansible` route bought nothing — and it fired when the panel merely got
  *slow*, which is what happens when a node is down. On exit 2 the CLI names the direct
  per-node probes instead of running anything.

**Activation-gate breadcrumb — `/opt/cluster/last-smoke.json`** (`cluster_smoke_report`).
`./sparky.sh activate` runs the smoke gate (ADR-0012) once the engines answer and
records the per-engine result + overall `ok` + timestamp — written **pass or fail**, so
"what did the last activation's gate find?" is answerable without re-running anything.
The reconciler **deletes** this file at the start of every activation, so a stale
verdict can never be read as one about the live model.

**Three state files, three questions.** They are deliberately owned by different
things (ADR-0018), which is what makes them independently trustworthy:

| File | Answers | Written by |
|---|---|---|
| `/opt/cluster/fleet.json` | what **may** run — the allowlist, per-node placement | `deploy` |
| `/opt/cluster/current-topology.json` | what **is** running | the **reconciler** |
| `/opt/cluster/last-smoke.json` | how the last activation's gate went | `sparky activate` |

Agent-facing usage is in [`skills/operations/SKILL.md`](../skills/operations/SKILL.md).

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
3. **P3 — control actions (DONE, then re-scoped):** built as deploy / dry-run /
   teardown / per-engine restart, detached + locked, with confirm modals and a live run
   log. ADR-0018 replaced that set with **activate** / **restart engines** and moved the
   service off the `deploy` identity.

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
- **2026-08-08 (ADR-0018)** — **Removed the ansible status fallback**, found by a node
  reboot. The panel answers in ~0.5 s normally but blocks on an unreachable node, and
  the CLI's 5 s budget turned *slow* into *absent* — so `sparky status` silently shelled
  `sudo -u deploy ansible` and prompted for a password, in the one situation where a
  no-sudo status matters most. Three changes: the CLI budget now exceeds the panel's
  worst case; the panel probes nodes **concurrently** so one unreachable node costs one
  timeout rather than the sum (and the cost stops growing with the roster); and the
  fallback is gone rather than gated, because reading status was never privileged in the
  first place. Exit 2 now names the direct probes.
- **2026-08-07 (ADR-0018)** — Two false alarms found by watching a real switch.
  (1) **Fail-safe was firing on every profile change.** Detection was "marker present
  and not active", but `ExecStop` is `docker stop --time=120`, so a clean deliberate
  stop sits in `deactivating` for up to two minutes with the marker still armed. Now
  narrowed to *desired* **and** `inactive` **and** marker — the ADR-0009 state is the
  one alarm that must never cry wolf, so it is the one that must be narrow. `deploy`
  also now sweeps `.running` markers for engines that no longer exist, which were a
  permanent phantom source. (2) **The outgoing profile looked broken mid-switch**,
  because the reconciler records what's live only once the switch *lands* — so the
  panel was faithfully describing engines that were meant to be stopping. Added the
  **`switching`** phase, derived from request≠live, which is information the reader
  actually wants. It never masks a fail-safe.
- **2026-08-06 (ADR-0018)** — Added the **engine `phase`**. Making activation a panel
  action meant the panel became the surface you watch through a ten-to-twenty-minute
  weight load — a window in which "not serving" is the *correct* state, and which the
  binary healthy/degraded model rendered identically to a broken engine. That is both
  wrong and a good way to learn to ignore red. Phase is derived from the marker, the unit
  state, API readiness, and (new) how long the unit has been active — the reconciler's
  read-only `status` verb now reports `active_for`, so `loading` and `stalled` can be
  told apart at all. `stalled` is a fault that was previously invisible: minute 2 and
  minute 60 of a hung bring-up looked the same. `ok` kept its meaning so gating still
  works.
- **2026-08-04 (ADR-0018)** — Took the panel **off the `deploy` identity**. It runs as
  `activator` with three narrow grants and no path to arbitrary root; its deploy /
  dry-run / teardown actions are gone (adding a model or changing a flag is a
  password-gated CLI deploy — the automation deliberately given up), replaced by
  **activate** and **restart engines**, both the same fixed reconciler. Turned on the
  `basic_auth` seam ADR-0008 left ready, with the hash as a runtime secret rather than
  a repo var. Dropped `docker inspect` for HTTP liveness checks so the panel needn't be
  in the root-equivalent docker group, and moved worker status onto the bounded
  forced-command channel. `/status.json` gained `requested` (what was asked for, which
  can differ from what came up) and `pending` (a deploy re-rendered a live engine).
- **2026-07-25** — Made the panel the **no-sudo status surface for agents**: added
  `/status.json` (full `gather()` as JSON + derived `ok`), routed `sparky status
  [--json]` through it with a health-reflecting exit code (ansible/systemd path is now
  only the fallback), and added the durable deploy-gate breadcrumb `last-smoke.json`
  (`sparky smoke --report`, written pass-or-fail). Documented the workflow in the new
  `skills/operations` skill. No ADR — extends the P1 status panel (ADR-0008) and the
  smoke gate (ADR-0012) within their patterns.
