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
    itl_mean_ms   REAL, itl_p99_ms  REAL
);
"""

_METRIC_COLS = (
    "output_toks_s", "total_toks_s", "requests_s",
    "ttft_mean_ms", "ttft_p99_ms", "tpot_mean_ms", "tpot_p99_ms",
    "itl_mean_ms", "itl_p99_ms",
)


@dataclass
class Run:
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


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record(self, run: Run) -> int:
        """Insert one run; returns its row id."""
        cols = ["ts", "label", "model", "profile", "scenario", "skipped", "quality_pass", *_METRIC_COLS]
        vals = [
            run.ts or int(time.time()),
            run.label, run.model, run.profile, run.scenario,
            int(run.skipped),
            None if run.quality_pass is None else int(run.quality_pass),
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
