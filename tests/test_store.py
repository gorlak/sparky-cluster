"""Unit tests for the SQLite trend store (ADR-0012). Uses a tmp_path db."""

import sqlite3

from sparky.measure.record.store import Row, Store


def test_record_and_read_back(tmp_path):
    with Store(tmp_path / "b.db") as s:
        rid = s.record(Row(
            label="26.04", model="minimax-m2", profile="minimax-m2.7-awq",
            scenario="throughput", output_toks_s=123.4, ts=1000,
        ))
        rows = s.rows()
    assert rid == 1
    assert len(rows) == 1
    r = rows[0]
    assert (r["label"], r["scenario"], r["output_toks_s"], r["ts"]) == ("26.04", "throughput", 123.4, 1000)
    assert r["skipped"] == 0
    assert r["quality_pass"] is None


def test_ts_autofilled_when_zero(tmp_path):
    with Store(tmp_path / "b.db") as s:
        s.record(Row(label="x", model="m", profile="p", scenario="latency"))
        assert s.rows()[0]["ts"] > 0


def test_skipped_and_quality_pass_encoding(tmp_path):
    with Store(tmp_path / "b.db") as s:
        s.record(Row(label="x", model="m", profile="p", scenario="multiturn", quality_pass=True, ts=1))
        s.record(Row(label="x", model="m", profile="p", scenario="multiturn", quality_pass=False, ts=2))
        s.record(Row(label="x", model="m", profile="p", scenario="latency", skipped=True, ts=3))
        rows = s.rows()
    assert rows[0]["quality_pass"] == 1
    assert rows[1]["quality_pass"] == 0
    assert rows[2]["skipped"] == 1 and rows[2]["quality_pass"] is None


def test_rows_filter_by_scenario_is_trend_ordered(tmp_path):
    with Store(tmp_path / "b.db") as s:
        s.record(Row(label="x", model="m", profile="p", scenario="throughput", ts=20))
        s.record(Row(label="x", model="m", profile="p", scenario="latency", ts=10))
        s.record(Row(label="x", model="m", profile="p", scenario="throughput", ts=5))
        tput = s.rows(scenario="throughput")
    assert [r["ts"] for r in tput] == [5, 20]  # oldest first, filtered


def test_persists_across_connections(tmp_path):
    db = tmp_path / "b.db"
    with Store(db) as s:
        s.record(Row(label="x", model="m", profile="p", scenario="latency", ts=1))
    with Store(db) as s2:
        assert len(s2.rows()) == 1


def test_wal_mode_enabled(tmp_path):
    with Store(tmp_path / "b.db") as s:
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_max_model_len_column_renamed_in_place_preserving_history(tmp_path):
    """ADR-0030 schema cleanup: an existing DB carrying the old `max_model_len` column is
    renamed IN PLACE to `context_length` on open, so the benchmark history the A/B story
    rests on survives — not dropped and re-added empty."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE benchmark_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, label TEXT, model TEXT, "
        "profile TEXT, scenario TEXT, kv_tokens INTEGER, max_model_len INTEGER)")
    con.execute("INSERT INTO benchmark_runs (label, max_model_len) VALUES ('old', 262144)")
    con.commit()
    con.close()

    with Store(db) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(benchmark_runs)")}
        assert "context_length" in cols and "max_model_len" not in cols
        (val,) = s._conn.execute(
            "SELECT context_length FROM benchmark_runs WHERE label='old'").fetchone()
    assert val == 262144  # the historical value rode the rename, not lost to a fresh column


def test_prefill_toks_s_is_migrated_onto_an_existing_db_and_round_trips(tmp_path):
    """The prefill sweep added `prefill_toks_s` after the table shipped. A DB predating it must
    gain the column empty on open (its history preserved), and new rows must store and read the
    value back."""
    from sparky.measure.record import store as store_mod
    # an "old" DB is exactly today's schema minus the one new column — so every other column
    # record() writes is present, and only prefill_toks_s exercises the ADD-COLUMN path.
    old_schema = store_mod._SCHEMA.replace("    prefill_toks_s REAL,\n", "")
    assert "prefill_toks_s" not in old_schema
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(old_schema)
    con.execute("INSERT INTO benchmark_runs (ts, label, model, profile, scenario) "
                "VALUES (1, 'old', 'm', 'p', 'latency')")
    con.commit()
    con.close()

    with Store(db) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(benchmark_runs)")}
        assert "prefill_toks_s" in cols
        s.record(Row(label="new", model="m", profile="p", scenario="prefill@64k",
                     prefill_toks_s=1097.0, ts=2))
        got = {r["label"]: r["prefill_toks_s"] for r in s.rows()}
    assert got["old"] is None          # historical row gained the column empty, not dropped
    assert got["new"] == 1097.0        # and a fresh row round-trips the value
