#!/usr/bin/env bash
# =============================================================================
# sparky step 5: vllm.service (containerized, MULTINODE TP=2, no Ray)
# =============================================================================
# Brings up vLLM serving Step-3.5-Flash-FP8 across both nodes via
# vLLM 0.19+ native multi-node (--nnodes/--node-rank/--master-addr).
# No Ray required.
#
# Two containers participate:
#   sparky:  vllm.service (this)     head (rank 0), serves API on :8000
#   snoopy:  vllm-worker.service     headless worker (rank 1)
#
# Preconditions:
#   - /opt/vllm/models/Step-3.5-Flash-FP8 exists on BOTH nodes
#   - /opt/vllm/nccl-env.conf exists on both nodes (install-step4.sh)
#   - nvcr.io/nvidia/vllm:26.04-py3 pulled on both nodes
#   - snoopy's vllm-worker.service unit installed (this script does it via ssh)
#
# What this does:
#   1. Installs vllm.service on sparky + daemon-reload
#   2. Installs vllm-worker.service on snoopy via ssh + daemon-reload there
#   3. Verifies image digests match on both nodes
#   4. Starts vllm-worker on snoopy (it will retry until the head is ready)
#   5. Starts vllm.service on sparky (head — both nodes rendezvous and load)
#   6. Polls http://localhost:8000/v1/models for up to 20 minutes
#   7. Runs a smoke chat completion
#
# Idempotency: re-runnable. `systemctl restart` replaces the container.
#
# Usage (run locally on sparky):
#   sudo bash ~/Projects/DGX-Spark-Setup/nodes/sparky/scripts/install-step5.sh
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[step5]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky, got $(hostname)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE="nvcr.io/nvidia/vllm:26.04-py3"
MODEL_NAME="Step-3.5-Flash-FP8"
SERVED_NAME="step-3.5-flash"
MODEL_DIR="/opt/vllm/models/$MODEL_NAME"
API="http://localhost:8000"
API_STARTUP_TIMEOUT=1200

SNOOPY_IP="10.0.200.13"
SNOOPY_USER="geoff"
# SSH as the invoking user (geoff), not root — root has no snoopy key/known_hosts.
SSH_AS_USER="sudo -u geoff"
SSH_KEY="/home/geoff/.ssh/id_ed25519_shared"
SSH_OPTS="-o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i $SSH_KEY"

ssh_check() {
    $SSH_AS_USER ssh $SSH_OPTS "$SNOOPY_USER@$SNOOPY_IP" "$@"
}

# --- preconditions ----------------------------------------------------------

log "checking preconditions"
[[ -f "$MODEL_DIR/config.json" ]] \
    || die "model missing at $MODEL_DIR — run install-step2.sh $MODEL_NAME first"
[[ -f /opt/vllm/nccl-env.conf ]] \
    || die "NCCL config missing — run install-step4.sh first"
command -v docker >/dev/null   || die "docker not installed"
docker info >/dev/null 2>&1    || die "docker daemon not responding"

log "verifying snoopy model is installed"
if ssh_check "test -f /opt/vllm/models/$MODEL_NAME/config.json" 2>/dev/null; then
    log "  snoopy model OK"
else
    warn "could not verify snoopy model via SSH (SSH agent unavailable under sudo)"
    warn "if model is missing on snoopy, vllm-worker will fail — verify with:"
    warn "  ssh snoopy 'ls /opt/vllm/models/$MODEL_NAME/config.json'"
fi

# Image digest cross-check — versions must be identical across nodes.
log "verifying image digest matches on both nodes"
SPARKY_DIGEST=$(docker inspect --format='{{.Id}}' "$IMAGE" 2>/dev/null) \
    || die "$IMAGE not found locally — sudo docker pull $IMAGE"
echo "$SPARKY_DIGEST" > /opt/vllm/image-digest.txt
if SNOOPY_DIGEST=$(ssh_check "sudo docker inspect --format='{{.Id}}' $IMAGE 2>/dev/null" 2>/dev/null); then
    if [[ "$SPARKY_DIGEST" != "$SNOOPY_DIGEST" ]]; then
        warn "sparky digest: $SPARKY_DIGEST"
        warn "snoopy digest: $SNOOPY_DIGEST"
        die "image digests differ — sudo docker pull $IMAGE on both nodes"
    fi
    log "  digests match: ${SPARKY_DIGEST:0:20}..."
else
    warn "could not check snoopy digest (docker needs sudo there); skipping"
fi

# --- 1. install vllm.service on sparky --------------------------------------

