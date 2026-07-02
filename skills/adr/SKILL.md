---
name: adr
description: When to write an ADR, what belongs in one vs. in documentation, and the immutability rules for completed ADRs. Read before creating or modifying any file in adr/ or before deciding whether a change needs an ADR.
---

## What an ADR is

An ADR captures **why** a decision was made — the context that existed at the
time, the options considered and rejected, and the trade-offs accepted. It is a
point-in-time record, not a living document. It answers "why does the system
work this way?" for a future reader who wasn't in the room.

## What belongs in an ADR vs. documentation

| Question | Goes in |
|---|---|
| Why did we choose X over Y? | ADR |
| What trade-offs did we accept? | ADR |
| What options did we reject and why? | ADR |
| What does the system do right now? | `README.md` / `docs/` |
| How do I operate or deploy it? | `README.md` |
| What are the known shortcomings? | `README.md` "Known Shortcomings" |
| What is the design of a subsystem? | `docs/<subsystem>.md` |

If you're tempted to add a "lessons learned" or "how it turned out" footnote
to an existing ADR: stop. That information belongs in the documentation or in
a new ADR that supersedes this one.

## Status lifecycle

```
Proposed → Accepted → Implemented
                   ↘ Superseded by ADR-NNNN
            Deprecated  (no replacement; decision no longer relevant)
```

- **Proposed** — under consideration; may still change or be rejected.
- **Accepted** — decision made; implementation not yet complete or deployed.
- **Implemented** — in production. The ADR is now immutable (see below).
- **Superseded by ADR-NNNN** — replaced by a later decision. The old ADR
  stays unchanged as historical record; the new one describes the new direction.
- **Deprecated** — no longer relevant, no replacement ADR needed.

## Immutability rule

Once an ADR reaches **Implemented**, the only permitted edit is adding a
`Superseded by ADR-NNNN` line at the top of the Status field. Nothing else
changes — not the context, not the consequences, not the options considered,
even if the decision turned out to be wrong or the rationale looks naive in
hindsight. The historical record of what was believed at the time is the point.

If a decision changes: write a **new ADR** with the next number, reference the
old one in its Context, set the old one to `Superseded by ADR-NNNN`.

## When to write an ADR

Write one when:
- A new service, tool, or architectural pattern is being added to the cluster.
- A significant trade-off is being accepted (e.g. "we can't do X because we
  chose Y").
- A prior decision is being reversed or replaced.
- Something was tried and failed in a way that constrains future options.

Do **not** write one for:
- Config value changes within an existing pattern (bump `vllm_image`, change
  `max_model_len`) — those belong in commit messages and/or `docs/`.
- Operational events (model downloads, driver updates) — those belong in
  `README.md` operational notes or nowhere.
- Decisions still under discussion — use Proposed status and don't commit
  until the decision is made.

## How to write one

1. Copy the structure from any existing `adr/NNNN-*.md`.
2. Number sequentially from the last entry in `adr/README.md`.
3. Status starts at `Proposed` or `Accepted` depending on whether the decision
   is final.
4. Sections: **Context** (what problem, what constraints), **Options
   considered** (each with pros/cons), **Decision** (what was chosen and the
   one-line reason), **Consequences** (what becomes easier, what becomes harder,
   what constraints are now in place).
5. Add a row to `adr/README.md`.
6. Commit the ADR **in the same commit** as the implementation it documents.
   See [[dev-workflow]] for commit conventions.

## Keeping documentation current

When a change is made to the cluster — new service, changed behaviour, updated
operational procedure — update the relevant living doc in the same commit:

- `README.md` — operational truth: what's deployed, how to run it, known
  shortcomings.
- `docs/<subsystem>.md` — subsystem design and current behaviour.
- `ansible/` vars and templates — the Ansible source of truth.

Documentation drift (docs describing old behaviour) is more harmful than a
missing ADR. When in doubt: update the docs first, write the ADR second.
