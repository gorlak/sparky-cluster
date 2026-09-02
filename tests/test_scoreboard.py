"""The fleet scoreboard (ADR-0011 Layer 3) — no hardware, no store.

The table is presentation; the DOMINANCE logic is a claim about which models are never
the right choice, and that has to be exactly right. Too eager and it retires a model
someone depends on; too shy and the scoreboard says nothing useful.
"""

from __future__ import annotations

from sparky.measure.record import scoreboard


def _rows(*specs):
    """specs: (label, accuracy, out_toks_s) -> the two store rows each model produces.

    Speed lives under the concurrency-1 `latency` scenario — that is what the scoreboard's
    out tok/s reads, since the fleet's workload is a single user session (2026-09-02)."""
    out = []
    for label, accuracy, toks in specs:
        if accuracy is not None:
            out.append({"label": label, "profile": label, "ts": 1,
                        "scenario": scoreboard.QUALITY_SCENARIO, "accuracy": accuracy})
        if toks is not None:
            out.append({"label": label, "profile": label, "ts": 1,
                        "scenario": "latency", "output_toks_s": toks,
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
    """A half-swept fleet must still render — otherwise an interrupted suite loses the
    models it did finish, and the operator sees nothing."""
    table = scoreboard.build(_rows(("scored", 0.60, 500.0), ("quality-only", 0.72, None)))
    labels = [r.label for r in table]
    assert labels == ["quality-only", "scored"]
    partial = next(r for r in table if r.label == "quality-only")
    assert partial.cells[0].text == "72.0%"
    assert partial.cells[1].text == "—"
    assert "latency" in partial.missing


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
    # out tok/s, TTFT and TPOT all live on the one concurrency-1 latency row, so build it
    # whole rather than as two rows that would collide on (label, scenario).
    rows = [{"label": "a", "profile": "a", "ts": 1, "scenario": scoreboard.QUALITY_SCENARIO,
             "accuracy": 0.60},
            {"label": "b", "profile": "b", "ts": 1, "scenario": scoreboard.QUALITY_SCENARIO,
             "accuracy": 0.70},
            {"label": "a", "profile": "a", "ts": 1, "scenario": "latency",
             "output_toks_s": 500.0, "ttft_p99_ms": 900.0, "tpot_mean_ms": 30.0},
            {"label": "b", "profile": "b", "ts": 1, "scenario": "latency",
             "output_toks_s": 300.0, "ttft_p99_ms": 200.0, "tpot_mean_ms": 10.0}]
    table = scoreboard.build(rows)
    by_label = {r.label: r for r in table}
    accuracy_i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "accuracy")
    ttft_i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "TTFT p99")
    toks_i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "out tok/s")
    assert by_label["b"].cells[accuracy_i].best        # higher accuracy
    assert by_label["b"].cells[ttft_i].best            # LOWER latency
    assert by_label["a"].cells[toks_i].best            # higher throughput


def test_the_prefill_column_reads_the_deepest_prefill_sweep():
    """prefill@64k is the long-context ingestion rate; it comes from the deepest prefill
    scenario's prefill_toks_s — a different store row than the speed and accuracy ones, and
    blank until that sweep has run."""
    rows = _rows(("m", 0.60, 90.0))
    rows.append({"label": "m", "profile": "m", "ts": 1, "scenario": "prefill@64k",
                 "prefill_toks_s": 1676.0})
    table = scoreboard.build(rows)
    i = next(i for i, c in enumerate(scoreboard.COLUMNS) if c[0] == "prefill@64k")
    assert table[0].cells[i].text == "1,676"


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


def test_two_models_on_the_same_cell_both_stay_visible():
    """minimax and nemotron-super sit at nearly the same (speed, accuracy) and mapped to one
    cell; a plain grid write let the later mark overwrite the earlier, and a whole model
    dropped off the plot silently. Every point must keep a distinct visible mark."""
    pts = [("m0", 25.0, 0.679), ("m1", 26.0, 0.686), ("m2", 96.0, 0.807)]
    art = scoreboard.plot(pts, set())
    grid_marks = {ch for line in art.splitlines() if line.startswith("  |")
                  for ch in line if ch.isalpha()}
    assert grid_marks == {"a", "b", "c"}, "a colliding point was overwritten off the grid"


def test_no_line_runs_off_the_edge():
    """A 43-char name used to shove the legend's tok/s column rightward and, with the
    `(dominated)` tag, push the line past 80 columns so a terminal wrapped it. Every line —
    grid, axis, and legend — must stay within one 80-column screen."""
    long_name = "nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4"
    pts = [(long_name, 32.0, 0.671), ("qwen3.6-35b-a3b-nvfp4", 96.0, 0.807)]
    art = scoreboard.plot(pts, {long_name})
    assert all(len(line) <= 80 for line in art.splitlines())
    assert "…" in art, "the over-long name should be elided, not printed in full"


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
    """The panel (activator) and the CLI (geoff) both rewrite the snapshot, sharing only the
    `activate` group. It must be world-readable (the panel reads it) AND group-writable — else
    whoever wrote it last freezes the other out (2026-09-02: an activator suite run could not
    overwrite a geoff-owned `chmod 644` snapshot, and the panel sat two weeks stale)."""
    from sparky import cli
    from sparky.foundation import topology
    target = tmp_path / "scoreboard.json"
    monkeypatch.setattr(cli, "PANEL_SNAPSHOT", target)
    # `m` must be in the live allowlist, or the scoreboard filters it as not-current and the
    # snapshot is empty — this test is about world-readability, not the retired filter.
    monkeypatch.setattr(cli.topology, "all_profiles",
                        lambda *a, **k: [topology.Profile(name="m", engines=())])

    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def rows(self):
            return [{"label": "m", "profile": "m", "ts": 1,
                     "scenario": scoreboard.QUALITY_SCENARIO, "accuracy": 0.6}]
    monkeypatch.setattr(cli.store, "Store", _DB)
    cli._refresh_panel_snapshot()
    assert target.exists()
    mode = target.stat().st_mode
    assert mode & 0o004, "panel (activator) cannot READ it"
    assert mode & 0o020, "the other identity cannot REFRESH it — the freeze bug (2026-09-02)"


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
    WRITES that file. Two did, and they disagreed: the suite's refresh skipped the profile
    attribution entirely, so every snapshot it wrote had no `hf_repo` (no Hub links on the
    web scoreboard, silently) and `retired: False` on everything (so Step-3.5-Flash and the
    four single-node profiles never left the page). Each suite then overwrote whatever a
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
