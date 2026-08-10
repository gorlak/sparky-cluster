# Claude Code — repository guide

This repository is agent-neutral. The full project documentation is in
`README.md` (imported below) — read it for the cluster architecture, the Ansible
deploy workflow, the identity model, and day-to-day operations.

## How this file works

**This file is an index, not a summary.** Each entry below states a skill's *topic*,
its *scope*, and *when to read it* — never what it says. A rule that appears here and
in a skill is a rule that will drift in one of them, and the copy is always the one
someone reads.

So: if you need the convention, open the file that owns it. If you are deciding
*whether* you need it, the entry below is enough. The same applies to `README.md`,
which describes the system and defers to `skills/` and `docs/` for how to work on it.

@README.md

---

## Skills

Agent skills live in `skills/` (kept here rather than the default
`.claude/skills/` to keep the repo vendor-neutral, so they are NOT auto-registered
as slash commands). Nothing activates them automatically — read the `SKILL.md`
when its trigger below matches what you are doing.

- **`skills/model-discovery/SKILL.md`** — searching HuggingFace (via the `hf` CLI) for
  models and quantizations that fit this cluster's hardware. Use when asked to check for
  better models, newer quantizations, or what's new that fits.
- **`skills/version-discovery/SKILL.md`** — checking everything versioned the cluster
  runs (container images, the panel's Python deps, vendored assets, the harness lock,
  model snapshots, the host baseline) for newer releases, and staging the bumps. Use when
  asked what needs updating, before an upgrade round, or when a defect's *clears-when*
  waits on a version. It stages; applying is a separate change.
- **`skills/model-evaluation/SKILL.md`** — the pre-deployment fit checklist for one model
  (memory math, `config.json`, serve flags), plus the fleet sourcing sweep that reviews
  deployed models for worthwhile upgrades. Read **before** deploying a model, when
  estimating fit, or when asked to assess upgrading the fleet.
- **`skills/model-bringup/SKILL.md`** — taking staged models from the inbox to serving,
  and the checkpoint traps that each cost a real bring-up. Read **before** writing a
  profile for newly staged weights — it is a pre-flight, and its value is in what it
  stops you assuming.
- **`skills/documentation/SKILL.md`** — which artifact a piece of writing belongs in:
  model fact sheets, upgrade trackers, decision records (`docs/adr/`), the defect
  register, the tombstone register, update pathways. Read when documenting a model,
  planning an upgrade, or writing or accepting an ADR — the boundaries between these are
  enforced, and guessing wrong puts a verdict where nobody will look for it.
- **`skills/development/SKILL.md`** — who stages and who commits, what belongs in one
  commit, message style, ADR-in-same-commit. **Read it before touching git at all** —
  before `add`, `commit`, `push`, or writing a commit message — because it constrains
  what you may run, not just how.
- **`skills/operations/SKILL.md`** — driving and verifying the cluster the **no-sudo**
  way. Read when asked whether the cluster is up or healthy, to switch which model
  serves, to confirm an activation worked, or before running anything long against it.

---

## Architecture Decision Records

Significant decisions live in `docs/adr/`, one file per decision. Their lifecycle,
immutability, and the question of when something deserves an ADR at all are owned by
[`skills/documentation/SKILL.md`](skills/documentation/SKILL.md).

**Before an update** — bumping a container, adding or removing a model, role or profile —
read [`docs/updating.md`](docs/updating.md). It is the change-fan-out checklist, and it
ends by consulting [`docs/defects.md`](docs/defects.md), the register of open defects the
cluster carries.
