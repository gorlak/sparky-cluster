#!/usr/bin/env bash
# =============================================================================
# bootstrap-deploy.sh — create the `deploy` automation identity (ONE-TIME)
# =============================================================================
# This is the only step that cannot be Ansible: it creates the user that
# Ansible itself runs as, plus the shared /opt/cluster project dir.
#
# Run it ONCE, by geoff, from sparky, AFTER the ansible project is complete:
#
#     bash ~/Projects/DGX-Spark-Setup/ansible/bootstrap-deploy.sh
#
# Do NOT pre-sudo it. It escalates per-step with `sudo` locally (you'll be
# prompted for your password) and reaches snoopy over your existing SSH
# (you'll be prompted for snoopy's sudo password too). Both are expected.
#
# Identity model (see also the conversation that produced this):
#   geoff   — human admin, password sudo. Stays "just a user".
#   deploy  — automation identity. NOPASSWD:ALL, owns the SSH keys + project.
#             geoff enters this context via `sudo -u deploy …` (password-gated);
#             a future dashboard runs as a systemd service with User=deploy.
#   cluster — shared group so geoff + deploy can both edit /opt/cluster.
#
# What it sets up on BOTH nodes:
#   - user `deploy` (home + /bin/bash)
#   - /etc/sudoers.d/deploy  ->  deploy ALL=(ALL) NOPASSWD: ALL
#   - deploy in the `docker` group
# On sparky only (control node):
#   - `cluster` group; deploy + geoff added to it
#   - /opt/cluster and /opt/cluster/model-cache  (deploy:cluster, 2775,
#     default ACLs g:cluster:rwx so both identities can edit in place)
#   - deploy ed25519 keypair (/home/deploy/.ssh/id_ed25519)
#   - that pubkey authorized for deploy@snoopy (control -> worker hop)
#   - the ansible project seeded into /opt/cluster/ansible
#
# Idempotent: re-runnable.
# =============================================================================
set -euo pipefail

DEPLOY_USER=deploy
CLUSTER_GROUP=cluster
ADMIN_USER=geoff
CLUSTER_DIR=/opt/cluster
SNOOPY=geoff@10.0.200.13
SSH_KEY=/home/geoff/.ssh/id_ed25519_shared
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\n\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(hostname)" == "sparky" ]] || die "run this on sparky (control node), got $(hostname)"
[[ "$(whoami)"  == "$ADMIN_USER" ]] || die "run this as $ADMIN_USER (not root), got $(whoami)"

# Shared, idempotent node-local setup. Fed to `bash -s` locally (sudo) and on
# snoopy (ssh + sudo). POSIX-plain so it runs identically in both places.
read -r -d '' NODE_SETUP <<'EOF' || true
set -eu
DEPLOY_USER=deploy

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
    echo "  created user $DEPLOY_USER"
else
    echo "  user $DEPLOY_USER already exists"
fi

SUDOERS=/etc/sudoers.d/deploy
if [ ! -f "$SUDOERS" ]; then
    printf 'deploy ALL=(ALL) NOPASSWD: ALL\n' > "$SUDOERS"
    chmod 0440 "$SUDOERS"
    visudo -cf "$SUDOERS"
    echo "  installed $SUDOERS"
else
    echo "  $SUDOERS already present"
fi

if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$DEPLOY_USER"
    echo "  $DEPLOY_USER in docker group"
fi

install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0700 "/home/$DEPLOY_USER/.ssh"
EOF

# --- deploy identity on both nodes -----------------------------------------
log "configuring deploy on sparky (you'll be asked for your sudo password)"
sudo bash -s <<<"$NODE_SETUP"

log "configuring deploy on snoopy (you'll be asked for snoopy's sudo password)"
# Transfer the script first (plain stdin pipe), THEN run it under sudo over a
# real tty. `ssh -t` can't allocate a PTY when stdin is a here-string, and
# without a PTY remote sudo has no terminal to prompt for the password on.
SNOOPY_TMP="/tmp/bootstrap-deploy-node.$$.sh"
printf '%s\n' "$NODE_SETUP" | ssh -i "$SSH_KEY" "$SNOOPY" "cat > '$SNOOPY_TMP'"
ssh -t -i "$SSH_KEY" "$SNOOPY" "sudo bash '$SNOOPY_TMP'; rm -f '$SNOOPY_TMP'"

