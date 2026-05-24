#!/usr/bin/env bash
# =============================================================================
# sync.sh — rsync a host folder from the Mac to its DGX Spark node
# =============================================================================
# Usage: ./sync.sh <sparky|snoopy>
#
# Mirrors ~/Projects/DGX-Spark-Setup/<host>/ on the Mac to the same path on
# the remote. Uses rsync -a --delete so the remote is an exact mirror; any
# files not in the Mac copy are removed from the remote. This keeps the
# breadcrumb on each node authoritative: what's on disk is what we pushed.
#
# Preserves executable bits (needed for scripts/*.sh) and times.
# =============================================================================

set -euo pipefail

die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $# -eq 1 ]] || die "usage: $0 <sparky|snoopy>"
HOST="$1"

case "$HOST" in
    sparky|snoopy) ;;
    *) die "unknown host '$HOST' (expected sparky or snoopy)" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DIR="$SCRIPT_DIR/$HOST/"
REMOTE="${HOST}.flummoxed.net"
REMOTE_DIR="~/Projects/DGX-Spark-Setup/$HOST/"

[[ -d "$LOCAL_DIR" ]] || die "$LOCAL_DIR not found"

printf '\033[1;34m[sync]\033[0m %s -> %s:%s\n' "$LOCAL_DIR" "$REMOTE" "$REMOTE_DIR"

# macOS ships rsync 2.6.9 which lacks --mkpath, so ensure the remote dir
# exists up front. (Harmless if it already exists.)
ssh "$REMOTE" "mkdir -p $REMOTE_DIR"

# -a: archive (perms, times, symlinks, recurse)
# --delete: remove remote files not present locally
rsync -a --delete "$LOCAL_DIR" "$REMOTE:$REMOTE_DIR"

printf '\033[1;32m[sync]\033[0m done\n'
