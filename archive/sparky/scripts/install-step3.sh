#!/usr/bin/env bash
# =============================================================================
# sparky step 3: ray-head service (containerized)
# =============================================================================
# Installs and starts ray-head.service. After this runs and snoopy's
# install.sh has been deployed, the cluster should report 2 nodes via
# `docker exec ray-head ray status`. This script verifies that.
#
# Preconditions:
#   - cleanup.sh has run (no legacy ray-head from the venv era)
#   - install-step5.sh's image pull has happened OR docker pull succeeds here
#   - snoopy's install.sh has been run (otherwise we'll only see 1 node)
#
# Usage (run locally on sparky):
#   sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/install-step3.sh
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[step3]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky, got $(hostname)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE="nvcr.io/nvidia/vllm:26.03.post1-py3"
NODE_JOIN_TIMEOUT=120     # seconds to wait for snoopy to join

# --- preconditions ----------------------------------------------------------

log "checking preconditions"
[[ -f /opt/vllm/nccl-env.conf ]] || die "NCCL config missing — run install-step4.sh first"
command -v docker >/dev/null || die "docker not installed"

# --- 1. pull image (idempotent) --------------------------------------------

log "ensuring $IMAGE is local"
docker pull "$IMAGE"
docker inspect --format='{{.Id}}' "$IMAGE" > /opt/vllm/image-digest.txt
log "  image digest: $(cat /opt/vllm/image-digest.txt)"

# --- 2. install + start unit -----------------------------------------------

log "installing ray-head.service unit"
install -o root -g root -m 0644 \
    "$REPO_ROOT/etc/systemd/system/ray-head.service" \
    /etc/systemd/system/ray-head.service

systemctl daemon-reload
systemctl enable ray-head.service

log "(re)starting ray-head.service"
systemctl restart --no-block ray-head.service

# --- 3. wait for the head to be up -----------------------------------------

log "waiting for ray-head container to be running"
for _ in $(seq 1 30); do
    if docker inspect -f '{{.State.Status}}' ray-head 2>/dev/null | grep -q running; then
        log "  ray-head container running"
        break
    fi
    sleep 2
done
docker inspect -f '{{.State.Status}}' ray-head 2>/dev/null | grep -q running \
    || die "ray-head container did not start within 60s"

# Give Ray's GCS a couple seconds to bind to 6379
sleep 3

# --- 4. check that snoopy has joined ---------------------------------------

log "polling for 2 nodes (sparky + snoopy) for up to ${NODE_JOIN_TIMEOUT}s"
START=$(date +%s)
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))

    # `ray status` from inside the head container. Active node count is the
    # number of `node_<id>` lines under "Active:".
    RAY_NODES=$(docker exec ray-head ray status 2>/dev/null | \
        awk '/^Active:/{a=1;next} /^Pending:/||/^Recent failures:/{a=0} a&&/node_/{c++} END{print c+0}')

    if [[ "$RAY_NODES" -ge 2 ]]; then
        log "  cluster has $RAY_NODES nodes after ${ELAPSED}s"
        break
    fi

    if [[ $ELAPSED -ge $NODE_JOIN_TIMEOUT ]]; then
        warn "only $RAY_NODES node(s) joined within ${NODE_JOIN_TIMEOUT}s"
        warn "is snoopy's install.sh deployed and ray-worker.service running?"
        warn "current ray status:"
        docker exec ray-head ray status >&2 || true
        die "snoopy did not join the cluster"
    fi

    if (( ELAPSED % 15 == 0 )) && [[ $ELAPSED -gt 0 ]]; then
        log "  ${ELAPSED}s: $RAY_NODES node(s) so far"
    fi
    sleep 3
done

# --- 5. summary ------------------------------------------------------------

log "ray status:"
docker exec ray-head ray status

log "sparky step 3 complete — Ray cluster is up with both nodes."
log "next: sudo bash install-step5.sh (brings up vllm.service for 235B / TP=2)"
