---
name: documentation
description: Conventions for this repo's docs/ tree — model fact sheets, upgrade trackers, and decision records (ADRs): where each lives, how they are structured and named, and the fact-vs-transition-vs-decision boundary. Read when asked to investigate/document a model, plan/track an upgrade, write an ADR, or decide whether a change needs one.
---

## The `docs/` tree — kinds of doc, and their purposes

| Location | Kind | Purpose |
|---|---|---|
| `docs/models/<model>.md` | **Fact sheet** | Independent statement of fact about **one** model — analyzed on its own, never vs another. |
| `docs/upgrades/<kind>-<…>.md` | **Upgrade tracker** | Living, gated tracker of a version/model **transition** and its cluster implications. |
| `docs/adr/NNNN-*.md` | **Decision record** | Immutable record of a decision already made — the "why." See "Decision records" below. **Not** for living state. |
| `docs/defects.md` | **Defect register** | Living index of **open** defects the cluster carries, each with a *clears-when* condition. Rolls up (links, never duplicates) the per-tracker WAR registers, ADR-0014, and README shortcomings. |
| `docs/updating.md` | **Update pathways** | The change-fan-out checklists (bump a container, add a model/role, …); each pathway ends by consulting the defect register. |
| `docs/*.md` | Reference | Standing docs (profiles, profile-tuning, serving-topology, control-interface). |

The boundary matters and is enforced:
- **Fact sheets** hold facts about a model, independent of any other. No transition analysis.
- **Upgrade trackers** hold the *delta* between where we are and a target, plus what it means for the cluster. They link to fact sheets; they never duplicate them.
- **ADRs** are immutable once Accepted. A thing still in flux is a tracker, not an ADR.
- **The defect register** is an *index*, not a home — a row links to the detailed WAR/ADR/fact-sheet that owns the analysis. When you add a workaround anywhere, add its `DEF-NNNN` row here and back-reference the ID from the code/profile comment. See `docs/defects.md` for the filing convention.

## Which artifact does the request want?

- "Investigate / analyze / document model X" → **fact sheet** `docs/models/X.md` (create or update).
- "Should profile P serve model M?" / "what changes if `step` runs 3.7?" → **profile upgrade tracker** `docs/upgrades/profile-P-M.md`.
- "Bump / upgrade the container to T" → **container upgrade tracker** `docs/upgrades/container-<coord>-T.md`, and walk the pathway in `docs/updating.md`.
- "We're carrying a bug / working around an upstream issue" → a **DEF-NNNN row** in `docs/defects.md` (+ detail in its home).
- "How do I update X / what else needs touching?" → `docs/updating.md` (the change-pathway checklists).
- "What needs updating / is there a newer version?" → the [[version-discovery]] skill (check + stage), which then walks `docs/updating.md`.
- A settled, one-way decision → an **ADR** (see "Decision records" below), not a tracker.

## Naming (upgrade trackers)

**Target version only** — the source is "wherever we are," and history covers it. Prefix by kind:

- `container-<image-coordinate>-<target-tag>.md` — e.g. `container-nvidia-vllm-26.06-py3.md` (mirrors `nvcr.io/nvidia/vllm:26.06-py3`; NGC images are calendar-versioned `YY.MM`).
- `profile-<profile>-<target-model>.md` — e.g. `profile-step-3.7-flash.md` (the step profile family adopting a new model).

Fact sheets are just `<model>.md`.

## Model fact sheet structure (`docs/models/<model>.md`)

Copy an existing one (e.g. `docs/models/step-3.7-flash.md`). Shape:

- **Header:** Last updated · Hardware line · **Installed quant** · **Target quant**.
- **Model Overview:** developer, architecture (MoE vs dense — say which), params (total / active), context window, special features, HF links.
- **Quantization Formats & Footprint** table: `format | source | disk | per-node @ TP | fit`.
- One **`## <QUANT> — <hf-repo>`** section per quant: memory-fit table, tooling status, draft serve flags, short assessment.
- **What to Watch For** · **Key Links**.

Do the analysis per [[model-evaluation]] (verify disk with `du` first; never pass `--quantization` for a self-declaring checkpoint; MoE loads *all* experts → size on total params). The fact sheet is where that analysis lands.

## Upgrade tracker structure (`docs/upgrades/<kind>-<…>.md`)

Copy an existing one (`container-nvidia-vllm-26.06-py3.md` or `profile-step-3.7-flash.md`). Shape:

