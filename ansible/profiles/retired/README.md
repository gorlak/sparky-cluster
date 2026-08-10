# Retired profiles — the configs, kept; the verdicts, elsewhere

**Nothing in this directory is part of the allowlist.** Both profile loaders glob
`profiles/*.yml` non-recursively — `topology.all_profiles()` and the `fleet` role's
`fileglob` — so a subdirectory is invisible to them by construction, not by convention.
A file here installs nothing, keeps no weights, and cannot be activated. There is a test
(`tests/test_topology.py`) that asserts exactly that, because the day it stops being true
is the day a retired model quietly reappears in the fleet.

## Why this exists

Deleting a profile `.yml` is how a model leaves the allowlist and how its weights get
evicted (ADR-0018). That gesture is right, but it throws away something the register was
never meant to hold: **the engineering in the config itself.**

A profile is not a name and a path. It is the memory math that decided `gpu_memory_utilization`,
the parser names that were *read from the chat template* rather than guessed, the quant
findings that made someone write "never pass `--quantization` to this checkpoint", and the
flags that a defect forced off. Recovering that from `git log` requires knowing it exists
and which commit removed it. In practice nobody looks, and the next person re-derives it —
which for a parser name costs a deploy, and for the memory math costs a bring-up.

## What lives where

| | question it answers | where |
|---|---|---|
| **verdict** | *Should* we run this model? | [`docs/models/tombstones.md`](../../../docs/models/tombstones.md) — one scannable table, checked by [model-discovery](../../../skills/model-discovery/SKILL.md) before any candidate is proposed |
| **config** | *How* did we run it, and what did we learn doing so? | here |

These are not two copies of one thing. The register is deliberately the **single owner**
of every verdict — it says nothing about flags. These files are deliberately silent on
whether the model is worth running — they say nothing about the verdict. Each links to
the other and neither restates it.

## Retiring a profile

1. `git mv ansible/profiles/<name>.yml ansible/profiles/retired/`
2. Add the retirement banner at the top (see any file here): date, one-line reason, and a
   link to the tombstone row that owns the verdict.
3. Add or update that row in `docs/models/tombstones.md`, with a falsifiable
   *reconsider-when*.
4. `./sparky.sh lint` — the profile count must drop.
5. `./sparky.sh deploy --evict` — reclaims the weights, if nothing else references them.
   Twins share weights: retiring a `-single` whose TP=2 sibling survives frees **no disk**.

## Reviving one

Copy it back to `ansible/profiles/`, then **re-verify before trusting it**. These configs
were correct against the container and vLLM of their day, and both move. In particular
re-check the parser names (`./sparky.sh probe parsers`), the architecture
(`./sparky.sh probe archs`), and the quant algo in `config.json` — the three things that
have each cost a deploy when assumed. Delete the row from the tombstone register only once
it actually serves; git history keeps the reasoning either way.
