# ADR-0010: Test/bench harness (unified `sparky` package)

**Date:** 2026-07-05
**Status:** Accepted

## Context

Two bodies of work are queued that both need to *drive the live cluster, collect
structured results, and report/store them*: the functional test regiment
(ADR-0011) and the benchmark regiment (ADR-0012). Sketched independently they
duplicate the same substrate:

| Capability | Tests (0011) | Benchmarks (0012) |
|---|---|---|
| Load `profiles/*.yml` + `current-topology.json` → engines/ports/served-names | ✅ | ✅ |
| vLLM API client (readiness, chat, tool-shape probe, stateful multiturn) | ✅ | ✅ |
| Result store (SQLite trend db) | ✅ (quality-pass rows) | ✅ |
| Durable breadcrumb / WAL (survive a node-hang, ADR-0009 analog) | ✅ | ✅ |
| Non-interactive `deploy`-context `ansible-playbook` invoke | ✅ (Layer 4) | ✅ (on-demand) |
| Rich table reporting | ✅ | ✅ (`compare.py` today) |

That middle stack is one library. Everything CUDA-linked already lives in the
NVIDIA container (ADR-0004); everything that *talks to* the cluster is Python
already — `vllm bench serve`, the FastAPI control panel (ADR-0008),
`scripts/download.py`, and the multiturn quality client (a stateful `httpx`
loop) **must** be Python. Ansible stays the declarative config layer; Python is
the "programs that talk to the cluster" layer. So the substrate wants a home,
and the question is how to structure it.

This ADR also **absorbs the storage decision** formerly recorded on its own
(SQLite for benchmark trends): the result store is shared substrate — tests log
quality-pass rows to it too — not a benchmark-only concern, so it belongs here.

## Options considered

**A. Per-tool scripts (status quo, `download.py`-style).**
Each capability a standalone `uv run --script` with inline PEP-723 deps. Works
for one-file tools, but a shared library across a multi-command test+bench suite
outgrows single-file scripts: no shared imports, no test collection, deps
re-resolved per script. Rejected as the *home* (kept for genuine one-offs).

**B. Python-native task runner replacing Make (`nox` / `invoke` / `poe`).**
Move orchestration into Python. Idiomatic for test matrices — but it buys
nothing over the existing Make here (the ops verbs are thin wrappers over
`ansible-playbook`/`ssh`), and costs a tool everyone must learn. Make is
universal and the repo is already fluent in it. Rejected.

**C. One `uv`-managed package (`sparky`) + Make stays the thin entrypoint (chosen).**
A single `pyproject.toml` at the repo root defines the `sparky` package: a shared
library (topology loader, API client, SQLite store, breadcrumb/WAL, ansible
invoker, reporting) with a Typer CLI on top, plus the pytest suites. Make keeps
its role as the memorable operator UI — new targets (`lint`, `test`, `bench`,
`smoke`) all delegate to `uv run …`. The ansible `site.yml` smoke hook and
the weekly systemd timer call the **same** CLI entrypoints — one implementation,
many callers.

## Decision

Option C. Extend the existing pattern (Make = entrypoint, `uv` = Python
execution — as `make download` already does) from a single script to a real
package.

- **Package:** `sparky` (import name and CLI command). Managed with `uv`
  (`pyproject.toml` + `uv.lock`); deps `typer`, `httpx`, `rich`, `pytest`.
- **Shared library:** `sparky/topology.py` (profiles + `current-topology.json`),
  `sparky/api.py` (readiness / chat / tool-shape probe / multiturn), `sparky/store.py`
  (SQLite), `sparky/breadcrumb.py` (durable WAL), `sparky/ansible.py`
  (non-interactive playbook runner), `sparky/report.py` (rich tables — absorbs
  `benchmark/compare.py`).
- **CLI:** `sparky <bench|smoke|test|report …>` (Typer). pytest owns the
  pure unit/render layers; the CLI owns anything that touches the cluster.
- **Make (thin):** `make lint` (ansible-lint / yamllint / `--syntax-check`),
  `make test` (`uv run pytest`), `make bench` / `make smoke`.
- **First utilities:** the no-hardware layers — a template-render test (ADR-0011
  Layer 2) and the topology loader — not throwaway demos.

**Result store (absorbed from the retired SQLite ADR).** One append-only SQLite
db, WAL mode, at the published runtime path **`/opt/cluster/benchmark/benchmark.db`**
(`deploy:cluster`, group-writable) so the timer/CLI writes it and the Grafana
container (uid 472) reads it via bind-mount — the repo tree is geoff-owned `0750`
and readable by neither. One row per scenario per run: `ts, label, model,
profile, scenario, skipped, quality_pass`, plus the throughput/latency columns.
Grafana gets a second datasource (`frser-sqlite-datasource` via
`GF_INSTALL_PLUGINS`) alongside Prometheus; a missed run is a gap
(`skipped=1`/no row), never a stale flat line.

## Consequences

- **First CI-able surface in the project.** `make lint` + `uv run pytest` run in
  seconds with no hardware and belong in a `.github`/Codeberg CI as well as
  locally — the foundation both ADR-0011 and ADR-0012 build on.
- **The shared lib is the seam.** Tests and benchmarks become two front-ends over
  one library; the multiturn client, store, and breadcrumb harness exist once.
- **Non-interactive privileged path is a shared prerequisite** (also flagged by
  ADR-0011/0012): `make deploy` gates on `sudo -u deploy` prompting for a
  password; a runner can't answer that. The affordance is the same one the
  dashboard needs — execute in the `deploy` context (it holds `NOPASSWD: ALL`)
  via a scoped test entrypoint. Not needed for lint / pytest / the API client, so
  it doesn't block standing up the skeleton.
- **Breadcrumbs are the ADR-0009 analog for the harness:** durable on-disk intent
  markers (persistent disk, not `/run`) so a deploy-driving test that hard-hangs a
  node degrades to skip-and-continue instead of re-freezing on rerun.
- **`benchmark/compare.py` and `run.sh` are absorbed** into the package
  (`report.py` + the `bench` command); the JSON files stay for raw record.
- Status flips to **Implemented** as the shared library fills in; the scaffold
  (`pyproject.toml`, package skeleton, Make targets) lands with the first real
  utility, not as an empty foundation.
