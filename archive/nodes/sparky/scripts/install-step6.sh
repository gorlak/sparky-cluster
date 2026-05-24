#!/usr/bin/env bash
# =============================================================================
# sparky step 6: Open WebUI (Docker Compose)
# =============================================================================
# Deploys Open WebUI as a Docker Compose service on sparky, pointing at the
# vLLM API on localhost:8000. Accessible at http://sparky (port 80).
#
# Auth is disabled (WEBUI_AUTH=false) — LAN-only access assumed.
# To re-enable auth: set WEBUI_AUTH=true in the compose file and
# `docker compose up -d` from /opt/open-webui/.
#
# Preconditions:
#   - vllm.service is up and serving on :8000
#   - docker + docker compose available
#
# Idempotency: re-runnable. `docker compose up -d` is a no-op if already up.
#
# Usage (run locally on sparky):
#   sudo bash ~/Projects/DGX-Spark-Setup/nodes/sparky/scripts/install-step6.sh
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[step6]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky, got $(hostname)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_SRC="$REPO_ROOT/opt/open-webui/docker-compose.yml"
INSTALL_DIR="/opt/open-webui"

# --- preconditions ----------------------------------------------------------

log "checking preconditions"
command -v docker >/dev/null            || die "docker not installed"
docker compose version >/dev/null 2>&1 || die "docker compose plugin not installed"
[[ -f "$COMPOSE_SRC" ]] \
    || die "compose file missing at $COMPOSE_SRC"

# vLLM should be up, but warn rather than block — WebUI will just show an error
# banner until vLLM is ready, and recovers automatically once it is.
if ! curl -fsS http://localhost:8000/v1/models >/dev/null 2>&1; then
    printf '\033[1;33m[warn]\033[0m vLLM is not responding on :8000 — Open WebUI will connect once it comes up\n' >&2
fi

# --- install ----------------------------------------------------------------

log "creating $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"

log "installing docker-compose.yml"
install -o root -g root -m 0644 "$COMPOSE_SRC" "$INSTALL_DIR/docker-compose.yml"

# --- pull + start -----------------------------------------------------------

log "pulling Open WebUI image"
docker compose -f "$INSTALL_DIR/docker-compose.yml" pull

log "starting Open WebUI"
docker compose -f "$INSTALL_DIR/docker-compose.yml" up -d

# --- verify -----------------------------------------------------------------

log "waiting for Open WebUI to be reachable on :80"
for i in $(seq 1 30); do
    curl -fsS http://localhost:80 >/dev/null 2>&1 && break
    sleep 2
done
curl -fsS http://localhost:80 >/dev/null 2>&1 \
    || die "Open WebUI did not come up on :80 after 60s — check: docker logs open-webui"

log "sparky step 6 complete"
log "  Open WebUI: http://sparky  (or http://$(hostname -I | awk '{print $1}'))"
log "  Data:       $INSTALL_DIR/data"
log "  Logs:       docker logs -f open-webui"
log "  Update:     cd $INSTALL_DIR && docker compose pull && docker compose up -d"