log "stopping any existing vllm container on sparky"
systemctl stop vllm.service 2>/dev/null || true
for _ in $(seq 1 30); do
    docker inspect vllm >/dev/null 2>&1 || break
    sleep 2
done
docker rm -f vllm >/dev/null 2>&1 || true

log "installing vllm.service"
install -o root -g root -m 0644 \
    "$REPO_ROOT/etc/systemd/system/vllm.service" \
    /etc/systemd/system/vllm.service

log "reloading systemd on sparky"
# NOTE: daemon-reload revokes nvidia device cgroup rules from running
# containers on THIS host only. snoopy is unaffected. No GPU containers
# are running on sparky at this point, so nothing to recover.
systemctl daemon-reload
systemctl enable vllm.service

# --- 2. snoopy: sync unit file, install, restart worker ---------------------

log "pushing vllm-worker.service to snoopy and restarting"
SNOOPY_UNIT_SRC="$REPO_ROOT/../snoopy/etc/systemd/system/vllm-worker.service"
[[ -f "$SNOOPY_UNIT_SRC" ]] \
    || die "vllm-worker.service not found at $SNOOPY_UNIT_SRC"

ssh_check "sudo systemctl stop vllm-worker.service 2>/dev/null; \
           sudo docker rm -f vllm-worker 2>/dev/null; true"

$SSH_AS_USER scp $SSH_OPTS \
    "$SNOOPY_UNIT_SRC" "$SNOOPY_USER@$SNOOPY_IP:/tmp/vllm-worker.service"

ssh_check "sudo install -o root -g root -m 0644 \
               /tmp/vllm-worker.service \
               /etc/systemd/system/vllm-worker.service && \
           sudo systemctl daemon-reload && \
           sudo systemctl enable vllm-worker.service && \
           sudo systemctl start vllm-worker.service"

log "  vllm-worker started on snoopy (will retry rendezvous until head is up)"

# --- 3. start vllm head on sparky -------------------------------------------

log "starting vllm.service on sparky (head node, rank 0)"
log "  both nodes will rendezvous at 10.0.200.12:29500 and begin loading ~50GB each"
systemctl start --no-block vllm.service

# --- 4. wait for API --------------------------------------------------------

log "polling $API/v1/models for up to ${API_STARTUP_TIMEOUT}s"
START=$(date +%s)
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))

    STATE=$(systemctl is-active vllm.service || true)
    if [[ "$STATE" == "failed" ]]; then
        warn "vllm.service entered failed state at ${ELAPSED}s"
        journalctl -u vllm.service -n 200 --no-pager >&2 || true
        die "vllm.service failed during startup"
    fi

    if MODEL_ID=$(curl -fsS "$API/v1/models" 2>/dev/null | \
            python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null); then
        if [[ "$MODEL_ID" == "$SERVED_NAME" ]]; then
            log "  API responded after ${ELAPSED}s (model=$MODEL_ID)"
            break
        fi
    fi

    if [[ $ELAPSED -ge $API_STARTUP_TIMEOUT ]]; then
        warn "API did not respond within ${API_STARTUP_TIMEOUT}s"
        journalctl -u vllm.service -n 200 --no-pager >&2 || true
        die "vllm.service did not come up in time"
    fi

    if (( ELAPSED % 60 == 0 )) && [[ $ELAPSED -gt 0 ]]; then
        log "  ${ELAPSED}s: still waiting (state=$STATE) — normal during weight load"
    fi
    sleep 5
done

# --- 5. smoke test ----------------------------------------------------------

log "GET /v1/models:"
curl -sS "$API/v1/models" | python3 -m json.tool

log "POST /v1/chat/completions (smoke test)"
RESP=$(curl -sS "$API/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\": \"$SERVED_NAME\",
        \"messages\": [{\"role\":\"user\",\"content\":\"Reply with exactly the word: ready\"}],
        \"max_tokens\": 8,
        \"temperature\": 0
    }")

printf '%s\n' "$RESP" | python3 -m json.tool

CONTENT=$(printf '%s\n' "$RESP" | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["choices"][0]["message"]["content"])' 2>/dev/null || true)
[[ -n "$CONTENT" ]] || die "chat completion returned empty content — see response above"
log "  completion content: $CONTENT"

# --- 6. summary -------------------------------------------------------------

log "vllm.service status:"
systemctl status vllm.service --no-pager --lines=0 || true

log "sparky step 5 complete — vLLM serving $MODEL_NAME (TP=2, no Ray) on $API"
log "next: install-step6.sh (Open WebUI), then install-step7.sh (OpenHands)"
