# `python-basic` — the example set

**This set does not rank the fleet, and a score from it is not a measurement of a model.**
It exists so that anyone who clones the repository has a working harness to run: it proves
the schema, exercises every stage, and gives the toolchain seam something to execute.

## Why it cannot rank anything

An earlier draft called this a *private* set whose contamination defence was never being
published. Both halves were wrong, and the correction is the reason this file exists:

1. **This repository is public.** Anything committed here is published by definition.
2. **The problems are canonical anyway.** `merge-intervals` is LeetCode 56, `topo-order` is
   textbook Kahn, `repair-mutable-default` is *the* Python gotcha. Secrecy of a file buys
   nothing when the task underneath appears thousands of times in any crawl.

Every model these problems would rank has seen them. Treating the resulting number as a
verdict is a misuse.

## What it still does honestly

The tests are written so that **recall is a liability**. `dedupe` asserts on unhashable
elements, which kills the memorised `set()` one-liner, and on `True == 1` distinct-but-equal.
`merge` asserts that touching intervals merge, that zero-width intervals survive, and that
the input is not mutated. A model regurgitating LeetCode 56 returns lists instead of
tuples, sorts in place, and fails.

That is a real property — it measures specification adherence over memorisation — and it is
the only claim this set makes.

Sets that *can* rank the fleet get their contamination resistance from constraining the
interface instead: a problem provides the primitives and forbids everything else, so
"implement a growable array against **this** allocator" has no crawled answer. See
[ADR-0024](../../../../docs/adr/0024-coding-measurement.md) §3.

## Shape

Two tracks:

- **`implement`** — a signature and a docstring; the model writes the body.
- **`repair`** — a function that fails a test it should pass, plus the failure output; the
  model fixes it. This is most of what a coding assistant actually does, and a single-shot
  generation benchmark never sees it.

One directory per problem, so code lives in files an editor, a linter and a formatter can
see:

```
<problem-id>/
  problem.yml    id, track, difficulty, prompt — prose and metadata only
  tests.py       hidden cases, one function each
  reference.py   the known-good answer
  broken.py      repair track only: the code the model is asked to fix
```

Tests are **named functions**, graded by severity. `@weight(n)` is injected by the runner,
so no import is needed inside a sandbox that has no site-packages:

```python
def test_keeps_first_occurrence():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]

@weight(3)
def test_unhashable_elements():
    # A set-based one-liner — the memorised answer — raises here.
    assert dedupe([[1], [2], [1]]) == [[1], [2]]
```

Weight 1 is an ordinary requirement; 3 marks a case that separates understanding from
recall. The measured effect on this problem: `dict.fromkeys(items)` — the one-liner every
model knows — passes five of six cases and scores **9 of 12** by weight, failing only the
unhashable one.

Tests are **hidden** — never sent to the model. They execute against its answer in the
sandbox, and pass@1 is the score. No judge model: the one domain where correctness is
mechanically decidable is the last place to introduce a model's opinion.

`reference.py` is never shown to a model; it exists to prove the problem is solvable, and
`tests/test_coding_problems.py` runs it through the same toolchain that scores a real
answer.

## Writing a good one

- **Decidable, not stylistic.** If a human would argue about whether the answer is right,
  it is the wrong problem.
- **Test the edges the prompt implies**, not just the happy path — an empty input, a
  duplicate, a boundary. Most model failures live there, and a test suite that misses them
  scores confident wrong answers as correct. On a canonical problem this is the *whole*
  signal.
- **No I/O, no network, no clock, no randomness.** The sandbox has no network and a
  ten-second budget; a problem that needs any of those is untestable here and its failures
  would be the harness's, not the model's.
- **Small.** One function, a handful of asserts. Long problems measure instruction-following
  and context handling — real abilities, measured elsewhere, and they blur this signal.
