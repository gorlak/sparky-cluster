#!/usr/bin/env bash
# Benchmark the live vLLM API across three scenarios.
# Usage: ./benchmark/run.sh [label]
# Results land in benchmark/results/ as JSON files.
set -euo pipefail

LABEL="${1:-baseline}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

MODEL_PATH="/models/Step-3.5-Flash-FP8"   # local path for tokenizer
SERVED_NAME="step-3.5-flash"              # --served-model-name on the API
SONNET="/opt/vllm/vllm-src/benchmarks/sonnet.txt"

echo "label    : $LABEL"
echo "timestamp: $TIMESTAMP"
echo "results  : $RESULTS_DIR"
echo ""

# Verify the API is up before starting
if ! curl -sf http://localhost:8000/health >/dev/null; then
    echo "ERROR: vLLM API not reachable at localhost:8000" >&2
    exit 1
fi

run_bench() {
    local scenario="$1"; shift
    local remote_file="/tmp/vllm_bench_${TIMESTAMP}_${scenario}.json"
    local local_file="$RESULTS_DIR/${TIMESTAMP}_${LABEL}_${scenario}.json"

    echo "━━━ $scenario ━━━"
    sudo docker exec vllm vllm bench serve \
        --host 127.0.0.1 \
        --port 8000 \
        --model "$MODEL_PATH" \
        --served-model-name "$SERVED_NAME" \
        --backend openai-chat \
        --endpoint /v1/chat/completions \
        --save-result \
        --result-dir /tmp/ \
        --result-filename "$(basename "$remote_file")" \
        --metadata "scenario=$scenario" "label=$LABEL" \
        "$@"
    sudo docker cp "vllm:$remote_file" "$local_file"
    sudo docker exec vllm rm -f "$remote_file"
    echo "  → $local_file"
    echo ""
}

# 1. Latency: one request at a time — measures TTFT and per-token speed at zero queueing
run_bench latency \
    --dataset-name sonnet \
    --dataset-path "$SONNET" \
    --sonnet-input-len 512 \
    --sonnet-output-len 256 \
    --sonnet-prefix-len 64 \
    --request-rate 1 \
    --num-prompts 40 \
    --num-warmups 5

# 2. Throughput: flood the server — measures peak tokens/s
run_bench throughput \
    --dataset-name sonnet \
    --dataset-path "$SONNET" \
    --sonnet-input-len 512 \
    --sonnet-output-len 512 \
    --sonnet-prefix-len 64 \
    --request-rate inf \
    --num-prompts 200 \
    --num-warmups 10

# 3. Prefix cache: 5 long shared prefixes, many requests per prefix — TTFT drops
#    dramatically when --enable-prefix-caching is on; flat when it's off.
run_bench prefix_cache \
    --dataset-name prefix_repetition \
    --prefix-repetition-prefix-len 1024 \
    --prefix-repetition-suffix-len 128 \
    --prefix-repetition-output-len 256 \
    --prefix-repetition-num-prefixes 5 \
    --request-rate inf \
    --num-prompts 100 \
    --num-warmups 5

echo "Done. Compare two runs with:"
echo "  ./benchmark/compare.py $LABEL <other-label>"
