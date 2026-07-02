# ADR-0010: SQLite for benchmark trend storage

**Date:** 2026-07-02
**Status:** Accepted

## Context

Benchmark results need to be visible as time-series trends in Grafana so
that regressions from driver updates, container bumps, or new model deploys
are visible over time. The existing approach (JSON files in `benchmark/results/`)
is not queryable as a trend — you can only compare two specific labels with
`compare.py`, not see a chart of throughput over six months.

Grafana queries a datasource; it does not store time-series data itself. So
the question is what datasource to use.

## Options considered

**A. Prometheus Pushgateway**
Pushgateway is the standard Prometheus mechanism for batch jobs that can't
be scraped. The benchmark script `curl`s metrics to the gateway after each
run; Prometheus scrapes the gateway; Grafana queries Prometheus.

Pros: fits natively in the existing Prometheus + Grafana stack; no new query
language.

Cons: Pushgateway doesn't support historical timestamps — it stores "last
pushed value" and Prometheus timestamps it at scrape time, not at run time.
More importantly, metrics *persist* until explicitly deleted: if the benchmark
stops running (API down, script broken), Prometheus keeps returning the last
value indefinitely and Grafana shows a flat line that looks like success.
Requires a new Docker service.

**B. Prometheus TSDB backfill (`promtool tsdb create-blocks-from openmetrics`)**
Convert JSON files to OpenMetrics format with real timestamps, write TSDB
blocks directly into Prometheus's data directory, reload Prometheus.

Pros: true historical timestamps; no new service.

Cons: fiddly — requires write access to Prometheus's data directory (inside
the Docker volume), a `SIGHUP` or reload after each run, and `promtool`
available on the host. Fragile to Prometheus version changes.

**C. Flat JSON files only (current state)**
Already exists. `compare.py` handles two-label comparison.

Pros: zero new infrastructure.

Cons: no trend visibility in Grafana. Comparison is always manual and
point-in-time. Doesn't answer "has throughput been declining over the past
three weeks?"

**D. SQLite + Grafana SQLite datasource plugin (`frser-sqlite-datasource`)**
Benchmark script appends one row per scenario per run to `benchmark/results/benchmark.db`
with a real Unix timestamp. Grafana loads the `frser-sqlite-datasource` plugin
(set via `GF_INSTALL_PLUGINS` in the compose template), mounts the db file,
and queries it directly with SQL.

Pros: real timestamps from the benchmark run; no new service; append-only
rows are the natural model for batch job results; SQL in Grafana panel is
straightforward; the db file lives alongside the existing JSON results; no
stale-metric footgun.

Cons: benchmark trends and live vLLM operational metrics (from Prometheus)
are in separate Grafana datasources — they can't be overlaid on the same
panel without a join. The Grafana SQLite plugin is community-maintained
(`frser-sqlite-datasource`), not an official Grafana plugin.

## Decision

SQLite (option D).

## Schema

```sql
CREATE TABLE benchmark_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,        -- Unix epoch seconds, time of run
    label       TEXT NOT NULL,           -- human label (e.g. "26.04", "nvfp4")
    model       TEXT NOT NULL,           -- served-model-name
    profile     TEXT NOT NULL,           -- active profile from current-topology.json
    scenario    TEXT NOT NULL,           -- latency | throughput | prefix_cache | multiturn
    skipped     INTEGER NOT NULL DEFAULT 0,  -- 1 if API was down at run time
    quality_pass INTEGER,                -- 1/0/NULL (NULL = not a multiturn run)
    output_toks_s     REAL,
    total_toks_s      REAL,
    requests_s        REAL,
    ttft_mean_ms      REAL,
    ttft_p99_ms       REAL,
    tpot_mean_ms      REAL,
    tpot_p99_ms       REAL,
    itl_mean_ms       REAL,
    itl_p99_ms        REAL
);
```

## Runtime location of the db (writer and reader must share a path)

The db has two accessors that are not the same identity: the weekly timer /
benchmark script **writes** it, and the Grafana container (user uid 472) **reads**
it via a bind-mount. Neither can use the repo working tree — that is
geoff-owned (`0750` home), not readable by the deploy user or the container.

The db therefore lives at a published runtime path, the same pattern as
`/opt/cluster/ansible` and the model store: **`/opt/cluster/benchmark/benchmark.db`**,
owned `deploy:cluster` (group-writable) so the timer can write it and the
Grafana container can read it. The `benchmark/results/*.json` files stay in the
repo tree for `compare.py`; only the trend db is published. SQLite is opened in
WAL mode so Grafana's reads never block the writer.

## Consequences

- Grafana gets a `benchmark` datasource (SQLite) alongside the existing
  `prometheus` datasource. Adding it means a second entry in the `grafana`
  role's `datasource.yml.j2` (which today provisions only Prometheus), pointing
  at the mounted db path. Benchmark trend panels live on a separate Grafana
  dashboard from operational metrics — acceptable given the different cadence
  (weekly vs continuous).
- The `frser-sqlite-datasource` plugin is installed via `GF_INSTALL_PLUGINS`
  in the Grafana compose template, and `/opt/cluster/benchmark/benchmark.db` is
  bind-mounted read-only into the Grafana container. Both changes belong to the
  **`grafana`** role (the role that owns Grafana's compose file and
  provisioning), not `open-webui`.
- The JSON results files in `benchmark/results/` are kept in the repo tree as a
  raw record and for use by `compare.py`. The published db is the authoritative
  trend store.
- No stale-metric risk: a missed run produces a `skipped=1` row or no row,
  both of which appear as a gap in Grafana rather than a misleading flat line.
