"""Benchmark runner — wraps `vllm bench serve` and records to the trend store (ADR-0012).

`vllm bench serve` runs inside the NVIDIA container (ADR-0004 — the aarch64 pip
torch is CPU-only), hitting the engine's OpenAI API. Per scenario the runner shells
into the engine's container, saves the JSON summary, copies it out, parses it, and
writes one `store.Run` row. It reads the container/served-name/model from
`current-topology.json`, fixing the stale `benchmark/run.sh` (which hardcoded the
pre-profile `vllm` container name + Step-3.5). Parsing and command assembly are
unit-tested; running needs the live engine + container.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from sparky.store import Run, Store

SONNET = "/opt/vllm/vllm-src/benchmarks/sonnet.txt"

# vllm bench serve JSON summary key -> store.Run field.
_METRIC_MAP = {
    "output_throughput": "output_toks_s",
    "total_token_throughput": "total_toks_s",
    "request_throughput": "requests_s",
    "mean_ttft_ms": "ttft_mean_ms",
    "p99_ttft_ms": "ttft_p99_ms",
    "mean_tpot_ms": "tpot_mean_ms",
    "p99_tpot_ms": "tpot_p99_ms",
    "mean_itl_ms": "itl_mean_ms",
    "p99_itl_ms": "itl_p99_ms",
}

# Per-scenario `vllm bench serve` args (faithful to benchmark/run.sh).
_SCENARIO_ARGS: dict[str, list[str]] = {
    # one request at a time — TTFT + per-token speed at zero queueing
    "latency": [
        "--dataset-name", "sonnet", "--dataset-path", SONNET,
        "--sonnet-input-len", "512", "--sonnet-output-len", "256", "--sonnet-prefix-len", "64",
        "--request-rate", "1", "--num-prompts", "40", "--num-warmups", "5",
    ],
    # flood the server — peak tokens/s
    "throughput": [
        "--dataset-name", "sonnet", "--dataset-path", SONNET,
        "--sonnet-input-len", "512", "--sonnet-output-len", "512", "--sonnet-prefix-len", "64",
        "--request-rate", "inf", "--num-prompts", "200", "--num-warmups", "10",
    ],
    # shared long prefixes — TTFT collapses with --enable-prefix-caching, flat without
    "prefix_cache": [
        "--dataset-name", "prefix_repetition",
        "--prefix-repetition-prefix-len", "1024", "--prefix-repetition-suffix-len", "128",
        "--prefix-repetition-output-len", "256", "--prefix-repetition-num-prefixes", "5",
        "--request-rate", "inf", "--num-prompts", "100", "--num-warmups", "5",
    ],
}
SCENARIOS = tuple(_SCENARIO_ARGS)


def metrics_from_summary(summary: dict) -> dict:
    """Map a `vllm bench serve` JSON summary to `store.Run` metric fields."""
    return {field: summary.get(key) for key, field in _METRIC_MAP.items()}


def bench_command(
    container: str,
    model_path: str,
    served_name: str,
    scenario: str,
    result_filename: str,
    *,
    port: int = 8000,
    label: str = "",
) -> list[str]:
    """Assemble the `sudo docker exec … vllm bench serve …` argv for one scenario."""
    if scenario not in _SCENARIO_ARGS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {SCENARIOS}")
    return [
        "sudo", "docker", "exec", container, "vllm", "bench", "serve",
        "--host", "127.0.0.1", "--port", str(port),
        "--model", model_path, "--served-model-name", served_name,
        "--backend", "openai-chat", "--endpoint", "/v1/chat/completions",
        # Greedy — reproducible A/B trend numbers (newer vllm bench serve dropped
        # the temperature=0 default, so set it explicitly).
        "--temperature", "0",
        "--save-result", "--result-dir", "/tmp/", "--result-filename", result_filename,
        "--metadata", f"scenario={scenario}", f"label={label}",
        *_SCENARIO_ARGS[scenario],
    ]


def run_scenario(engine: dict, scenario: str, label: str) -> dict:
    """Run one scenario against a live engine (from current-topology.json); returns the summary.

    Not unit-tested — shells into the container. `engine` is a current-topology
    engine dict (`container`, `served_as`, `model`, `port`).
    """
    container = engine["container"]
    remote = f"/tmp/bench_{label}_{scenario}.json"
    cmd = bench_command(
        container, f"/models/{engine['model']}", engine["served_as"],
        scenario, Path(remote).name, port=int(engine.get("port", 8000)), label=label,
    )
    subprocess.run(cmd, check=True)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / Path(remote).name
        subprocess.run(["sudo", "docker", "cp", f"{container}:{remote}", str(local)], check=True)
        subprocess.run(["sudo", "docker", "exec", container, "rm", "-f", remote], check=False)
        return json.loads(local.read_text())


def run_all(
    label: str,
    engine: dict,
    store: Store,
    profile: str,
    *,
    scenarios: tuple[str, ...] = SCENARIOS,
) -> list[Run]:
    """Run every scenario against `engine`, recording one store row each."""
    recorded: list[Run] = []
    for scenario in scenarios:
        summary = run_scenario(engine, scenario, label)
        run = Run(
            label=label, model=engine["served_as"], profile=profile, scenario=scenario,
            **metrics_from_summary(summary),
        )
        store.record(run)
        recorded.append(run)
    return recorded
