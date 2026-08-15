# ADR-0027: Module boundaries in the harness — serve, measure, and a core that is neither

**Date:** 2026-08-15
**Status:** Proposed

## Context

[ADR-0010](0010-test-bench-harness.md) made the harness a real package rather than a pile of
scripts, and that was the right call. It did not say how the package should be *arranged*,
so it grew flat: **22 modules besides `__init__`, ~5,800 lines, one namespace.**

Meanwhile the vocabulary settled into two stacks —
[`docs/measurement.md`](../measurement.md) and the glossary in
[`skills/development`](../../skills/development/SKILL.md) both describe a **serving stack**
(profiles, variants, engines) and a **measuring stack** (suites, runs, results). The
filesystem shows neither.

The split is real and lopsided:

| stack | modules |
|---|---|
| **serving** | `topology` `fleet` `activate` `ansible` |
| **measuring** | `suite` `runner` `suitectl` `bench` `evals` `coding` `quality` `multiturn` `soak` `tools` `vision` `sandbox` `reference` `store` `report` `scoreboard` |
| **neither** | `api` (zero dependencies) |

Four against sixteen, invisible in a flat directory. Someone changing how activation works
has no signal that two-thirds of the package is irrelevant to them.

### What a dependency audit found

The graph is nearly clean, and its exceptions are the interesting part.

**Two library modules import `cli`** — the entrypoint — which is backwards. Both are lazy,
in-function, and documented as deliberate:

- `suite.py` reads the **Operate scope** from the CLI so a suite step cannot name a
  privileged command: *"Read from the CLI at call time rather than duplicated, so the two
  cannot disagree."*
- `activate.py` pulls `SMOKE_REPORT` and `_smoke`: *"Imported at call time: the gate lives
  in `cli`, which imports this module."*

Both comments are honest about the inversion and neither is wrong about *why*. The scope
genuinely must not be duplicated, and the smoke gate genuinely runs during activation. What
is wrong is where those two things live.

**And `FLEET_LOCK` is declared twice** — `ansible.py:45` and `runner.py:62` — with a test
whose only job is to catch the copies drifting apart. It exists because they *did*:

> *"They were different files until 2026-08-11, while a comment in `ansible.py` asserted
> they were the same. `deploy` took `fleet.lock`; the suite took `runner.lock`, which only
> ever excluded other suites. So nothing stopped a deploy from re-rendering engine files,
> pulling an image or evicting weights in the middle of a measurement — and the resulting
> numbers would belong to no configuration, invisibly."*

A duplicated constant guarded by a test is a workaround for having no shared home.

## Decision

**Three extractions first, then two packages and a rule.** The extractions are defects in
their own right; doing them first means the layout is a clean move rather than a
rearrangement of tangles.

### 1. Extract what does not belong in `cli`

- **The Operate scope** is a fact about the command surface, not a feature of the
  entrypoint. It moves to a module both `cli` and `suite` import. The anti-duplication
  property that motivated the inversion is preserved — one definition, two readers.
- **The smoke gate** is a measurement that activation happens to trigger. It moves out of
  `cli` into its own module.

Both `→ cli` edges disappear. Nothing in the package depends on the entrypoint.

### 2. Give the fleet lock a home

`FLEET_LOCK` and the mutual exclusion around it move into the core, and `ansible` and
`runner` both import it. The duplicated constant goes; so does the need for a test that
compares two copies.

This also removes the *only* structural reason serving would depend on measuring — the lock
belongs to neither stack. It is how they refuse to interleave.

### 3. Two packages and a core

```
sparky/
  cli.py                  the entrypoint — imports everything, imported by nothing
  core/                   depends on nothing in sparky
    api.py                the vLLM client
    fleetlock.py          the deploy/run mutex
    scope.py              the command scopes
  serve/                  topology · fleet · activate · ansible
  measure/                suite · runner · suitectl · bench · evals · coding · quality
                          multiturn · soak · tools · vision · sandbox · reference
                          store · report · scoreboard · smoke
```

### 4. The rule, and a test that enforces it

> **`measure` may import `serve`. `serve` may not import `measure`. Neither imports `cli`.
> `core` imports nothing from `sparky`.**

`measure → serve` is legitimate and stays: `bench` needs a profile's topology to record what
it measured. The reverse is not, once the lock lives in `core`.

**A test asserts the direction**, in the spirit of ADR-0011's Layer 1 — the boundary is only
real if something fails when it is crossed. A comment describing an architecture is a
comment; an import-direction test is the architecture.

## Consequences

- **Scope becomes visible in the filesystem.** Working on activation means `serve/` — four
  modules, not twenty-two.
- **The public import path changes.** `from sparky import topology, bench` becomes
  `from sparky.serve import topology` / `from sparky.measure import bench`. The README
  advertises importability as a feature, so it changes with this.
- **Two lazy imports and a duplicated constant go away**, along with the test that guarded
  the duplication. That test is not deleted for convenience — its subject ceases to exist.
- **A new invariant is enforced rather than described.** Crossing the boundary fails the
  suite instead of being noticed in review, or not.
- **~60 files change their imports.** Mechanical, and the test suite is the check.
- **`sparky.cli:app` stays the entry point**, so `sparky.sh` and the installed harness venv
  are unaffected.

## Alternatives rejected

**Leave it flat.** Defensible at 22 modules and indefensible at 30. The measuring stack is
the one that grows — ADR-0024 stages objectives, discrimination and a judge; ADR-0026 adds
context depth. Flat gets worse on a known trajectory.

**Split the packages without the extractions.** The two `cli` imports become cross-package
cycles and the duplicated lock becomes a cross-package duplicate. Both are legal and both
would be baked in by a boundary drawn around them. The extractions are what make the
boundary honest.

**A package per regiment** (`measure/bench/`, `measure/coding/`…). Most are one file. Depth
without benefit.

**Keep `smoke` in `cli` and let `serve` import `cli` lazily.** Preserves a documented
inversion because it currently works. The comment explaining why a library imports its
entrypoint is the argument against it.

## Sequencing

**After the suite/runner rename lands and deploys.** That change touched ~60 files and is
not yet verified on the cluster; adding a package restructure on top would give a deploy
failure two candidate causes. This is a repo-only change with no runtime surface, so it can
wait for a clean baseline and cost nothing by waiting.

## References

- [ADR-0010](0010-test-bench-harness.md) — the harness as a package; this arranges what that created
- [ADR-0011](0011-functional-tests.md) — the layered test regiment the import-direction test joins
- [ADR-0018](0018-provision-select-split.md) — why deploy and a run must not interleave, which is what the lock enforces
- [`docs/synchronization.md`](../synchronization.md) — the lock table, and the incident that produced the duplicated constant
- [`skills/development`](../../skills/development/SKILL.md) — the glossary these package names follow
