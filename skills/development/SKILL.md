---
name: development
description: Development workflow conventions for this repository. Read before making changes, committing, or pushing. Covers git commit ownership, staging, and collaboration style.
---

## Git Commits

**Geoff runs all git commits.** Never run `git commit` or `git push` unless
explicitly asked.

When work is ready to commit:
1. Stage the relevant files: `git add <specific files>` — prefer named files
   over `git add -A` to avoid accidentally including model weights, `.env`
   files, or other untracked noise.
2. Show a suggested commit message for Geoff to use or edit.
3. Stop. Geoff runs the commit.

If Geoff explicitly asks you to commit, do so. Otherwise, prepare and stage,
then hand off.

## Suggested Commit Message Format

Follow the existing commit history style (concise imperative subject line,
no trailing period). Check `git log --oneline -10` before writing a message
to match the tone and granularity of recent commits.

Example handoff:
```
Files staged. Suggested message:

    Add benchmark regiment: multiturn quality check, SQLite storage, weekly timer
```

## What Belongs in a Commit

Each commit should correspond to one shipped item — a new feature, a new
profile, a new ADR, a bug fix, a role addition. Don't bundle unrelated
changes. If an ADR is written for a decision, commit the ADR in the same
commit as the implementation it documents.

## ADRs

Every significant architectural or operational decision shipped to the cluster
gets an ADR in `docs/adr/` — see [[documentation]] for when and how to write one.
Write the ADR alongside the implementation and commit both together.

## No Cleanup Commits

Don't create "cleanup", "fix typo", or "update comments" commits speculatively.
If a cleanup is needed as part of a real change, include it in that commit.
