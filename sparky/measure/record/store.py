"""SQLite trend store for benchmark + quality runs (ADR-0012).

One append-only row per scenario per run, WAL mode so Grafana's reads never block
the writer. The runtime db lives at `/opt/cluster/benchmark/benchmark.db`
(`deploy:cluster`, group-writable) so the timer/CLI writes it and the Grafana
container reads it via bind-mount (ADR-0010). A missed run is a `skipped=1` row or
no row — a gap in the trend, never a misleading flat line.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path("/opt/cluster/benchmark/benchmark.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    label         TEXT NOT NULL,
    model         TEXT NOT NULL,
    profile       TEXT NOT NULL,
    scenario      TEXT NOT NULL,
    skipped       INTEGER NOT NULL DEFAULT 0,
    quality_pass  INTEGER,
    output_toks_s REAL, total_toks_s REAL, requests_s REAL,
    ttft_mean_ms  REAL, ttft_p99_ms REAL,
    tpot_mean_ms  REAL, tpot_p99_ms REAL,
    itl_mean_ms   REAL, itl_p99_ms  REAL,
    -- the `quality` regiment (ADR-0016): an accuracy score, so models can be RANKED
    -- rather than only compared on throughput. NULL for bench rows.
    accuracy      REAL,
    items         INTEGER,
    unparseable   INTEGER,
    -- Which harness produced this row. `vllm bench serve` measured inside the container;
    -- the HTTP-native regiment (ADR-0016) measures client-side and includes network and
    -- client overhead. Mixing them in one table would invite a false comparison, so rows
    -- carry their provenance and the scoreboard says so.
    harness       TEXT,
    -- Context capacity. Speed metrics cannot express "how much can this READ", which is
    -- the binding constraint for long-document and whole-codebase work.
    kv_tokens     INTEGER,
    max_model_len INTEGER
);
"""

# Columns added after the table shipped. SQLite has no "ADD COLUMN IF NOT EXISTS", and
# the trend store is a live file on the cluster — dropping it would discard the
# benchmark history the whole A/B story rests on, so migrate in place.
_MIGRATIONS = ("accuracy REAL", "items INTEGER", "unparseable INTEGER",
               "harness TEXT", "kv_tokens INTEGER", "max_model_len INTEGER",
               # Weighted partial credit (ADR-0024). Deliberately its own column rather
               # than a redefinition of `accuracy`, which stays pass@1 for every scenario:
               # a graded number and a binary one answer different questions, and
               # overloading the binary one changes the meaning of every historical row.
               "score REAL")

_METRIC_COLS = (
    "output_toks_s", "total_toks_s", "requests_s",
    "ttft_mean_ms", "ttft_p99_ms", "tpot_mean_ms", "tpot_p99_ms",
    "itl_mean_ms", "itl_p99_ms",
)


@dataclass
class Row:
    """One scenario result. `ts=0` is filled with the current epoch at insert."""

    label: str
    model: str
    profile: str
    scenario: str
    ts: int = 0
    skipped: bool = False
    quality_pass: bool | None = None
    output_toks_s: float | None = None
    total_toks_s: float | None = None
    requests_s: float | None = None
    ttft_mean_ms: float | None = None
    ttft_p99_ms: float | None = None
    tpot_mean_ms: float | None = None
    tpot_p99_ms: float | None = None
    itl_mean_ms: float | None = None
    itl_p99_ms: float | None = None
    accuracy: float | None = None
    items: int | None = None
    unparseable: int | None = None
    score: float | None = None
    harness: str | None = None
    kv_tokens: int | None = None
    max_model_len: int | None = None


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(benchmark_runs)")}
        for column in _MIGRATIONS:
            if column.split()[0] not in existing:
                self._conn.execute(f"ALTER TABLE benchmark_runs ADD COLUMN {column}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record(self, run: Row) -> int:
        """Insert one run; returns its row id."""
        cols = ["ts", "label", "model", "profile", "scenario", "skipped", "quality_pass",
                "accuracy", "items", "unparseable", "score", "harness", "kv_tokens",
                "max_model_len",
                *_METRIC_COLS]
        vals = [
            run.ts or int(time.time()),
            run.label, run.model, run.profile, run.scenario,
            int(run.skipped),
            None if run.quality_pass is None else int(run.quality_pass),
            run.accuracy, run.items, run.unparseable, run.score, run.harness,
            run.kv_tokens, run.max_model_len,
            *(getattr(run, c) for c in _METRIC_COLS),
        ]
        placeholders = ", ".join("?" * len(cols))
        cur = self._conn.execute(
            f"INSERT INTO benchmark_runs ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        self._conn.commit()
        return cur.lastrowid

    def rows(self, *, scenario: str | None = None) -> list[dict]:
        """All rows (optionally one scenario), oldest first — the trend order."""
        query = "SELECT * FROM benchmark_runs"
        params: tuple = ()
        if scenario is not None:
            query += " WHERE scenario = ?"
            params = (scenario,)
        query += " ORDER BY ts, id"
        return [dict(r) for r in self._conn.execute(query, params)]
