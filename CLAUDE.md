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
- **`skills/model-evaluation/SKILL.md`** — evaluating models: the pre-deployment
  fit checklist for one model (memory math, `config.json`, `vllm serve` flags),
  plus the **fleet sourcing sweep** that reviews the deployed models for worthwhile
  upgrades. Use before deploying a model, when estimating fit, or when asked to
  "assess upgrading our models".
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

---

## Architecture Decision Records

Significant decisions shipped to this cluster are documented in `docs/adr/` (one
file per decision). When implementing something new, write the ADR alongside the
implementation and commit both together.

**Before an update** (bumping a container, adding a model/role/profile), read
`docs/updating.md` — it lists every place that must move together and ends by
consulting `docs/defects.md`, the register of open defects the cluster carries (each
with a *clears-when* condition, so a bump knows what to re-test).
