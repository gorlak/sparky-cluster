#!/usr/bin/env bash
# Hypothesis: `systemctl daemon-reload` revokes nvidia device cgroup rules
# from running docker containers, even with --cgroupns=host.
#
# Test: bring ray-head up cleanly, confirm GPU works, then do daemon-reload
# and re-check. If GPU access is lost, daemon-reload is the gremlin.
#
# Usage:  sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/test-daemon-reload-gremlin.sh
set +e

log()  { printf '\n\033[1;34m[test]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky"

log "step 1: full teardown of ray-head"
systemctl stop ray-head 2>/dev/null
docker rm -f ray-head >/dev/null 2>&1
sleep 2

log "step 2: fresh start"
systemctl start ray-head
for _ in $(seq 1 20); do
    docker inspect -f '{{.State.Status}}' ray-head 2>/dev/null | grep -q running && break
    sleep 1
done

log "step 3: BEFORE daemon-reload — nvidia-smi inside ray-head"
echo "----"
docker exec ray-head nvidia-smi -L
RC_BEFORE=$?
echo "---- rc=$RC_BEFORE"

log "step 4: provoke — systemctl daemon-reload"
systemctl daemon-reload
sleep 2

log "step 5: AFTER daemon-reload — nvidia-smi inside ray-head"
echo "----"
docker exec ray-head nvidia-smi -L
RC_AFTER=$?
echo "---- rc=$RC_AFTER"

echo
log "=== RESULT ==="
if [[ $RC_BEFORE -eq 0 && $RC_AFTER -ne 0 ]]; then
    echo "  CONFIRMED: daemon-reload broke GPU access in the running container."
    echo "  Fix install-step5.sh to daemon-reload BEFORE (re)starting ray-head."
elif [[ $RC_BEFORE -eq 0 && $RC_AFTER -eq 0 ]]; then
    echo "  daemon-reload was NOT the cause — GPU still works after reload."
    echo "  Need to look elsewhere."
else
    echo "  GPU access was already broken BEFORE the reload (rc=$RC_BEFORE)."
    echo "  Different bug — investigate ray-head startup itself."
fi
