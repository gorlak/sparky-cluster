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

- **`skills/model-scout/SKILL.md`** — search HuggingFace for new models /
  quantizations that fit this cluster's hardware. Use when asked to check for
  better models, newer quantizations, or what's new that fits.
- **`skills/model-evaluation/SKILL.md`** — pre-deployment checklist for a new
  model (memory-fit estimate, writing `vllm serve` flags). Use before deploying
  a new model or estimating whether one fits.
- **`skills/dev-workflow/SKILL.md`** — development workflow conventions: git
  commit ownership, staging, ADR requirements. Read before making changes or
  preparing commits. **Geoff runs all git commits** — never run `git commit`
  unless explicitly asked.
- **`skills/adr/SKILL.md`** — when to write an ADR vs. update documentation,
  the status lifecycle (Proposed → Accepted → Implemented → Superseded), and
  the immutability rule for Implemented ADRs. Read before touching any file in
  `docs/adr/` or before deciding whether a new decision needs one.

---

## Architecture Decision Records

Significant decisions shipped to this cluster are documented in `docs/adr/`. See
`docs/adr/README.md` for the index. When implementing something new, write the ADR
alongside the implementation and commit both together.