# --- sparky-only: cluster group + /opt/cluster -----------------------------
log "creating cluster group and $CLUSTER_DIR on sparky"
sudo bash -s <<EOF
set -eu
getent group "$CLUSTER_GROUP" >/dev/null 2>&1 || groupadd "$CLUSTER_GROUP"
usermod -aG "$CLUSTER_GROUP" "$DEPLOY_USER"
usermod -aG "$CLUSTER_GROUP" "$ADMIN_USER"
install -d -o "$DEPLOY_USER" -g "$CLUSTER_GROUP" -m 2775 "$CLUSTER_DIR" "$CLUSTER_DIR/model-cache"
if command -v setfacl >/dev/null 2>&1; then
    setfacl -R  -m g:"$CLUSTER_GROUP":rwx "$CLUSTER_DIR"
    setfacl -R -d -m g:"$CLUSTER_GROUP":rwx "$CLUSTER_DIR"
    echo "  default ACLs set (g:$CLUSTER_GROUP:rwx)"
else
    echo "  WARNING: setfacl not found — new files may not be group-writable"
fi
EOF

# --- deploy SSH keypair on sparky ------------------------------------------
log "ensuring deploy has an SSH keypair on sparky"
if sudo test ! -f /home/deploy/.ssh/id_ed25519; then
    sudo -u deploy ssh-keygen -t ed25519 -N '' \
        -f /home/deploy/.ssh/id_ed25519 -C 'deploy@sparky'
    log "  generated /home/deploy/.ssh/id_ed25519"
else
    log "  keypair already present"
fi
DEPLOY_PUBKEY="$(sudo cat /home/deploy/.ssh/id_ed25519.pub)"

# --- authorize deploy@sparky -> deploy@snoopy ------------------------------
log "authorizing deploy@sparky's key on deploy@snoopy"
AUTH_TMP="/tmp/bootstrap-deploy-auth.$$.sh"
cat <<EOF | ssh -i "$SSH_KEY" "$SNOOPY" "cat > '$AUTH_TMP'"
set -eu
install -d -o deploy -g deploy -m 0700 /home/deploy/.ssh
AUTH=/home/deploy/.ssh/authorized_keys
touch "\$AUTH"; chown deploy:deploy "\$AUTH"; chmod 0600 "\$AUTH"
grep -qxF "$DEPLOY_PUBKEY" "\$AUTH" || echo "$DEPLOY_PUBKEY" >> "\$AUTH"
EOF
ssh -t -i "$SSH_KEY" "$SNOOPY" "sudo bash '$AUTH_TMP'; rm -f '$AUTH_TMP'"

# --- verify the control->worker hop as deploy ------------------------------
log "verifying deploy@sparky can reach deploy@snoopy without a password"
if sudo -u deploy ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -i /home/deploy/.ssh/id_ed25519 deploy@10.0.200.13 'echo ok' 2>/dev/null | grep -qx ok; then
    log "  OK — deploy@sparky -> deploy@snoopy works"
else
    die "deploy SSH hop failed — check /home/deploy/.ssh on both nodes"
fi

# --- initial publish of the ansible project into /opt/cluster --------------
# The repo is the source of truth; this is the first publish of it to the live
# runtime location. Thereafter `make deploy` re-publishes (rsync) automatically.
log "publishing ansible project to $CLUSTER_DIR/ansible"
sudo rsync -a --chown="$DEPLOY_USER:$CLUSTER_GROUP" "$SCRIPT_DIR"/ "$CLUSTER_DIR/ansible/"

log "bootstrap complete."
log "  • Source of truth: the git repo (~/Projects/DGX-Spark-Setup/ansible)."
log "  • $CLUSTER_DIR/ansible is the published runtime copy deploy runs from."
log "  • Log out and back in (or run: newgrp $CLUSTER_GROUP) to pick up the"
log "    '$CLUSTER_GROUP' group so 'make deploy' can publish to $CLUSTER_DIR."
log "  • Then: cd ~/Projects/DGX-Spark-Setup/ansible && make deploy"
