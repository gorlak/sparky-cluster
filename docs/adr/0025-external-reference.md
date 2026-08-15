# ADR-0025: An external reference — scoring a frontier model on our own sets

**Date:** 2026-08-14
**Status:** Proposed

## Context

[ADR-0024](0024-coding-measurement.md) bought contamination resistance by **constraining the
interface**: an answer is compiled against a contract the problem declares and may reach
nothing the problem did not provide. That works, and it costs exactly what it was always
going to cost — *"the numbers are not comparable to anyone else's."*

Which leaves two questions the fleet cannot ask itself:

**How far from the ceiling is any of this?** "qwen3.6 scores 55% weighted" is a ranking, not
a scale. It does not say whether 55% is near the best anyone could do on these problems or
nowhere close, and that is the number behind the actual decision — *what am I giving up by
running locally?*

**Is the SET any good?** A set every model fails ranks nothing, and today there is no way to
tell *"my models are weak here"* from *"my problem is badly written."* A known-strong model
separates them: if it scores near zero, the problems are ambiguous rather than hard; if it
scores 100%, the set cannot discriminate at the top and needs harder problems.

Both are answered by scoring one frontier model on the same sets, by the same path. Nothing
else here changes.

## Decision

**Score Claude on the same sets, through the API, as a labelled non-fleet reference.**

### 1. The API, not the `claude` CLI

`claude -p` is available, authenticated on the box, and needs no credential handling at all.
It is still the wrong instrument, and the reason is **measurement validity rather than
permission**:

`claude -p` runs an *agent*. Even with `--allowed-tools ''` it carries a system prompt and
an agent frame; the fleet gets a single bare user message at `temperature=0.0` with no
system prompt and no tools. Putting those two numbers in one column is a category error
that flatters one side systematically. It also exposes no temperature control, so the
reference would not be reproducible run to run.

The API path is already apples-to-apples: one bare user turn, no system prompt,
`temperature=0.0`, both sides.

**The agent column is legitimate — separately.** The operator writes software *with* Claude
Code, so "what does an agentic harness score on these problems" is a real and
decision-relevant question. It is simply a different measurement, and it earns its keep
later: when the question becomes *"would scaffolding a local model close the gap?"*, there
has to be a column agents are allowed to compete in.

| scenario | what it measures |
|---|---|
| `coding:<set>@<v>` | raw completion — the fleet and the API reference share this |
| `coding:<set>@<v>/agent` | an agentic harness on the same problems |

**Never blended.** Same rule as ADR-0024's sets: two conditions, two scenarios.

### 2. No borrowed system prompts

A tempting symmetry — Anthropic publishes system prompts, so inject one into the local
models and level the field. Three reasons not to, escalating:

1. **It is the wrong artifact.** What is published is the **claude.ai web and mobile chat**
   prompt, in the documentation release notes (not GitHub), and Anthropic states it **does
   not apply to the API**. Claude Code's prompt is not published; repositories claiming it
   are community extractions of unknown accuracy. Injecting it would not reproduce the
   reference we are comparing against.
2. **A system prompt is model-specific.** Claude's is written against Claude's training and
   tendencies. Handing it to Qwen is not levelling the field; it is issuing instructions
   calibrated for a different model, and it may well hurt.
3. **It makes our scores a function of someone else's release schedule.** That prompt
   changes with every model release, so the trend store would move for reasons unrelated to
   our models — the exact failure ADR-0024 already refuses when it declines procedurally
   generated problems: *scores would move when the generator changed rather than when the
   models did.*

**A system-prompt axis is still allowed** — ours, short, in the repo, versioned, and in the
scenario key, so it moves when we move it. It belongs with ADR-0024 §9's objectives, not in
the baseline. For ranking, the condition only has to be **identical across models**; a
preamble that lifts every model equally changes no ranking and buys a variable for nothing.

### 3. Thinking is priced, not equalised

The two mechanisms do not correspond, and pretending they do produces a number nobody can
interpret:

| | our models | Anthropic |
|---|---|---|
| how it thinks | emergent `<think>` blocks, trained in, inline in the completion | explicit `thinking: {budget_tokens: N}` |
| who controls it | nobody — the model decides | a number you set |
| whose budget | **shares `max_tokens` with the answer** | allocated within `max_tokens` |
| running out | produces **no answer at all** | still answers |

There is no defensible definition of "equal thinking" across that gap. So do not define
one. **Thinking is a cost** — GPU-seconds locally, dollars remotely — and a cost is
measured, not equalised.

Record the tokens each answer actually spent, and the asymmetry stops being a confound and
becomes data: *"qwen3.6 scores 55% at 1,900 tokens a problem; Sonnet scores 80% at 3,100."*
That is the same philosophy ADR-0024 §8 applies to memory — efficiency measured beside
correctness, never folded into it.

Investigating parity for this ADR turned up a defect in the *fleet's own* scores — the
token budget covering thinking and answer together, with neither the spend nor the finish
reason recorded. That is a local-model concern and belongs to the local-model decision, so
it lives in [ADR-0024](0024-coding-measurement.md) §7 rather than here. **It stands whether
or not an external reference is ever run**, and this ADR depends on it: without recorded
spend there is no way to price thinking, and pricing it is the whole of this section.

### 4. The credential never rests on the cluster

The key lives in the environment of whoever runs the script, and nowhere else. No deploy
writes it, no service reads it, nothing under `/opt/cluster` holds it. A file is permitted
only if the operator chose to create one — `ANTHROPIC_API_KEY_FILE`, or a conventional path
— and only if it is not group- or world-readable; that mode check is what makes a
credential at rest defensible rather than merely convenient.

