#!/usr/bin/env bash
# =============================================================================
# snoopy cleanup: tear down the pip-venv Ray worker stack
# =============================================================================
# Same motivation as sparky/scripts/cleanup.sh — the pypi aarch64 torch is
# CPU-only, so we're switching to NVIDIA's `nvcr.io/nvidia/vllm:25.10-py3`
# container. This script removes the ray-worker systemd unit and the
# /opt/vllm/venv. It keeps /opt/vllm/models/ (221GB of weights) and
# /opt/vllm/nccl-env.conf — both re-used by the container.
#
# Idempotent: safe to re-run.
#
# Usage (run locally on snoopy):
#   sudo bash ~/Projects/DGX-Spark-Setup/snoopy/scripts/cleanup.sh
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[cleanup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "snoopy" ]] || die "must run on snoopy, got $(hostname)"

# --- 1. stop + disable + remove systemd units -------------------------------

for unit in ray-worker.service; do
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

if [[ -d /opt/vllm/ray-tmp ]]; then
    log "removing /opt/vllm/ray-tmp"
    rm -rf /opt/vllm/ray-tmp
fi

rm -f /opt/vllm/versions.txt

# --- 3. report what's kept --------------------------------------------------

log "keeping the following for the Docker-based rebuild:"
if [[ -d /opt/vllm/models ]]; then
    MODELS_SIZE=$(du -sh /opt/vllm/models 2>/dev/null | awk '{print $1}')
    log "  /opt/vllm/models  ($MODELS_SIZE)"
else
    warn "  /opt/vllm/models  MISSING"
fi
if [[ -f /opt/vllm/nccl-env.conf ]]; then
    log "  /opt/vllm/nccl-env.conf"
else
    warn "  /opt/vllm/nccl-env.conf MISSING"
fi
if id vllm &>/dev/null; then
    log "  vllm user (uid=$(id -u vllm), owns /opt/vllm/models)"
fi

log "snoopy cleanup complete. snoopy will be re-enlisted after sparky is working solo."
