#!/usr/bin/env bash
# =============================================================================
# push-model.sh — rsync the pre-downloaded model from Mac to a Spark node
# =============================================================================
# Usage: ./push-model.sh <sparky|snoopy> [MODEL_DIRNAME]
#   MODEL_DIRNAME defaults to Qwen3-235B-A22B-Instruct-2507-FP8 (multinode
#   production). Pass Qwen3.5-122B-A10B-FP8 for the sparky single-node test.
#
# Source: ~/Projects/DGX-Spark-Setup/model-cache/<MODEL_DIRNAME>/
#   (on this Mac — should already be downloaded + verified)
#
# Destination: ~/model-cache/<MODEL_DIRNAME>/ on the node
#   (user's home, NOT the project dir — we don't want a 235GB breadcrumb in
#   the project, and sync.sh's --delete would nuke it anyway. The node-side
#   install-step2.sh moves it into /opt/vllm/models/.)
#
# Transport: rsync over SSH on 10GbE (hostname resolution should land on the
# Mac's local 10GbE path to the nodes). Uses --partial --inplace so a killed
# run resumes cleanly.
#
# Runs in parallel with itself: invoke for sparky and snoopy from two terminals
# to push both in parallel.
# =============================================================================

set -euo pipefail

die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $# -ge 1 && $# -le 2 ]] || die "usage: $0 <sparky|snoopy> [MODEL_DIRNAME]"
HOST="$1"
MODEL_DIRNAME="${2:-Qwen3-235B-A22B-Instruct-2507-FP8}"
case "$HOST" in sparky|snoopy) ;; *) die "unknown host '$HOST'" ;; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/model-cache/$MODEL_DIRNAME/"
REMOTE="${HOST}.flummoxed.net"
DEST_DIR="~/model-cache/$MODEL_DIRNAME/"

[[ -d "$SRC" ]] || die "source missing: $SRC"
[[ -f "$SRC/config.json" ]] || die "source incomplete (no config.json): $SRC"

printf '\033[1;34m[push]\033[0m %s -> %s:%s\n' "$SRC" "$REMOTE" "$DEST_DIR"

# Pre-create destination parent (macOS rsync lacks --mkpath).
ssh "$REMOTE" "mkdir -p ~/model-cache"

# -a                 archive
# --info=progress2   aggregate progress line
# --partial          keep partial files so ^C resumes
# --inplace          avoid the double-write-then-rename; OK since remote isn't
#                    serving these bytes concurrently
# --progress (per-file) instead of --info=progress2 (aggregate) because
# macOS ships rsync 2.6.9 which predates the latter.
rsync -a --progress --partial --inplace \
    "$SRC" "$REMOTE:$DEST_DIR"

printf '\033[1;32m[push]\033[0m done — next: sudo bash ~/Projects/DGX-Spark-Setup/%s/scripts/install-step2.sh %s (on %s)\n' "$HOST" "$MODEL_DIRNAME" "$HOST"
