"""Unit tests for the benchmark runner's pure seams (ADR-0012).

The live run (docker exec into the container) isn't unit-tested; the JSON->store
mapping and the command assembly are.
"""

import pytest

from sparky.bench import SCENARIOS, bench_command, metrics_from_summary


def test_metrics_from_summary_maps_keys():
    summary = {
        "output_throughput": 100.0, "total_token_throughput": 150.0, "request_throughput": 2.5,
        "mean_ttft_ms": 30.0, "p99_ttft_ms": 60.0,
        "mean_tpot_ms": 5.0, "p99_tpot_ms": 9.0,
        "mean_itl_ms": 4.0, "p99_itl_ms": 8.0,
        "unrelated": 123,
    }
    m = metrics_from_summary(summary)
    assert m["output_toks_s"] == 100.0
    assert m["total_toks_s"] == 150.0
    assert m["requests_s"] == 2.5
    assert m["ttft_p99_ms"] == 60.0
    assert m["itl_mean_ms"] == 4.0
    assert "unrelated" not in m


def test_metrics_missing_keys_are_none():
    assert metrics_from_summary({})["output_toks_s"] is None


def test_scenarios_are_the_three():
    assert set(SCENARIOS) == {"latency", "throughput", "prefix_cache"}


def test_bench_command_shape():
    cmd = bench_command(
        "vllm-minimax-m2.7-nvfp4", "/models/M", "minimax-m2", "throughput", "out.json",
        port=8000, label="26.04",
    )
    assert cmd[:5] == ["sudo", "docker", "exec", "vllm-minimax-m2.7-nvfp4", "vllm"]
    assert cmd[5:7] == ["bench", "serve"]
    assert "--served-model-name" in cmd and "minimax-m2" in cmd
    assert "--result-filename" in cmd and "out.json" in cmd
    assert "--request-rate" in cmd and "inf" in cmd  # throughput floods
    assert cmd[cmd.index("--temperature") + 1] == "0"  # greedy for reproducibility
    i = cmd.index("--metadata")
    assert cmd[i + 1] == "scenario=throughput"
    assert cmd[i + 2] == "label=26.04"


def test_bench_command_rejects_unknown_scenario():
    with pytest.raises(ValueError):
        bench_command("c", "/m", "s", "nonsense", "f.json")
