"""Unit tests for the SQLite trend store (ADR-0012). Uses a tmp_path db."""

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
