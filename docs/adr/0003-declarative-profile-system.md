# ADR-0003: Declarative YAML profile system

**Date:** 2026-05-24
**Status:** Implemented

## Context

Different workloads call for different cluster configurations: a single large
model tensor-parallelised across both nodes, two smaller models each on their
own node, or no serving at all (full hardware for dev). Switching between
these with per-step scripts required editing multiple files and manually
tearing down and recreating systemd units.

## Options considered

**A. Per-model scripts (prior approach)**
Each model configuration lives in its own shell script. Switching requires
running the right script in the right order on the right nodes. No diff, no
idempotency, no shared structure.

**B. Feature flags in a single config file**
One `all.yml` with booleans for every possible option. Gets unwieldy as the
number of combinations grows; a typo can produce an undefined intermediate
state.

**C. Separate YAML profile files under `ansible/profiles/`**
Each profile declares the full `serving_topology` (engines, models, nodes,
ports, `gpu_memory_utilization`, `max_model_len`) plus front-end toggles.
`make deploy PROFILE=<name>` applies one profile end-to-end; Ansible diffs
current vs desired and restarts only what changed.

## Decision

Separate profile files (option C).

## Consequences

- Adding a new model configuration is a new file in `profiles/`, not a
  surgery on existing config. Reviewing a profile in isolation is tractable.
- `make deploy PROFILE=step` is the single command to switch the cluster to
  any state. Ansible handles teardown, reconfiguration, and bring-up.
- Profile files are the unit of git history: a commit that adds a profile is
  self-contained and reviewable.
- The profile schema is documented in `docs/serving-topology.md`. All roles
  consume `serving_topology` from the active profile via Ansible vars.
- Trade-off: profiles are point-in-time snapshots. A config value shared
  across all profiles (e.g., `web_domain`) still lives in `group_vars/all.yml`;
  only per-deployment topology lives in the profile file.
