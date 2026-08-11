"""The fleet scoreboard (ADR-0011 Layer 3) — no hardware, no store.

The table is presentation; the DOMINANCE logic is a claim about which models are never
the right choice, and that has to be exactly right. Too eager and it retires a model
someone depends on; too shy and the scoreboard says nothing useful.
"""

from __future__ import annotations

from sparky import scoreboard


def _rows(*specs):
    """specs: (label, accuracy, out_toks_s) -> the two store rows each model produces."""
    out = []
    for label, accuracy, toks in specs:
        if accuracy is not None:
            out.append({"label": label, "profile": label, "ts": 1,
                        "scenario": scoreboard.QUALITY_SCENARIO, "accuracy": accuracy})
        if toks is not None:
            out.append({"label": label, "profile": label, "ts": 1,
                        "scenario": "throughput", "output_toks_s": toks,
                        "requests_s": toks / 100})
    return out


def test_a_model_worse_on_both_axes_is_dominated():
    table = scoreboard.build(_rows(("fast", 0.55, 800.0), ("slow", 0.71, 120.0),
                                   ("pointless", 0.40, 100.0)))
    _points, dominated = scoreboard.pareto(table)
    assert dominated == {"pointless"}


def test_the_frontier_is_never_dominated():
    """Both ends of a genuine trade-off must survive — retiring either would be a
    judgement about workload that this data cannot make."""
    table = scoreboard.build(_rows(("accurate", 0.80, 100.0), ("quick", 0.50, 900.0)))
    _points, dominated = scoreboard.pareto(table)
    assert dominated == set()


def test_an_equal_model_is_not_dominated_by_a_tie():
    """Dominance needs strictly better on at least one axis. Two identical models are
    both still valid choices — flagging one would be arbitrary."""
    table = scoreboard.build(_rows(("a", 0.60, 500.0), ("b", 0.60, 500.0)))
    _points, dominated = scoreboard.pareto(table)
    assert dominated == set()


def test_models_missing_a_measurement_are_shown_not_dropped():
    """A half-swept fleet must still render — otherwise an interrupted sweep loses the
    models it did finish, and the operator sees nothing."""
    table = scoreboard.build(_rows(("scored", 0.60, 500.0), ("quality-only", 0.72, None)))
    labels = [r.label for r in table]
    assert labels == ["quality-only", "scored"]
    partial = next(r for r in table if r.label == "quality-only")
    assert partial.cells[0].text == "72.0%"
    assert partial.cells[1].text == "—"
    assert "throughput" in partial.missing


def test_a_model_without_both_axes_is_left_out_of_the_scatter():
    """It cannot be placed on a trade-off plot, and guessing a coordinate would be a
    fabricated data point."""
    table = scoreboard.build(_rows(("full", 0.60, 500.0), ("partial", 0.90, None)))
    points, _dominated = scoreboard.pareto(table)
    assert [p[0] for p in points] == ["full"]


def test_the_latest_run_wins():
    """Re-measuring a model corrects its row rather than adding a second one."""
    rows = [
        {"label": "m", "profile": "m", "ts": 1, "scenario": scoreboard.QUALITY_SCENARIO,
         "accuracy": 0.50},
        {"label": "m", "profile": "m", "ts": 2, "scenario": scoreboard.QUALITY_SCENARIO,
         "accuracy": 0.70},
    ]
    table = scoreboard.build(rows)
    assert len(table) == 1
    assert table[0].cells[0].text == "70.0%"


def test_best_is_marked_per_column_and_respects_direction():
    """Higher accuracy wins; LOWER latency wins. Marking the max in both would praise
    the slowest model."""
    rows = _rows(("a", 0.60, 500.0), ("b", 0.70, 300.0))
    rows += [{"label": "a", "profile": "a", "ts": 1, "scenario": "latency",
              "ttft_p99_ms": 900.0, "tpot_mean_ms": 30.0},
             {"label": "b", "profile": "b", "ts": 1, "scenario": "latency",
              "ttft_p99_ms": 200.0, "tpot_mean_ms": 10.0}]
    table = scoreboard.build(rows)
    by_label = {r.label: r for r in table}
    accuracy_i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "accuracy")
    ttft_i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "TTFT p99")
    toks_i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "out tok/s")
    assert by_label["b"].cells[accuracy_i].best        # higher accuracy
    assert by_label["b"].cells[ttft_i].best            # LOWER latency
    assert by_label["a"].cells[toks_i].best            # higher throughput


def test_markdown_is_committable():
    table = scoreboard.build(_rows(("a", 0.60, 500.0), ("b", 0.70, 300.0)))
    md = scoreboard.to_markdown(table)
    assert md.startswith("| model |")
    assert "`a`" in md and "`b`" in md
    assert "**70.0%**" in md          # the winner is emphasised


