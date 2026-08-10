---
name: development
description: Development workflow conventions for this repository. Read before making changes, committing, or pushing. Covers git commit ownership, staging, and collaboration style.
---

## Git: the user stages AND commits

**The user runs `git add`, `git commit` and `git push`.** Do not run any of them
unless explicitly asked. Leave changes in the working tree; they decide what
enters the index.

This changed on 2026-08-11. It used to be "prepare and stage, then hand off",
and the reason for tightening it is that **staging is where the review
happens**. An agent that stages has already made the include/exclude call, and
`git add -A` in particular sweeps up whatever else the session left lying
around. Handing over a pre-staged index invites approving a set nobody read.

When work is ready:
1. **Leave it unstaged.** Say what changed and why.
2. Suggest a commit message if asked.
3. Stop.

### When asked for a commit message, read the index first

**Do not describe the work you remember doing — describe what is actually
staged.** Those differ more often than they should: the user stages selectively,
a session may have touched files that were deliberately left out, and a long
session's memory of "what we did" drifts from the diff.

```bash
git status --short          # what is staged vs merely modified
git diff --cached --stat    # the shape of it
git diff --cached           # read it when the message makes claims
```

State the mismatch plainly if there is one — "the index has 12 files; the
retirement docs we discussed are not among them" — rather than writing a
message for work that is not there. A commit message that overstates its own
diff is worse than a terse one, because it is the record everyone trusts later.

## Suggested Commit Message Format

Follow the existing commit history style (concise imperative subject line, no
trailing period; clauses separated by semicolons). Check `git log --oneline -10`
before writing one, to match the tone and granularity of recent commits.

Bodies are rare in this log and should stay rare — but a large or
consequence-heavy commit earns one when the **findings** are not recoverable
from the diff (a measurement that changed a decision, a defect root-caused, a
claim in the docs corrected).

Example handoff:
```
Staged: 4 files (sparky/bench.py, sparky/store.py, tests/test_bench.py, docs/adr/0012-*.md).
Suggested message:

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
