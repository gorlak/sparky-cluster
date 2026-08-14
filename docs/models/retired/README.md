# Retirement reports — the engineering, kept; the verdicts, elsewhere

A model or shape leaving the fleet should stop being *planned for*; it should not stop being
*known about*.

**A retirement report carries both the reasoning and the configuration.** The profile YAML is
embedded here in a fenced block rather than kept as a live-looking `.yml`, because a file
nothing parses, sitting in the directory that *is* the allowlist, invites exactly one
question — "why isn't this running?" — and answers it only if you read to the end. The
config was also the smaller half: the nine retired profiles were 603 comment lines against
191 of YAML.

This supersedes `docs/models/retired/`, whose README argued for keeping the files rather
than citing a commit hash. That argument was against *losing* the config — *"recovering that
from git log requires knowing it exists and which commit removed it. In practice nobody
looks"* — and embedding it in the report a reader is already holding satisfies it better than
a parallel directory did.

## Why this exists

On 2026-08-12 these sheets were **deleted** when their models were retired, on the
reasoning that a living doc which plans for a dead model is how a future sweep talks itself
back into one. The first half of that is right. The second half — "read it with
`git show <hash>:<path>` if you need it" — contradicts a decision this repo had already
made one directory over:

> *"Recovering that from `git log` requires knowing it exists and which commit removed it.
> In practice nobody looks, and the next person re-derives it — which for a parser name
> costs a deploy, and for the memory math costs a bring-up."*
> — [`docs/models/retired/README.md`](../../../docs/models/retired/README.md)

That argument does not care whether the artifact is a `.yml` or a `.md`. The sheets carried
20–39 lines each of memory math, quantization footprints and SM12.1 workarounds;
`mistral-medium-3.5.md` carried a section on the dense-model KV trade-off that turned out to
prefigure the bandwidth rule in [model-discovery](../../../skills/model-discovery/SKILL.md).
Deleting them would have cost that twice.

## What lives where

| | question | where |
|---|---|---|
| **verdict** | *Should* we run it? | [`../tombstones.md`](../tombstones.md) — the single owner |
| **facts** | What *is* it, and what did we work out? | here |
| **error text** | Why did it fail, verbatim? | [`../../bring-up-failures.md`](../../bring-up-failures.md) |

## What does NOT get archived

**Upgrade trackers.** A tracker holds *"the delta between where we are and a target"*
([documentation skill](../../../skills/documentation/SKILL.md)) — it is transitional by
construction. When the target is retired the delta is not history, it is noise, and its
durable findings belong in the failure catalogue or a tombstone before it goes.
`docs/upgrades/profile-step-3.7-flash.md` was deleted on those grounds and stays deleted;
`git show fd4c6d8:docs/upgrades/profile-step-3.7-flash.md` if ever needed.

**A sheet's forward-looking sections are frozen, not updated.** Each file opens with a
banner saying so. Do not maintain them — that is what retirement means.
