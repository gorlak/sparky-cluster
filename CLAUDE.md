# Claude Code — repository guide

This repository is agent-neutral. The full project documentation is in
`README.md` (imported below) — read it for the cluster architecture, the Ansible
deploy workflow, the identity model, and day-to-day operations.

@README.md

---

## Skills

Agent skills live in `skills/` (kept here rather than the default
`.claude/skills/` to keep the repo vendor-neutral, so they are NOT auto-registered
as slash commands). When a task matches one, read its `SKILL.md` and follow it:

- **`skills/model-discovery/SKILL.md`** — search HuggingFace (via the `hf` CLI)
  for new models / quantizations that fit this cluster's hardware. Use when asked
  to check for better models, newer quantizations, or what's new that fits.
- **`skills/version-discovery/SKILL.md`** — check everything versioned the cluster runs
  (container images, the panel's Python deps, vendored assets, the harness lock, model
  snapshots, the host baseline) for newer releases and **stage** the bumps. Use when
  asked what needs updating, before an upgrade round, or when a defect's *clears-when*
  waits on a version. It stages; applying is a separate change.
- **`skills/model-evaluation/SKILL.md`** — evaluating models: the pre-deployment
  fit checklist for one model (memory math, `config.json`, `vllm serve` flags),
  plus the **fleet sourcing sweep** that reviews the deployed models for worthwhile
  upgrades. Use before deploying a model, when estimating fit, or when asked to
  "assess upgrading our models".
- **`skills/model-bringup/SKILL.md`** — taking staged models from the inbox to serving:
  batch profiles into ONE deploy (activations serialize, deploys don't), probe the
  container before activating, and the checkpoint traps that each cost a real bring-up
  (the repo name lies about the quant; a `tokenizer.json` doesn't mean vLLM accepts it;
  a guessed parser name is a refusal to start). Use after discovery has staged weights.
- **`skills/documentation/SKILL.md`** — conventions for the `docs/` tree: model
  fact sheets (`docs/models/`), upgrade trackers (`docs/upgrades/`, named
  `container-…` / `profile-…`), **decision records** (`docs/adr/` — ADR
  lifecycle, immutability, when to write one), the **defect register**
  (`docs/defects.md`), and **update pathways** (`docs/updating.md`). Read when
  documenting a model, planning/tracking an upgrade, or writing/deciding on an ADR.
- **`skills/development/SKILL.md`** — development workflow conventions: git
  commit ownership, staging, ADR-in-same-commit. Read before making changes or
  preparing commits. **Geoff runs all git commits** — never run `git commit`
  unless explicitly asked.
- **`skills/operations/SKILL.md`** — drive and verify the cluster the **no-sudo** way:
  `./sparky.sh activate <profile>` (the unprivileged operation an agent *can* run —
  `deploy` is Geoff's, password-gated), `./sparky.sh status [--json]` (reads the control
  panel; exit code = health), and the activation-gate breadcrumb
  (`/opt/cluster/last-smoke.json`). Read when asked "is the cluster up/healthy", to
  switch which model serves, or to confirm an activation worked.

---

## Architecture Decision Records

Significant decisions shipped to this cluster are documented in `docs/adr/` (one
file per decision). When implementing something new, write the ADR alongside the
implementation and commit both together.

**Before an update** (bumping a container, adding or removing a model/role/profile),
read `docs/updating.md` — it lists every place that must move together and ends by
consulting `docs/defects.md`, the register of open defects the cluster carries (each
with a *clears-when* condition, so a bump knows what to re-test).
