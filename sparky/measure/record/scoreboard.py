"""The fleet scoreboard — every model on one screen, so a choice can be argued about.

`report` answers "did this change help?" for two labels. It cannot answer the question
that actually drives sourcing: **which of the models we have should be serving, and is a
new one worth the disk?** That needs the whole fleet, side by side, on the axes that
trade off against each other.

**The trade-off is the point, not a footnote.** Quality and speed pull in opposite
directions — MTP buys 2.3x decode and (ADR-0014's errata) costs vision and constrained
tool calling; a 235B model out-reasons a 35B one and serves a fraction as fast. A table
that showed only accuracy would recommend the biggest model every time, which is wrong
for a two-node cluster where the alternative is *also* not serving anything else.

Reads the trend store, taking the **latest** run per (label, scenario) — so re-running a
model updates its row rather than adding a second one, and a suite interrupted halfway
still renders the models it finished.

Deliberately no ranking column. There is no single ordering: the right model depends on
whether the workload is interactive, agentic or batch, and inventing a composite score
would hide that judgement behind a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

QUALITY_SCENARIO = "quality:mmlu-pro"

# (label, store scenario, column, format, higher_is_better)
COLUMNS = [
    ("accuracy", QUALITY_SCENARIO, "accuracy", "{:.1%}", True),
    # SINGLE-USER speed. Both come from the concurrency-1 (`latency`) scenario, because the
    # fleet's real workload is one session hitting the model at a time (chosen 2026-09-02).
    # The `throughput` scenario runs 16-way, and its aggregate out tok/s — ~5x higher for a
    # small-active MoE that batches well — answers a saturation question this cluster does not
    # face. That run is still recorded; it is just not what the board headlines. `req/s` here is
    # therefore single-user turnaround (1/request-time), not aggregate completions.
    ("out tok/s", "latency", "output_toks_s", "{:.0f}", True),
    ("req/s", "latency", "requests_s", "{:.2f}", True),
    ("TTFT p99", "latency", "ttft_p99_ms", "{:.0f}ms", False),
    ("TPOT", "latency", "tpot_mean_ms", "{:.1f}ms", False),
    ("prefix TTFT", "prefix_cache", "ttft_mean_ms", "{:.0f}ms", False),
    # Prefill speed at depth: prompt tokens ingested per second on a 64k-token prompt. This is
    # the long-context "how long before it answers a whole document" cost — attention is
    # O(n^2), so the `TTFT` column (a 512-token prompt) does not predict it. Blank until the
    # sweep that gathers the prefill scenarios runs (added 2026-09-02).
    ("prefill@64k", "prefill@64k", "prefill_toks_s", "{:,.0f}", True),
    # How much it can READ per request — the configured context window. Long-context work is
    # bound by this, not by tokens/s. NOT kv_tokens (the TOTAL KV pool, tens of millions): that
    # is concurrency/cache size, and reading it as "context" is what produced the outlandish
    # 34M numbers (2026-09-02). It was ALSO why sglang read blank — its kv_tokens was null, but
    # context_length comes through from /v1/models on every engine. For the fleet the KV pool
    # dwarfs the window, so configured == usable; a future tight-KV model (kv < window) would
    # want min(kv_tokens, this).
    ("context", "latency", "context_length", "{:,.0f}", True),
]


@dataclass
class Cell:
    value: float | None
    text: str
    best: bool = False


@dataclass
class Row:
    label: str
    profile: str
    cells: list[Cell] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Perf rows predating ADR-0016 came from `vllm bench serve` INSIDE the container.
    # Those numbers are not comparable to the HTTP-native ones (which include network and
    # client overhead), so a row that mixes harnesses must say so rather than imply a
    # like-for-like comparison.
    legacy_perf: bool = False
    # Accuracy measured with many items unparseable is a floor, not a score: every
    # unreadable answer counts as wrong, so the model is being punished for our
    # extraction. MiniMax read 34-39% unparseable on 2026-08-09 while Qwen3-VL read
    # 4%, which makes those two numbers incomparable however similar they look.
    unreliable: bool = False
    nodes: int | None = None      # fleet occupancy: 1 leaves a node free, 2 takes both
    # The exact upstream repo, so a row can be pasted into huggingface.co. The label is
    # the model name lowercased; the ORG is not recoverable from it.
    hf_repo: str | None = None
    # Measured, but the profile has since left the allowlist. The numbers stay — the TP=1
    # twins are the entire evidence for retiring the TP=1 shape, and deleting them would
    # erase the comparison that justified the decision. But a scoreboard that shows nine
    # models when the fleet has four invites picking one that cannot be activated.
    retired: bool = False


def latest_by_scenario(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(label, scenario) -> most recent row. `store.rows()` is oldest-first, so a later
    run simply overwrites — re-measuring a model corrects it rather than duplicating."""
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        out[(row["label"], row["scenario"])] = row
    return out


