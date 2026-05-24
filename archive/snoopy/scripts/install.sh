#!/usr/bin/env bash
# =============================================================================
# snoopy one-shot install (containerized)
# =============================================================================
# Runs on snoopy as root. Sets up everything this worker needs so that, once
# sparky's ray-head is up, snoopy auto-joins the Ray cluster.
#
# What this does:
#   1. Pulls nvcr.io/nvidia/vllm:26.03.post1-py3 (must match sparky's image
#      digest — torch/ray/python ABI has to be identical across the cluster).
#   2. Installs ray-worker.service.
#   3. Enables + starts it. The unit retries every 10s until sparky's
#      ray-head at 10.0.200.12:6379 is reachable — boot order doesn't matter.
#
# Preconditions:
#   - cleanup.sh has been run (no legacy venv/ray-worker)
#   - /opt/vllm/nccl-env.conf is in place
#   - /opt/vllm/models/Qwen3-235B-A22B-Instruct-2507-FP8 has been rsynced
#     (run push-model.sh from the Mac, or download directly on snoopy)
#   - docker + nvidia-container-toolkit installed
#
# Idempotent: re-runnable.
#
# Usage (from the Mac):
#   ssh snoopy 'sudo bash ~/Projects/DGX-Spark-Setup/snoopy/scripts/install.sh'
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "snoopy" ]] || die "must run on snoopy, got $(hostname)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE="nvcr.io/nvidia/vllm:26.03.post1-py3"
MODEL_DIR="/opt/vllm/models/Qwen3-235B-A22B-Instruct-2507-FP8"

# --- preconditions ----------------------------------------------------------

log "checking preconditions"
[[ -f /opt/vllm/nccl-env.conf ]] || die "NCCL config missing at /opt/vllm/nccl-env.conf"
command -v docker >/dev/null    || die "docker not installed"
docker info >/dev/null 2>&1     || die "docker daemon not responding"

if [[ ! -f "$MODEL_DIR/config.json" ]]; then
    warn "model not found at $MODEL_DIR"
    warn "ray-worker can still start without it, but vllm.service on sparky"
    warn "will fail until snoopy has the same weights at the same path."
    warn "run push-model.sh from the Mac to rsync the weights."
fi

# GPU passthrough must work for ray-worker to advertise the GB10.
log "verifying docker --gpus all"
docker run --rm --gpus all "$IMAGE" nvidia-smi -L >/dev/null 2>&1 \
    || die "docker --gpus all failed — check nvidia-container-toolkit"

# --- 1. pull image ----------------------------------------------------------

log "pulling $IMAGE"
docker pull "$IMAGE"
IMAGE_DIGEST=$(docker inspect --format='{{.Id}}' "$IMAGE")
log "  image digest: $IMAGE_DIGEST"
mkdir -p /opt/vllm
echo "$IMAGE_DIGEST" > /opt/vllm/image-digest.txt
log "  (compare to sparky's /opt/vllm/image-digest.txt — must match)"

# --- 2. install ray-worker.service ------------------------------------------

log "installing ray-worker.service unit"
install -o root -g root -m 0644 \
    "$REPO_ROOT/etc/systemd/system/ray-worker.service" \
    /etc/systemd/system/ray-worker.service

log "reloading systemd and enabling ray-worker"
systemctl daemon-reload
systemctl enable ray-worker.service

# Start it. Failing immediately (head not yet up) is fine — Restart=on-failure
# will keep retrying until sparky comes online.
log "starting ray-worker.service"
systemctl start ray-worker.service || true

# --- verification -----------------------------------------------------------

systemctl is-enabled --quiet ray-worker.service \
    || die "ray-worker.service is not enabled"

STATE="$(systemctl is-active ray-worker.service || true)"
log "  ray-worker.service: enabled=yes, active=$STATE"
[[ "$STATE" == "active" ]] || \
    log "  (not-yet-active is expected if sparky's ray-head isn't up)"

log "snoopy install complete."
log "next: on the Mac, run sparky's install-step3.sh — it will verify both"
log "      nodes joined the cluster via 'docker exec ray-head ray status'."