**Claude Code's own credential store is off limits.** Not on grounds of volume or terms:
that token is minted by an OAuth flow *for a specific client*, and replaying it from another
program means presenting as a client we are not. Reading another program's credential store
is not something this repository will do.

**What actually protects the key is its scope, not the file layout.** A subcommand would be
equally safe from the panel and from a suite, because neither has the variable set. This
is worth stating plainly because the first draft of this decision claimed otherwise.

### 5. One entry point — it is a quality measurement, not a separate tool

`sparky coding --via anthropic`. The reference belongs beside the other quality
measurements, because that is what it is: the same problems, the same sandbox, the same
verdicts, differing only in which endpoint answers.

An earlier draft made it a second root script on the grounds that egress is not a cluster
operation. That argument was real but it was solved in the wrong place — a second entry
point also means a second implementation of set discovery, the run loop and recording, and
**a reference that drifts from the thing it calibrates is worthless.** One engine, one door,
and the concerns that motivated the split are handled as behaviour rather than as file
layout:

- **Egress is explicit at the call site.** `--via anthropic` is not a default and cannot be
  reached by accident; the local path remains the one that needs no flag.
- **A private set's prompts are refused by default.** A set carried by a submodule is
  precisely the asset that was kept private, and sending its prompts to a third party is a
  decision, not a side effect. `--via anthropic` **refuses** such a set unless
  `--publish-prompts` is passed as well. Public sets need no ceremony. (The hidden tests
  never leave regardless; only what a model is shown.)
- **Campaigns stay local.** The suite regiment invokes the local path only. A campaign
  must not depend on a third party's availability or on a credential, and the reference is a
  constant that does not belong in a per-model sweep anyway.

### 6. A reference row is not a fleet member

Recorded under the profile `reference`. It cannot be activated, must never draw a retirement
verdict, and must never be read as a candidate for serving. The scoreboard exists to decide
which of *our* models should serve; a yardstick cannot.

Two models are wanted: **Sonnet** as the realistic comparison and **Opus** as the stretch.
Both are one `--model` flag over the same credential — a second reference costs nothing but
the tokens.

The reference is a **constant for a given set**. It moves when the set moves or when a new
model ships, not on a schedule and not as part of any campaign. The harness records the set
version with every reference score, so a coding run can say *"c-basic moved to v2; the
reference is still on v1"* rather than silently comparing across versions.

## Consequences

- **The scoreboard gains a scale**, and the fleet's numbers stop being purely ordinal.
- **Set quality becomes measurable.** A strong model scoring near zero indicts the problems,
  not the models — a signal available no other way.
- **The cluster holds no credential and needs no egress.** A deploy installs nothing for
  this; the panel and every suite are structurally unable to invoke it.
- **A private set's prompts leave the box when the reference is run.** That is an operator
  decision each time, which is the main reason it is not a flag on an existing command.
- **Scores now depend on a third party's availability** — for the reference column only. The
  fleet's own measurement path is untouched, which is why this is not wired into `deploy`.
- **Token accounting becomes part of the measurement**, and with it a correction to
  ADR-0024's budget that the fleet needed anyway.
- **It still needs the deployed sandbox**, so "anyone can run it" is bounded by cluster
  access plus their own key — not by the key alone.
- **The suite regiment stays local.** A campaign that could reach an external service
  would inherit its outages and its bill; the reference is a constant and does not belong in
  a per-model sweep.

## Alternatives rejected

**Drive the `claude` CLI instead of the API.** Needs no credential, which is genuinely
attractive. Rejected as the *comparable* reference because it measures an agent against bare
completions. Kept as a separately-scoped column, where it is a real measurement of a real
thing.

**Lift the key from Claude Code's credential store.** Replaying a token minted for another
client. Not built.

**Inject Anthropic's published system prompt into the fleet.** Wrong artifact, model-specific
by nature, and it would tie our trend store to their release cadence.

**A separate root script (`./claude.sh`).** Built first, then withdrawn. The security
argument for it did not hold — the credential's *scope*, not the entry point, is what keeps
the panel and every suite out, since neither has the variable set. What remained was
egress visibility, and that is better served by an explicit flag plus a refusal on private
sets than by a second program carrying a second copy of the measurement.

**Equalise thinking between the two.** No definition survives contact with the mechanisms:
one is a dial, the other a behaviour, and their failure modes differ. Measuring what each
spent is both easier and more useful.

**Run the reference during a deploy.** The cadence is right — the yardstick only moves when
the set moves, and the set changes at deploy time — but it would make `deploy` depend on a
third party, and `deploy` is the operation that must work when things are already broken.
The harness noticing a stale reference gets the same cadence with no coupling.

## Staging

1. [ADR-0024](0024-coding-measurement.md) §7's token accounting — recorded spend and a kept
   finish reason. A prerequisite: without it nothing here can be priced.
2. `--via anthropic` on `sparky coding`, with Sonnet recorded under the `reference`
   profile, and a refusal to send a private set's prompts without `--publish-prompts`.
3. Extended thinking for the reference, inside the same total budget, with tokens recorded
   for both sides.
4. Opus as a second reference.
5. The `/agent` column, if and when the scaffolding question is worth asking.

## References

- [ADR-0024](0024-coding-measurement.md) — the sets, the sandbox and the verdicts this reuses
  wholesale; §8 for why efficiency is measured beside correctness rather than folded into it
- [ADR-0016](0016-continuous-evaluation-outer-loop.md) — the measurement loop this reports into
- [ADR-0018](0018-provision-select-split.md) — why nothing network-facing may hold privilege
- `sparky/reference.py` — the client; `sparky coding --via` is the entry point
- [docs.claude.com/en/release-notes/system-prompts](https://docs.claude.com/en/release-notes/system-prompts)
  — what is actually published, and why it is not what we would need