def build(rows: list[dict]) -> list[Row]:
    latest = latest_by_scenario(rows)
    labels = sorted({label for label, _ in latest})

    table: list[Row] = []
    for label in labels:
        profile = next((r["profile"] for (l, _), r in latest.items() if l == label), "?")
        entry = Row(label=label, profile=profile)
        entry.nodes = nodes_used(next((r for (l, _), r in latest.items() if l == label), None))
        q = latest.get((label, QUALITY_SCENARIO))
        if q and q.get("items") and (q.get("unparseable") or 0) > 0.15 * q["items"]:
            entry.unreliable = True
        for _name, scenario, column, fmt, _hib in COLUMNS:
            source = latest.get((label, scenario))
            value = source.get(column) if source else None
            if value is None:
                entry.cells.append(Cell(None, "—"))
                if scenario not in entry.missing:
                    entry.missing.append(scenario)
            else:
                entry.cells.append(Cell(float(value), fmt.format(value)))
                if scenario != QUALITY_SCENARIO and (source or {}).get("harness") != "http":
                    entry.legacy_perf = True
        table.append(entry)

    # mark the best value per column, so the eye finds the trade-off rather than reading
    # twelve numbers. Ties all get marked — a false "winner" is worse than a shared one.
    for i, (_n, _s, _c, _f, higher_better) in enumerate(COLUMNS):
        values = [r.cells[i].value for r in table if r.cells[i].value is not None]
        if not values:
            continue
        target = max(values) if higher_better else min(values)
        for row in table:
            if row.cells[i].value == target:
                row.cells[i].best = True
    return table


