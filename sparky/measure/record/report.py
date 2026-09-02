"""Benchmark A/B report from the trend store (ADR-0012; absorbs benchmark/compare.py).

Compares the latest run of two labels per scenario, computing per-metric percent
*improvement* (direction-aware: throughput up is good, latency down is good).
`compare` is pure and unit-tested; `render` prints a rich table per scenario.
"""

from __future__ import annotations

from dataclasses import dataclass

# (store column, display, format, higher_is_better)
METRICS = [
    # Accuracy and context first: they are what a paired comparison is usually ABOUT.
    # The TP=1 vs TP=2 question is "what does decode cost me, and how much context do I
    # gain" — and a report that showed only tokens/s could not answer either half.
    ("accuracy", "Accuracy", "{:.1%}", True),
    ("kv_tokens", "KV context (tokens)", "{:,.0f}", True),
    ("output_toks_s", "Output tok/s", "{:.1f}", True),
    ("total_toks_s", "Total tok/s", "{:.1f}", True),
    ("requests_s", "Requests/s", "{:.2f}", True),
    ("ttft_mean_ms", "TTFT mean (ms)", "{:.1f}", False),
    ("ttft_p99_ms", "TTFT p99 (ms)", "{:.1f}", False),
    ("tpot_mean_ms", "TPOT mean (ms)", "{:.2f}", False),
    ("tpot_p99_ms", "TPOT p99 (ms)", "{:.2f}", False),
    ("itl_mean_ms", "ITL mean (ms)", "{:.2f}", False),
    ("itl_p99_ms", "ITL p99 (ms)", "{:.2f}", False),
    ("prefill_toks_s", "Prefill tok/s", "{:.0f}", True),
]
SCENARIOS = ("quality:mmlu-pro", "latency", "throughput", "prefix_cache",
             "prefill@4k", "prefill@16k", "prefill@64k")


def improvement_pct(a: float | None, b: float | None, higher_better: bool) -> float | None:
    """Percent improvement of b over a, sign-corrected so + is always better."""
    if a is None or b is None or a == 0:
        return None
    raw = (b - a) / abs(a) * 100.0
    return raw if higher_better else -raw


@dataclass
class MetricDelta:
    metric: str
    display: str
    fmt: str
    a: float | None
    b: float | None
    improvement_pct: float | None

    @property
    def better(self) -> bool | None:
        return None if self.improvement_pct is None else self.improvement_pct > 0


def _latest_by_scenario(rows: list[dict], label: str) -> dict[str, dict]:
    # rows come oldest→newest, so the last write for a scenario wins.
    return {r["scenario"]: r for r in rows if r["label"] == label}


def compare(store, label_a: str, label_b: str) -> dict[str, list[MetricDelta]]:
    """Per-scenario metric deltas between the latest `label_a` and `label_b` runs."""
    rows = store.rows()
    a = _latest_by_scenario(rows, label_a)
    b = _latest_by_scenario(rows, label_b)
    out: dict[str, list[MetricDelta]] = {}
    for scenario in SCENARIOS:
        ra, rb = a.get(scenario), b.get(scenario)
        if ra is None and rb is None:
            continue
        deltas = []
        for col, display, fmt, higher in METRICS:
            va = ra.get(col) if ra else None
            vb = rb.get(col) if rb else None
            if va is None and vb is None:
                continue
            deltas.append(MetricDelta(col, display, fmt, va, vb, improvement_pct(va, vb, higher)))
        out[scenario] = deltas
    return out


def render(console, label_a: str, label_b: str, comparison: dict[str, list[MetricDelta]]) -> None:
    from rich.table import Table

    if not comparison:
        console.print(f"[yellow]No overlapping benchmark data for '{label_a}' / '{label_b}'.[/]")
        return
    for scenario, deltas in comparison.items():
        table = Table(title=scenario.upper(), title_justify="left")
        table.add_column("metric")
        table.add_column(label_a, justify="right")
        table.add_column(label_b, justify="right")
        table.add_column("Δ improvement", justify="right")
        for d in deltas:
            sa = d.fmt.format(d.a) if d.a is not None else "—"
            sb = d.fmt.format(d.b) if d.b is not None else "—"
            if d.improvement_pct is None:
                sd = "—"
            else:
                arrow = " ▲" if d.improvement_pct > 2 else (" ▼" if d.improvement_pct < -2 else "")
                color = "green" if d.better else "red"
                sd = f"[{color}]{d.improvement_pct:+.1f}%{arrow}[/]"
            table.add_row(d.display, sa, sb, sd)
        console.print(table)
