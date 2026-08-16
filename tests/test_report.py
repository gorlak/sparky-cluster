"""Unit tests for the benchmark A/B report (ADR-0012)."""

from sparky.measure.record.report import compare, improvement_pct
from sparky.measure.record.store import Row, Store


def test_improvement_pct_is_direction_aware():
    assert improvement_pct(100, 120, higher_better=True) == 20.0
    assert improvement_pct(100, 80, higher_better=False) == 20.0   # latency down = better
    assert improvement_pct(100, 120, higher_better=False) == -20.0
    assert improvement_pct(0, 5, higher_better=True) is None       # no baseline
    assert improvement_pct(None, 5, higher_better=True) is None


def test_compare_computes_deltas_and_direction(tmp_path):
    with Store(tmp_path / "b.db") as s:
        s.record(Row(label="A", model="m", profile="p", scenario="throughput",
                     output_toks_s=100, ttft_p99_ms=100, ts=1))
        s.record(Row(label="B", model="m", profile="p", scenario="throughput",
                     output_toks_s=120, ttft_p99_ms=80, ts=2))
        cmp = compare(s, "A", "B")
    deltas = {d.metric: d for d in cmp["throughput"]}
    assert deltas["output_toks_s"].improvement_pct == 20.0 and deltas["output_toks_s"].better is True
    assert deltas["ttft_p99_ms"].improvement_pct == 20.0 and deltas["ttft_p99_ms"].better is True  # 100→80


def test_compare_uses_latest_row_per_label(tmp_path):
    with Store(tmp_path / "b.db") as s:
        s.record(Row(label="A", model="m", profile="p", scenario="latency", output_toks_s=50, ts=1))
        s.record(Row(label="A", model="m", profile="p", scenario="latency", output_toks_s=80, ts=2))  # newer
        s.record(Row(label="B", model="m", profile="p", scenario="latency", output_toks_s=80, ts=3))
        cmp = compare(s, "A", "B")
    d = {x.metric: x for x in cmp["latency"]}
    assert d["output_toks_s"].a == 80  # latest A, not 50
    assert d["output_toks_s"].improvement_pct == 0.0


def test_compare_handles_one_sided_scenario(tmp_path):
    with Store(tmp_path / "b.db") as s:
        s.record(Row(label="A", model="m", profile="p", scenario="latency", output_toks_s=50, ts=1))
        cmp = compare(s, "A", "B")
    d = {x.metric: x for x in cmp["latency"]}
    assert d["output_toks_s"].a == 50
    assert d["output_toks_s"].b is None
    assert d["output_toks_s"].improvement_pct is None