def to_markdown(table: list[Row]) -> str:
    """For committing into docs/ — a scoreboard that only exists in a terminal cannot be
    cited in a decision three weeks later."""
    head = "| model | " + " | ".join(c[0] for c in COLUMNS) + " |"
    rule = "|---" * (len(COLUMNS) + 1) + "|"
    lines = [head, rule]
    for row in table:
        cells = [f"**{c.text}**" if c.best and c.value is not None else c.text
                 for c in row.cells]
        if row.unreliable:
            cells[0] = f"{cells[0]}†"
        suffix = " ⚠︎" if row.legacy_perf else ""
        if row.retired:
            suffix += " (retired)"
        lines.append(f"| `{row.label}`{suffix} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


# --- the trade-off view -----------------------------------------------------

def nodes_used(row: dict | None) -> int | None:
    """How much of the FLEET a model occupies — this cluster's version of "cost".

    The literature ranks LLMs on quality x latency x price-per-token. We own the
    hardware, so price is not the scarce thing: NODES are. A TP=2 model takes both and
    leaves nothing for dev work or a second model; a single-node model leaves sparky
    entirely free. That is a real cost and it is why a 35B on one node can beat a 235B on
    two even while scoring lower.
    """
    if not row:
        return None
    profile = row.get("profile") or ""
    return 1 if profile.endswith("-single") else 2


def pareto(table: list[Row], x_col: str = "out tok/s", y_col: str = "accuracy"
           ) -> tuple[list[tuple[str, float, float]], set[str]]:
    """Points for the quality-vs-speed scatter, plus the set of DOMINATED labels.

    Dominance is computed over THREE axes — accuracy, throughput, and nodes occupied —
    even though only two are plotted. Two-axis dominance under-reports: a model that is
    accurate and fast but eats both nodes is genuinely beaten by one that matches it on
    a single node, and a 2D view cannot see that.

    A model is dominated when another is at least as good on every axis and strictly
    better on one. Everything on the frontier is a legitimate choice depending on
    workload; everything behind it is not. That is the only conclusion this data supports
    without smuggling in a weighting nobody agreed.
    """
    xi = next(i for i, c in enumerate(COLUMNS) if c[0] == x_col)
    yi = next(i for i, c in enumerate(COLUMNS) if c[0] == y_col)
    points = [(r.label, r.cells[xi].value, r.cells[yi].value) for r in table
              if r.cells[xi].value is not None and r.cells[yi].value is not None]

    nodes = {r.label: (r.nodes if r.nodes is not None else 2) for r in table}
    dominated = set()
    for label, x, y in points:
        for other, ox, oy in points:
            if other == label:
                continue
            # fewer nodes is better, so compare it inverted
            at_least = ox >= x and oy >= y and nodes[other] <= nodes[label]
            strictly = ox > x or oy > y or nodes[other] < nodes[label]
            if at_least and strictly:
                dominated.add(label)
                break
    return points, dominated


NAME_W = 40   # legend name column: wide enough for all but the longest, which get middle-elided


def _fit(text: str, width: int) -> str:
    """Middle-elide to `width` so BOTH ends survive. The family prefix and the quant suffix
    (`nvidia-nemotron-…-nvfp4`) are each identifying; lopping off either end makes two models
    read alike. A fixed width is also what keeps the legend's later columns in alignment —
    the old `{label:<34}` did not truncate, so a 43-char name shoved `tok/s` off to the right
    and, with `(dominated)`, ran the line past the terminal edge."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    head = (width - 1) // 2
    return text[:head] + "…" + text[len(text) - (width - 1 - head):]


def _place(grid: list[list[str]], row: int, col: int, mark: str) -> None:
    """Drop `mark` at (row, col), or the nearest free cell if that one is taken. Two models at
    nearly-identical speed+accuracy map to the same cell (minimax and nemotron-super do), and a
    plain assignment let the later one overwrite the earlier — which then vanished from the plot
    with no sign it was ever there. The scatter is a qualitative trade-off view, so nudging a
    collision to an adjacent cell is truer than dropping the point."""
    h, w = len(grid), len(grid[0])
    if grid[row][col] == " ":
        grid[row][col] = mark
        return
    for radius in range(1, max(h, w) + 1):     # expanding ring — the closest free cell wins
        for r in range(max(0, row - radius), min(h, row + radius + 1)):
            for c in range(max(0, col - radius), min(w, col + radius + 1)):
                if max(abs(r - row), abs(c - col)) == radius and grid[r][c] == " ":
                    grid[r][c] = mark
                    return
    grid[row][col] = mark                      # grid full (more points than cells) — don't lose it


def plot(points: list[tuple[str, float, float]], dominated: set[str],
         width: int = 56, height: int = 15) -> str:
    """A terminal scatter. Deliberately ASCII: this belongs next to the table in the same
    output, and a PNG the operator has to go and open would not get looked at.

    Two invariants keep it readable: every point gets a *visible* mark (collisions nudge to a
    free cell via `_place`, rather than silently overwriting), and no line runs past the frame
    (names middle-elide to a fixed column, and the right-hand axis label is flush to the edge,
    not spilling past it)."""
    if len(points) < 2:
        return "  (need at least two measured models to plot a trade-off)"
    xs = [p[1] for p in points]
    ys = [p[2] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    span_x = (x1 - x0) or 1.0
    span_y = (y1 - y0) or 1.0

    ordered = sorted(points, key=lambda p: -p[2])
    marks = [chr(ord("a") + i) if i < 26 else "*" for i in range(len(ordered))]

    grid = [[" "] * width for _ in range(height)]
    legend = []
    for mark, (label, x, y) in zip(marks, ordered):
        col = int((x - x0) / span_x * (width - 1))
        row = height - 1 - int((y - y0) / span_y * (height - 1))
        _place(grid, row, col, mark)
        flag = "  (dominated)" if label in dominated else ""
        legend.append(f"    {mark}  {_fit(label, NAME_W):<{NAME_W}} "
                      f"{x:>5.0f} tok/s  {y:>5.1%}{flag}")

    lo, hi = f"{x0:.0f} tok/s", f"{x1:.0f} tok/s"
    # `hi` flush to the frame's right edge: the label line's "   " gutter (3) matches the axis
    # rule's "  +" (3), and lo + pad + hi spans exactly `width`, so hi ends where the rule does.
    axis = "   " + lo + " " * max(1, width - len(lo) - len(hi)) + hi

    out = [f"  accuracy {y1:.0%}"]
    out += ["  |" + "".join(r) for r in grid]
    out.append(f"  accuracy {y0:.0%}" if y1 != y0 else "  |")
    out.append("  +" + "-" * width)
    out.append(axis)
    out.append("")
    out += legend
    return "\n".join(out)


def to_json(table: list[Row], points, dominated: set[str]) -> dict:
    """A snapshot for the control panel to render.

    The panel does NO analysis — dominance and best-marking are subtle enough that a
    second implementation would drift, and a scoreboard that disagrees with itself is
    worse than no scoreboard. `sparky` computes; the panel displays.
    """
    return {
        "columns": [c[0] for c in COLUMNS],
        # Per-column metadata, so the page can render an A/B delta without re-deciding
        # what "better" means. Direction is the part that is easy to get backwards —
        # lower TTFT wins, higher tok/s wins — and a comparison view that colours a
        # latency regression green is worse than no comparison view. The page does
        # arithmetic (b/a - 1); the JUDGEMENT of which way is good stays here.
        "column_meta": [{"name": n, "higher_is_better": hib, "scenario": s}
                        for n, s, _c, _f, hib in COLUMNS],
        "rows": [{
            "label": r.label, "profile": r.profile, "nodes": r.nodes,
            "hf_repo": r.hf_repo, "retired": r.retired,
            "unreliable": r.unreliable, "legacy_perf": r.legacy_perf,
            "missing": r.missing,
            "cells": [{"text": c.text, "best": c.best, "value": c.value} for c in r.cells],
        } for r in table],
        "scatter": {
            "points": [{"label": l, "x": x, "y": y, "dominated": l in dominated}
                       for l, x, y in points],
            "x_label": "output tok/s", "y_label": "accuracy",
        },
        "dominated": sorted(dominated),
    }
