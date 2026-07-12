#!/usr/bin/env bash
# sparky — the harness CLI (ADR-0010). Thin wrapper over the uv-managed package;
# no global install, no PATH bouncing. Works from any cwd:
#   ./sparky.sh topology <profile>
#   ./sparky.sh smoke
#   ./sparky.sh bench <label>   /   ./sparky.sh report <a> <b>
set -euo pipefail
repo="$(cd "$(dirname "$0")" && pwd)"
exec uv run --project "$repo" sparky "$@"
