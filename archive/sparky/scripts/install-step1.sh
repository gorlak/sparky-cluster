#!/usr/bin/env bash
# =============================================================================
# sparky step 1: install vLLM + Ray
# =============================================================================
# Creates the `vllm` system user, a venv at /opt/vllm/venv, and pip-installs
# vllm + ray[default]. Mirrors snoopy/scripts/install.sh's install section —
# same package versions on both nodes is important: Ray and NCCL both expect
# matching Python/torch/vLLM across workers.
#
# What's NOT in this step (deliberately):
#   - NCCL config       -> step 4 (only needed when vllm.service starts)
#   - ray-head.service  -> step 3
#   - model download    -> step 2 (ordered after step 3 so snoopy is cluster-joined
#                           before we rsync weights to it)
#   - vllm.service      -> step 5
#   - Open WebUI        -> step 6
#   - OpenHands         -> step 7
#
# Idempotency: re-runnable — every action is guarded.
# Failure: `set -euo pipefail` + ERR trap aborts on any non-zero command.
#
# Usage (from the Mac):
#   ssh sparky 'sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/install-step1.sh'
# =============================================================================

set -euo pipefail

log()  { printf '\n\033[1;34m[step1]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "failed at line $LINENO (exit $?)"' ERR

[[ $EUID -eq 0 ]] || die "must be run as root (use sudo)"

# Hostname guard — this script is sparky-specific only in the sense that it
# belongs under sparky/. The work itself is host-agnostic, but we assert
# anyway so a misfire (e.g. running sparky's script on snoopy) is caught.
if [[ "$(hostname)" != "sparky" ]]; then
    die "expected to run on sparky, got hostname=$(hostname)"
fi

# --- 1. vllm system user ----------------------------------------------------

log "ensuring 'vllm' system user exists"
if id vllm &>/dev/null; then
    log "  already present (uid=$(id -u vllm))"
else
    # --system: system UID range, no login.
    # --no-create-home: /opt/vllm is its working dir, set up below.
    # --shell /usr/sbin/nologin: prevents interactive login via this account.
    useradd --system --no-create-home --shell /usr/sbin/nologin vllm
    log "  created vllm user (uid=$(id -u vllm))"
fi

# --- 2. /opt/vllm venv ------------------------------------------------------

log "preparing /opt/vllm venv"
mkdir -p /opt/vllm
chown vllm:vllm /opt/vllm

# Create venv as the vllm user so every file inside is owned correctly from
# the start — no recursive chown needed afterwards.
if [[ ! -x /opt/vllm/venv/bin/python ]]; then
    log "  creating venv at /opt/vllm/venv"
    runuser -u vllm -- python3 -m venv /opt/vllm/venv
else
    log "  venv already exists, reusing"
fi

log "upgrading pip tooling"
runuser -u vllm -- /opt/vllm/venv/bin/pip install --upgrade pip setuptools wheel

# --- 3. vllm + ray install --------------------------------------------------

log "installing vllm + ray[default] (this can take several minutes)"
# First try the stable channel. If that fails (CUDA 13/Blackwell wheel gap),
# retry with --pre for nightly/rc wheels. On snoopy this fallback was not
# needed — vllm 0.19.1 had a stable aarch64 wheel.
if ! runuser -u vllm -- /opt/vllm/venv/bin/pip install "vllm" "ray[default]"; then
    warn "stable vllm install failed — retrying with --pre (nightly/rc)"
    runuser -u vllm -- /opt/vllm/venv/bin/pip install --pre "vllm" "ray[default]"
fi

# --- verification -----------------------------------------------------------

log "verifying vllm and ray imports"
# `pip install` succeeding doesn't prove the package imports — native
# extensions can fail at load time (CUDA ABI, missing .so). This catches
# that before any later step tries to use them.
runuser -u vllm -- /opt/vllm/venv/bin/python -c \
    "import vllm, ray; print(f'vllm={vllm.__version__} ray={ray.__version__}')"

# Pin versions to a file so step 3 / later steps can cross-check that
# sparky and snoopy match. (Mismatched vllm/ray across nodes is a classic
# source of distributed-inference weirdness.)
runuser -u vllm -- /opt/vllm/venv/bin/python -c \
    "import vllm, ray; print(f'vllm={vllm.__version__}'); print(f'ray={ray.__version__}')" \
    > /opt/vllm/versions.txt
chown vllm:vllm /opt/vllm/versions.txt
log "  wrote /opt/vllm/versions.txt for later cross-node comparison"

log "sparky step 1 complete."
log "next: step 3 (ray-head.service)."
