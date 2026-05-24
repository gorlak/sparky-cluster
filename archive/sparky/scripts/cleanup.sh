#!/usr/bin/env bash
# =============================================================================
# sparky cleanup: tear down the pip-venv vLLM/Ray stack
# =============================================================================
# We originally installed vLLM and Ray directly into /opt/vllm/venv via pip.
# That turned out to be a dead end on DGX Spark (GB10 / sm_121 / aarch64): the
# pypi torch wheel for aarch64 is CPU-only, so vllm's native _C extension
# couldn't find libcudart.so.12 at startup. We're switching to NVIDIA's
# prebuilt container image (`nvcr.io/nvidia/vllm:25.10-py3`) which ships
# matching torch/vllm/ray/flashinfer built for sm_121.
#
# This script tears down *only* the venv-era artifacts. It deliberately KEEPS:
#   - /opt/vllm/models/        (221GB of weights — reused by the container)
#   - /opt/vllm/nccl-env.conf  (reused as --env-file for the container)
#   - the `vllm` system user   (still owns the model files)
#
# Idempotent: safe to re-run. Missing services/files are not errors.
#
# Usage (run locally on sparky):
#   sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/cleanup.sh
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[cleanup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky, got $(hostname)"

# --- 1. stop + disable + remove systemd units -------------------------------

for unit in vllm.service ray-head.service; do
    if systemctl list-unit-files "$unit" &>/dev/null \
       && systemctl cat "$unit" &>/dev/null; then
        log "stopping + disabling $unit"
        systemctl stop "$unit" 2>/dev/null || true
        systemctl disable "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
    else
        log "$unit not installed — skipping"
    fi
done

log "reloading systemd"
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# --- 2. remove the pip venv -------------------------------------------------

if [[ -d /opt/vllm/venv ]]; then
    VENV_SIZE=$(du -sh /opt/vllm/venv 2>/dev/null | awk '{print $1}')
    log "removing /opt/vllm/venv ($VENV_SIZE)"
    rm -rf /opt/vllm/venv
else
    log "/opt/vllm/venv already gone"
fi

# Ray's temp dir is venv-era state — gone with the venv.
if [[ -d /opt/vllm/ray-tmp ]]; then
    log "removing /opt/vllm/ray-tmp"
    rm -rf /opt/vllm/ray-tmp
fi

# HF cache from the aborted download attempt, if any.
if [[ -d /opt/vllm/hf-cache ]]; then
    log "removing /opt/vllm/hf-cache"
    rm -rf /opt/vllm/hf-cache
fi

# Leftover versions.txt pin from the venv install.
rm -f /opt/vllm/versions.txt

# --- 3. report what's kept --------------------------------------------------

log "keeping the following for the Docker-based rebuild:"
if [[ -d /opt/vllm/models ]]; then
    MODELS_SIZE=$(du -sh /opt/vllm/models 2>/dev/null | awk '{print $1}')
    log "  /opt/vllm/models  ($MODELS_SIZE)"
else
    warn "  /opt/vllm/models  MISSING — you'll need to re-push weights"
fi
if [[ -f /opt/vllm/nccl-env.conf ]]; then
    log "  /opt/vllm/nccl-env.conf"
else
    warn "  /opt/vllm/nccl-env.conf MISSING — will be re-created by install-step4"
fi
if id vllm &>/dev/null; then
    log "  vllm user (uid=$(id -u vllm), owns /opt/vllm/models)"
fi

log "sparky cleanup complete. next: sudo bash install-step5.sh (container-based)"
