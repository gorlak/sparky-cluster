#!/usr/bin/env bash
# =============================================================================
# sparky step 4: NCCL env config
# =============================================================================
# Drops /opt/vllm/nccl-env.conf into place. The file is identical to snoopy's
# (same interface names, same GID, same GDR level) — both sides must agree or
# the NCCL bootstrap will silently fall back to a suboptimal path.
#
# This file is only consumed later, when vllm.service (step 5) sources it via
# EnvironmentFile=. Ray itself doesn't read NCCL env vars, so applying this
# now or in step 5 makes no observable difference — we drop it here so that
# by the time step 5 runs, the only new thing is the vllm unit itself.
#
# Idempotency: `install(1)` is atomic overwrite — safe to re-run.
#
# Usage (from the Mac):
#   ssh sparky 'sudo bash ~/Projects/DGX-Spark-Setup/nodes/sparky/scripts/install-step4.sh'
# =============================================================================

set -euo pipefail

log() { printf '\n\033[1;34m[step4]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"
[[ "$(hostname)" == "sparky" ]] || die "must run on sparky, got $(hostname)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log "installing /opt/vllm/nccl-env.conf"
install -o vllm -g vllm -m 0644 \
    "$REPO_ROOT/opt/vllm/nccl-env.conf" \
    /opt/vllm/nccl-env.conf

# --- verification -----------------------------------------------------------

log "cross-checking with snoopy's nccl-env.conf"
# Both nodes must have byte-identical NCCL env. Mismatched NCCL_IB_HCA or
# NCCL_IB_GID_INDEX is a classic source of multi-node hangs, so we compare
# checksums now rather than discover the problem during vllm.service startup.
SPARKY_SUM="$(sha256sum /opt/vllm/nccl-env.conf | awk '{print $1}')"
# We shell out via ssh as the invoking user (geoff) since root may not have
# a usable ssh key to snoopy. SUDO_USER is the login user who ran sudo.
if [[ -n "${SUDO_USER:-}" ]]; then
    SNOOPY_SUM="$(sudo -u "$SUDO_USER" ssh snoopy.flummoxed.net \
        'sha256sum /opt/vllm/nccl-env.conf' 2>/dev/null | awk '{print $1}' || true)"
else
    SNOOPY_SUM=""
fi

if [[ -z "$SNOOPY_SUM" ]]; then
    log "  could not reach snoopy to compare (skipping cross-check)"
elif [[ "$SPARKY_SUM" == "$SNOOPY_SUM" ]]; then
    log "  match: sha256=${SPARKY_SUM:0:12}…"
else
    die "NCCL config mismatch: sparky=$SPARKY_SUM snoopy=$SNOOPY_SUM"
fi

log "sparky step 4 complete."
log "next: step 2 (model download)."