def test_plot_needs_two_points():
    table = scoreboard.build(_rows(("only", 0.60, 500.0)))
    points, dominated = scoreboard.pareto(table)
    assert "at least two" in scoreboard.plot(points, dominated)


def test_accuracy_with_many_unparseable_items_is_flagged():
    """Unparseable answers count as wrong, so a high rate makes accuracy a FLOOR rather
    than a score — and incomparable to a model that parsed cleanly. MiniMax read 34%
    against Qwen3-VL's 4% on 2026-08-09; presenting those side by side unmarked would
    invite a false conclusion."""
    rows = [{"label": "messy", "profile": "p", "ts": 1,
             "scenario": scoreboard.QUALITY_SCENARIO,
             "accuracy": 0.48, "items": 140, "unparseable": 54},
            {"label": "clean", "profile": "q", "ts": 1,
             "scenario": scoreboard.QUALITY_SCENARIO,
             "accuracy": 0.71, "items": 140, "unparseable": 6}]
    table = {r.label: r for r in scoreboard.build(rows)}
    assert table["messy"].unreliable
    assert not table["clean"].unreliable
    assert "†" in scoreboard.to_markdown(scoreboard.build(rows))


def test_the_panel_snapshot_survives_a_missing_directory(tmp_path, monkeypatch):
    """The snapshot refresh runs after every recorded bench and eval. A measurement that
    SUCCEEDED must never be reported as failed because /opt/cluster is absent (a dev
    checkout), read-only, or mid-deploy — the snapshot is a convenience, the measurement
    is the product."""
    from sparky import cli
    monkeypatch.setattr(cli, "PANEL_SNAPSHOT", tmp_path / "nope" / "scoreboard.json")
    cli._refresh_panel_snapshot()          # must not raise


def test_the_snapshot_is_world_readable(tmp_path, monkeypatch):
    """The panel runs as `activator`, which is not in the `cluster` group that owns
    /opt/cluster. A snapshot it cannot read renders as 'no scoreboard yet'."""
    from sparky import cli
    target = tmp_path / "scoreboard.json"
    monkeypatch.setattr(cli, "PANEL_SNAPSHOT", target)

    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def rows(self):
            return [{"label": "m", "profile": "m", "ts": 1,
                     "scenario": scoreboard.QUALITY_SCENARIO, "accuracy": 0.6}]
    monkeypatch.setattr(cli.store, "Store", _DB)
    cli._refresh_panel_snapshot()
    assert target.exists()
    assert target.stat().st_mode & 0o004, "panel (activator) cannot read it"


def test_the_snapshot_carries_column_direction():
    """The A/B view computes b/a-1 in the browser and colours it. Which direction counts
    as *better* must travel with the data — a comparison that paints a latency regression
    green is worse than having no comparison at all."""
    table = scoreboard.build(_rows(("a", 0.60, 500.0)))
    points, dominated = scoreboard.pareto(table)
    payload = scoreboard.to_json(table, points, dominated)
    meta = {m["name"]: m["higher_is_better"] for m in payload["column_meta"]}
    assert meta["accuracy"] is True
    assert meta["out tok/s"] is True
    assert meta["TTFT p99"] is False        # lower latency wins
    assert meta["TPOT"] is False
    assert [m["name"] for m in payload["column_meta"]] == payload["columns"]


# --- one producer, not two --------------------------------------------------

def test_the_panel_snapshot_is_attributed_like_the_cli(monkeypatch, tmp_path):
    """The panel renders a FILE and does no analysis — which only holds while one thing
    WRITES that file. Two did, and they disagreed: the sweep's refresh skipped the profile
    attribution entirely, so every snapshot it wrote had no `hf_repo` (no Hub links on the
    web scoreboard, silently) and `retired: False` on everything (so Step-3.5-Flash and the
    four single-node profiles never left the page). Each sweep then overwrote whatever a
    correct `scoreboard --json` had produced.
    """
    import json

    from sparky import cli

    seen = {}
    real_table = cli._scoreboard_table       # captured BEFORE the patch, or it recurses

    def fake_table(*, include_retired=False):
        seen["called"] = True
        return real_table(include_retired=include_retired)

    monkeypatch.setattr(cli, "PANEL_SNAPSHOT", tmp_path / "scoreboard.json")
    monkeypatch.setattr(cli, "_scoreboard_table", fake_table)
    cli._refresh_panel_snapshot()
    assert seen.get("called"), "the snapshot must come from the one attributed producer"

    payload = json.loads((tmp_path / "scoreboard.json").read_text())
    assert payload["rows"], "expected the real trend store to have rows"
    assert not any(r["retired"] for r in payload["rows"]), "a retired row reached the panel"
    assert any(r["hf_repo"] for r in payload["rows"]), "no Hub links in the snapshot"
