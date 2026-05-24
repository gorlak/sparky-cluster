#!/usr/bin/env python3
"""Compare two benchmark runs side-by-side.
Usage: ./benchmark/compare.py <label_a> <label_b>
"""
import json
import sys
import glob
from pathlib import Path

# (key, display name, format, higher_is_better)
METRICS = [
    ("output_throughput",       "Output tok/s",      "{:.1f}",  True),
    ("total_token_throughput",  "Total tok/s",        "{:.1f}",  True),
    ("request_throughput",      "Requests/s",         "{:.2f}",  True),
    ("mean_ttft_ms",            "TTFT mean (ms)",     "{:.1f}",  False),
    ("p99_ttft_ms",             "TTFT p99 (ms)",      "{:.1f}",  False),
    ("mean_tpot_ms",            "TPOT mean (ms)",     "{:.2f}",  False),
    ("p99_tpot_ms",             "TPOT p99 (ms)",      "{:.2f}",  False),
    ("mean_itl_ms",             "ITL mean (ms)",      "{:.2f}",  False),
    ("p99_itl_ms",              "ITL p99 (ms)",       "{:.2f}",  False),
]

SCENARIOS = ["latency", "throughput", "prefix_cache"]

RESULTS_DIR = Path(__file__).parent / "results"


def load_label(label: str) -> dict[str, dict]:
    out = {}
    for scenario in SCENARIOS:
        files = sorted(RESULTS_DIR.glob(f"*_{label}_{scenario}.json"))
        if files:
            with open(files[-1]) as f:
                out[scenario] = json.load(f)
    return out


def fmt_delta(va: float, vb: float, higher_better: bool) -> str:
    if va == 0:
        return "n/a"
    raw = (vb - va) / abs(va) * 100
    improvement = raw if higher_better else -raw
    sign = "+" if improvement >= 0 else ""
    marker = " ▲" if improvement > 2 else (" ▼" if improvement < -2 else "  ")
    return f"{sign}{improvement:.1f}%{marker}"


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <label_a> <label_b>")
        sys.exit(1)

    label_a, label_b = sys.argv[1], sys.argv[2]
    data_a = load_label(label_a)
    data_b = load_label(label_b)

    if not data_a:
        sys.exit(f"No results found for '{label_a}' in {RESULTS_DIR}")
    if not data_b:
        sys.exit(f"No results found for '{label_b}' in {RESULTS_DIR}")

    col = 14
    for scenario in SCENARIOS:
        a = data_a.get(scenario)
        b = data_b.get(scenario)
        if a is None and b is None:
            continue

        print(f"\n{'━'*72}")
        print(f"  {scenario.upper()}")
        print(f"{'━'*72}")
        print(f"  {'Metric':<24}  {label_a:>{col}}  {label_b:>{col}}  {'Δ (improvement)':>16}")
        print(f"  {'-'*24}  {'-'*col}  {'-'*col}  {'-'*16}")

        for key, name, fmt, higher_better in METRICS:
            va = (a or {}).get(key)
            vb = (b or {}).get(key)
            if va is None and vb is None:
                continue
            sa = fmt.format(va) if va is not None else "—"
            sb = fmt.format(vb) if vb is not None else "—"
            sd = fmt_delta(va, vb, higher_better) if (va is not None and vb is not None) else "—"
            print(f"  {name:<24}  {sa:>{col}}  {sb:>{col}}  {sd:>16}")

    print()


if __name__ == "__main__":
    main()
