# Candidate model fact sheets — evaluated, wanted, not yet holdable

A **candidate** is a model this cluster has evaluated and would run, but cannot serve
today — and whose weights we do **not** hold. It is neither a fleet model nor a rejection,
and before this folder existed it had nowhere to live.

## Why this exists

The registers on either side of a candidate both refused it, for good reasons:

| | takes | rejects a candidate because |
|---|---|---|
| [`../tombstones.md`](../tombstones.md) | models we **will not run** | a candidate is one we *want* to run. A tombstone's job is to stop a sweep re-proposing something; filing a wanted model there inverts the signal. |
| [`../../defects.md`](../../defects.md) | a **deployed** thing that is broken | a defect's *clears-when* is tested against installed weights and engine files. There are none. |
| [`../<model>.md`](..) | models the fleet **holds** | those sheets describe something we can `activate` today, or park with `blocked: true`. A candidate has no profile to block. |

So the finding fell through the gap — and a finding with no home is re-derived. That is the
exact waste [`tombstones.md`](../tombstones.md) was built to prevent, stated in its own
opening: *"the expensive failure mode is rediscovering six months later, at the cost of a
download, a profile, a deploy and possibly a frozen node, that we already knew."* The same
argument applies to a model we already decided we **want** — re-running the screen costs the
same hours whether the answer was yes or no.

## What belongs here

A sheet in this folder means all of:

1. The model **passed the fit and speed gates** in [model-evaluation](../../../skills/model-evaluation/SKILL.md) — the arithmetic is done and written down.
2. We **do not hold the weights.** Nothing is staged, no profile exists.
3. The blocker is **external and nameable** — an unreleased vLLM architecture, an absent
   quant, a kernel that does not exist for sm_121. "We haven't got round to it" is not a
   blocker; that is a to-do.

If it fails (1) it is a tombstone. If (2) stops being true it is promoted (below). If (3)
cannot be named, the sheet is a wish and does not belong in a register.

## The lifecycle

**Promote** when the weights are staged: move the sheet to [`../<model>.md`](..), and the
blocker — now testable against installed files — becomes a `DEF-NNNN` row in
[`defects.md`](../../defects.md) with the profile carrying `blocked: true`. This is the path
[`deepseek-v4-flash.md`](../deepseek-v4-flash.md) already walked; it sits in `docs/models/`
rather than here precisely because we hold its weights.

**Reject** when the *Clears when* is met and the model then fails: move the verdict to
[`../tombstones.md`](../tombstones.md) and **delete the sheet.**

That deletion is deliberate, and it is the one place this folder departs from
[`retired/README.md`](../retired/README.md), which argues hard *against* deleting sheets.
The argument there turns on content: a retired sheet holds engineering **proven on this
hardware** — parser names that cost a deploy to learn, memory math from a real bring-up,
SM12.1 workarounds. A candidate sheet holds none of that. It is estimates, arithmetic and
links, and every one of those the tombstone row can carry in a paragraph. Nothing is
hard-won here, so nothing is lost.

*Retired sheets are kept because they are expensive. Candidate sheets are cheap by
construction.*

## Structure

Same as a fact sheet — [documentation](../../../skills/documentation/SKILL.md) owns the
shape — with two additions that carry a candidate's whole reason for existing:

- **Blocked on** — the named external thing, with a link to it.
- **Clears when** — falsifiable, in the spirit of a defect's *clears-when* and a tombstone's
  *reconsider-when*. A candidate with no clearing condition is a bookmark.

Naming follows the rest of `docs/models/`: the model name, lowercased, no vendor prefix and
no quant suffix — `mimo-v2.5.md`, beside `minimax-m3.md` and `mistral-medium-3.5.md`.
