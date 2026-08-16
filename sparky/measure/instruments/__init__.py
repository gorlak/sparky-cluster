"""measure/instruments — the regiments that actually MEASURE a model.

`bench` (throughput/latency via `vllm bench serve`), `evals` (MMLU-Pro subset), `coding`
(the ADR-0024 scorer) with its apparatus `sandbox` (the confined runner) and `reference`
(the yardstick model), `soak` (endurance), `tools` (the tool-calling shape). Each is a
standalone instrument the loop points at; they do not depend on each other.
"""
