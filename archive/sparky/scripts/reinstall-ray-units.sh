#!/usr/bin/env bash
# Re-installs ray-head.service and vllm.service from the synced repo,
# reloads systemd, restarts ray-head, and verifies the bind mount took.
#
# Run AFTER `./sync.sh sparky` from the Mac.
# Then on snoopy:  sudo systemctl restart ray-worker
# Then re-run install-step5.sh on sparky.
#
# Usage:  sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/reinstall-ray-units.sh
set -euo pipefail

log()  { printf '\n\033[1;34m[reinstall]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log "installing units from $REPO_ROOT"
install -m 0644 "$REPO_ROOT/etc/systemd/system/ray-head.service" /etc/systemd/system/
install -m 0644 "$REPO_ROOT/etc/systemd/system/vllm.service"     /etc/systemd/system/

log "verifying mount lines are present"
grep -q 'ray-tmp:/tmp/ray' /etc/systemd/system/ray-head.service \
    || die "ray-head.service is missing the ray-tmp bind mount — sync didn't reach sparky"
grep -q 'ray-tmp:/tmp/ray' /etc/systemd/system/vllm.service \
    || die "vllm.service is missing the ray-tmp bind mount"
log "  ok"

log "daemon-reload + restart ray-head"
systemctl daemon-reload
systemctl stop vllm.service 2>/dev/null || true
systemctl restart ray-head.service

log "waiting for ray-head container to be running"
for _ in $(seq 1 30); do
    docker inspect -f '{{.State.Status}}' ray-head 2>/dev/null | grep -q running && break
    sleep 1
done
docker inspect -f '{{.State.Status}}' ray-head 2>/dev/null | grep -q running \
    || die "ray-head container did not start"

log "verifying /tmp/ray bind mount inside ray-head"
docker inspect ray-head --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' | \
    grep -q '/opt/vllm/ray-tmp -> /tmp/ray' \
    || die "bind mount not visible in running container"
log "  ok"

log "current ray status:"
sleep 5
docker exec ray-head ray status 2>&1 || true

cat <<EOF

NEXT STEPS:
  1. on snoopy:   sudo systemctl restart ray-worker
  2. wait ~15s, then on sparky: sudo docker exec ray-head ray status
     (expect 2 GPUs total — '0.0/2.0 GPU' under Resources)
  3. on sparky:   sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/install-step5.sh
EOF
