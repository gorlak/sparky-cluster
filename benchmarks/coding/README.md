# Coding measurement — problem sets

One folder per set under [`problems/`](problems/). A set is self-describing: it declares
which toolchain runs it and how its answers are packaged, and the harness reads those
declarations without interpreting them.

**Nothing outside a set's folder knows what language that set uses** ([ADR-0024](../../docs/adr/0024-coding-measurement.md) §2).
`sparky/coding.py` chooses problems, drives stages and computes numbers; a grep for any
language name in it finds nothing. Language lives in exactly two places: here, and in
`vllm-sandbox`'s fixed `TOOLCHAINS` dict.

## What ships, and what may be missing

| set | ships | what it is for |
|---|---|---|
| [`python-basic`](problems/python-basic/) | with the clone | proving the schema and exercising the harness |

A set may also be a **git submodule**, in which case a cloner without access to it gets an
empty directory. That is a normal state, not an error: `lint`, `test` and a coding run all
work without it, and a run **names what it could not measure** rather than quietly
producing a smaller number.

Consequently **scores are never blended across sets.** Each records under its own scenario
(`coding:<name>@<version>`). Averaging two sets would produce a figure whose meaning
changed with whichever submodules happened to be checked out.

## Adding a set

Create `problems/<name>/` containing:

```
set.yml                 the declaration — name, version, toolchain, fence tags, answer form
<problem-id>.yml        one file per problem; the id must match the filename
reference/<problem-id>.*  one reference solution per problem, in the set's own language
README.md               what this set measures and what it does not
```

`tests/test_coding_problems.py` then validates it automatically: every reference must pass
its problem's hidden tests, and every `repair` problem's broken code must fail them. That
validation runs through the **same toolchain that scores a real answer**, because a
reference *is* an answer — the only one whose verdict is known in advance.

If the set needs a language no toolchain covers, that toolchain is added to `vllm-sandbox`
and installed by a deploy. A set supplies a *key*, never program text: a set may come from
a repository this cluster does not control, and set data must never become program text in
a root-invoked program.

## What a good problem looks like

Owned by each set's own README, because it depends on what the set is trying to measure.
Two rules are universal:

- **The tests are hidden.** They are never sent to a model. A test that appears in the
  prompt turns the benchmark into a copying exercise, and a test pins this.
- **The reference is not a model answer.** It exists to prove the problem is solvable and
  to normalise measurements against it — never to be shown.
