# ADR-0001: Three-tier identity model (geoff / deploy / vllm)

**Date:** 2026-05-24
**Status:** Implemented

## Context

The cluster requires two distinct kinds of privilege: a human administrator who
edits config and triggers deploys, and an automation identity that can apply
those deploys without a password gate at every step. A third concern is model
weight ownership: the files are large, owned once at ingest, and should be
read-only to the serving runtime.

Running automation as root was ruled out immediately. Running it as `geoff`
leaks the human's credential into cron/scripts and makes audit unclear.

## Options considered

**A. Everything as `geoff` with NOPASSWD sudo**
Simple, but gives every script full root access under the human's identity.
Audit trail conflates human actions with automation.

**B. Single service account with full NOPASSWD sudo**
Cleaner separation, but one compromised deploy script = root everywhere.

**C. Three-tier: geoff / deploy / vllm**
- `geoff` — human, password-gated sudo, limited NOPASSWD scope (`systemctl`,
  `docker`, `journalctl`, `install`). Reviews, edits, triggers deploys.
- `deploy` — automation, `NOPASSWD: ALL` on both nodes, owns
  `/opt/cluster/ansible` and its SSH key. Ansible runs as this user. `geoff`
  enters this context via `sudo -u deploy …` (his password is the gate).
- `vllm` — service account, uid 996, no home, no shell. Owns model weights.
  The container runs as this user for filesystem access to weights.

## Decision

Three-tier (option C).

## Consequences

- `geoff`'s password is the single human gate into the automation context; no
  passwordless path to root for humans.
- Automation (`deploy`) has full sudo but is only entered via `sudo -u deploy`
  from `geoff` — not from any externally-accessible service account.
- Model weights are owned by `vllm` (not `deploy`, not `root`), preventing a
  misbehaving deploy script from modifying weights accidentally.
- `bootstrap-deploy.sh` must be run once as `geoff` to create the `deploy`
  user — this is the only step that can't be Ansible (it creates the user
  Ansible runs as).
- The `cluster` group (geoff + deploy) owns `/opt/cluster` (mode 2775 + default
  ACLs) so both identities can read and write the runtime copy.