- **Header:** **Status** (🟡 in progress · 🔵 evaluated, not started · ✅ done · ⛔ abandoned) · Current · Target · Last updated. State plainly that it is a living tracker, not an ADR.
- **Why** — what the upgrade buys.
- **Options / what changes** — a table comparing current vs target(s).
- **Implications for the cluster** — memory/headroom/profile-archetype/dev-availability effects.
- **Dependencies** — what must land first; cross-link other trackers it waits on.
- **Workarounds (WARs) register** — every workaround the target requires, as a table
  (`WAR | fixes | upstream issue | cost | applied in | remove when | status`). Workarounds
  are **debt**: each is tied to an upstream bug and an explicit **removal condition**
  (issue closed / fixed in version N). On a later bump, review the register, drop any whose
  condition holds, and **re-test one at a time** to confirm it's no longer load-bearing —
  removing several at once hides which still mattered.
- **Completion criteria** — the concrete conditions under which we re-attempt or finish. This is what makes it *gated* rather than a wish.
- **Retry / deploy plan** — how we'll attempt it safely (behind the fail-safe net, solo-before-multinode, soak, etc.).
- **Re-assessment log** — dated entries; append, don't rewrite history.
- **References** — fact sheets, dependent trackers, relevant ADRs.

## After writing

- **Cross-link, don't duplicate:** trackers → fact sheets; dependent trackers → each other. Keep fact sheets free of transition analysis.
- Update pointers if the doc is new (e.g. the README `docs/` tree, a `group_vars` comment that should reference it).
- Committing: see [[development]] (Geoff runs the commits; stage and stop).

## Decision records (ADRs)

ADRs live in `docs/adr/` and are the immutable record of decisions — the **why**,
distinct from the living docs above. One file per decision; copy an existing
`docs/adr/NNNN-*.md` for the format.

### What an ADR is
It captures **why** a decision was made — the context at the time, the options
considered and rejected, the trade-offs accepted. Point-in-time, not living. It
answers "why does the system work this way?" for a reader who wasn't in the room.

### ADR vs. the living docs

| Question | Goes in |
|---|---|
| Why choose X over Y? / what trade-offs? / what did we reject? | **ADR** |
| What does the system do right now? / how to operate it? | `README.md` / `docs/` |
| Known shortcomings? | `README.md` "Known Shortcomings" |
| A model's facts, or an upgrade's delta? | `docs/models/` / `docs/upgrades/` (above) |

Tempted to add a "lessons learned" footnote to an existing ADR? Stop — that goes in
the docs, or in a new ADR that supersedes this one.

### Status lifecycle
`Proposed → Accepted` (↘ `Superseded by ADR-NNNN`; `Deprecated` = no replacement).
Two active states only — **no `Implemented`, no in-progress**; this is the standard
ADR lifecycle (Nygard / MADR). Implementation *tracking* ("how much is built") lives
in code / README / TODO, never in the status. **Proposed** = a design not yet built.
**Accepted** = the decision *and* its implementation landed together (see "How to
write one"), it's now in effect — and **immutable**.

### Immutability
Once **Accepted**, the only permitted edit is adding a `Superseded by ADR-NNNN`
line — nothing else, even if the decision turned out wrong. To change a decision,
write a **new** ADR referencing the old one and set the old one to Superseded. The
historical record of what was believed at the time is the point.

### When to write one
Write for: a new service/tool/pattern; a significant accepted trade-off; reversing a
prior decision; something tried-and-failed that constrains future options. Do **not**
write for: config-value changes within an existing pattern (→ commit message / docs);
operational events (→ README notes); decisions still under discussion (use Proposed,
don't commit).

### How to write one
1. Copy an existing `docs/adr/NNNN-*.md`.
2. Number from the highest existing `docs/adr/NNNN-*.md` filename.
3. Status `Proposed` while it's an unbuilt design; flip to `Accepted` in the commit
   that lands the implementation (usually the same commit — step 5).
4. Sections: **Context** · **Options considered** · **Decision** · **Consequences**.
5. Commit in the **same commit** as the implementation it documents ([[development]]).

### Documentation drift beats a missing ADR
When cluster behaviour changes, update the living doc in the same commit (`README.md`
operational truth; `docs/<subsystem>.md` design; `ansible/` source of truth). Docs
describing old behaviour are more harmful than a missing ADR — update the docs first,
write the ADR second.
