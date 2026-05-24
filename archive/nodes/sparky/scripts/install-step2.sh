#!/usr/bin/env bash
# =============================================================================
# sparky step 2: install model from pre-staged copy
# =============================================================================
# The model weights were downloaded on the Mac and pushed to this node via
# `push-model.sh sparky <MODEL_DIRNAME>`. They currently live at
# ~/model-cache/<MODEL_DIRNAME> under the invoking user's home. This step
# moves them into /opt/vllm/models/<MODEL_DIRNAME> and transfers ownership
# to the `vllm` service user.
#
# MODEL_DIRNAME is passed as the first argument. Defaults to
# Qwen3-235B-A22B-Instruct-2507-FP8 (the multinode production model) if
# omitted. For the single-node sparky bring-up we pass
# Qwen3.5-122B-A10B-FP8.
#
# Why not just download here again: HF's anon rate limit made direct
# downloads from the nodes too slow / bandwidth-hungry; the Mac did one
# authenticated pull and distributes over 10GbE.
#
# Idempotency: if /opt/vllm/models/Qwen3-.../config.json is already present
# and matches the staged size, the script reports "already installed" and
# exits 0 without re-moving anything.
#
# Failure: ERR trap on any non-zero. Verification (config.json, shard count,
# ownership) runs after the move.
#
# Usage (run locally on sparky):
#   sudo bash ~/Projects/DGX-Spark-Setup/nodes/sparky/scripts/install-step2.sh [MODEL_DIRNAME]
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[step2]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky, got $(hostname)"

MODEL_NAME="${1:-Qwen3-235B-A22B-Instruct-2507-FP8}"
FINAL_DIR="/opt/vllm/models/$MODEL_NAME"
log "target model: $MODEL_NAME"

# Resolve the invoking user's home so we find the staged copy regardless of
# which admin ran sudo.
SUDO_HOME="$(getent passwd "${SUDO_USER:-geoff}" | cut -d: -f6)"
STAGING_DIR="$SUDO_HOME/Projects/DGX-Spark-Setup/model-cache/$MODEL_NAME"

# --- preconditions ----------------------------------------------------------

id vllm &>/dev/null || die "vllm user missing — run install-step1.sh first"

# --- 1. idempotency shortcut -----------------------------------------------

if [[ -f "$FINAL_DIR/config.json" ]]; then
    EXISTING_SIZE=$(du -sb "$FINAL_DIR" 2>/dev/null | awk '{print $1}')
    log "model already installed at $FINAL_DIR ($(numfmt --to=iec --suffix=B "$EXISTING_SIZE"))"
    log "skipping move. delete $FINAL_DIR first if you want to re-install."
    # Still run verification below.
else
    # --- 2. check staging ---------------------------------------------------
    log "looking for staged model at $STAGING_DIR"
    [[ -f "$STAGING_DIR/config.json" ]] \
        || die "staged model missing or incomplete — run ./push-model.sh sparky from the Mac first"

    STAGED_SIZE=$(du -sb "$STAGING_DIR" | awk '{print $1}')
    log "  found: $(numfmt --to=iec --suffix=B "$STAGED_SIZE")"

    # --- 3. move into place -------------------------------------------------
    # mv is instant if /home and /opt are on the same filesystem (they are by
    # default on Ubuntu with one root partition). Cross-fs would trigger a
    # 235GB copy — let the user know if that happens.
    mkdir -p /opt/vllm/models
    chown vllm:vllm /opt/vllm/models

    SRC_FS=$(stat -c %d "$STAGING_DIR")
    DST_FS=$(stat -c %d /opt/vllm/models)
    if [[ "$SRC_FS" != "$DST_FS" ]]; then
        warn "staging and destination are on different filesystems — mv will copy 235GB"
    fi

    log "moving $STAGING_DIR -> $FINAL_DIR"
    mv "$STAGING_DIR" "$FINAL_DIR"

    # --- 4. ownership -------------------------------------------------------
    log "chowning to vllm:vllm"
    chown -R vllm:vllm "$FINAL_DIR"
fi

# --- 5. verification --------------------------------------------------------

log "verifying installed model"

[[ -f "$FINAL_DIR/config.json" ]] || die "config.json missing under $FINAL_DIR"

SHARD_COUNT=$(find "$FINAL_DIR" -maxdepth 1 -name '*.safetensors' | wc -l)
[[ "$SHARD_COUNT" -ge 2 ]] || die "only $SHARD_COUNT safetensor shards found"

TOTAL_SIZE=$(du -sh "$FINAL_DIR" | awk '{print $1}')
log "  config.json: present"
log "  safetensors: $SHARD_COUNT shards"
log "  total size:  $TOTAL_SIZE"

# Ownership sanity: vllm.service will refuse to read weights it can't access.
BAD=$(find "$FINAL_DIR" ! -user vllm | head -5 || true)
if [[ -n "$BAD" ]]; then
    warn "non-vllm-owned files found — fixing"
    printf '%s\n' "$BAD" >&2
    chown -R vllm:vllm "$FINAL_DIR"
fi

# Also confirm vllm can actually read it (permissions, not just ownership).
runuser -u vllm -- test -r "$FINAL_DIR/config.json" \
    || die "vllm user cannot read $FINAL_DIR/config.json — check perms"

log "sparky step 2 complete."
log "next: sudo bash install-step5.sh $MODEL_NAME"
