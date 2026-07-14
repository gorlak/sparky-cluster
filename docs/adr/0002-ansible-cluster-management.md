# ADR-0002: Ansible for cluster management

**Date:** 2026-05-24
**Status:** Accepted

## Context

The cluster started with a set of per-step shell scripts (`install-step*.sh`)
driven by a root `Makefile`. As the number of services, nodes, and
configuration dimensions grew, the scripts became hard to reason about:
no idempotency, no diff before apply, no clear separation between the
control node and the worker node, no way to add a new service without
touching multiple files.

## Options considered

**A. Keep and extend the shell scripts**
Familiar, no new tooling. But not idempotent, order-sensitive, and every
two-node operation requires manual SSH coordination.

**B. Terraform / Pulumi**
Designed for infrastructure provisioning (VMs, DNS, cloud resources), not
for managing services on already-provisioned bare-metal. Would fight the
tool constantly for things like templating systemd units or syncing files.

**C. Ansible**
Agentless (snoopy needs only Python + SSH), idempotent, `--check --diff`
gives a dry-run before any change, and the inventory + role model maps
naturally to "head node does X, worker node does Y." Already installed
as apt's `ansible-core` on sparky.

**D. Nix / Salt / Chef / Puppet**
Heavier: require agents, daemons, or a richer ecosystem than the problem
warrants for a two-node homelab cluster.

## Decision

Ansible (option C).

## Consequences

- Source of truth is the git repo (`ansible/`). `make deploy` publishes it to
  `/opt/cluster/ansible` (the runtime copy the `deploy` user reads) and then
  runs `ansible-playbook` from there. Editing in the repo and deploying is
  the only supported workflow.
- `--check --diff` (`make check`) is available before every deploy.
- Ansible is agentless: snoopy needs no Ansible install. The control node
  (sparky) runs `ansible-core` from apt, so it tracks the system and stays
  on `secure_path`.
- Roles are the unit of extension: adding a new service means adding a role,
  not splicing into existing scripts.
- The publish step (`rsync repo → /opt/cluster/ansible`) is required because
  the `deploy` user can't read `geoff`'s `0750` home directory. This is a
  small extra step but makes the identity model clean.
