# ADR-0027: Module boundaries in the harness — four tiers that point downward

**Date:** 2026-08-15
**Status:** Accepted (2026-08-16)

## Context

[ADR-0010](0010-test-bench-harness.md) made the harness a real package rather than a pile of
scripts, and that was the right call. It did not say how the package should be *arranged*,
so it grew flat: **22 modules besides `__init__`, ~5,800 lines, one namespace.**

Meanwhile the vocabulary settled into stacks —
[`docs/measurement.md`](../measurement.md) and the glossary in
[`skills/development`](../../skills/development/SKILL.md) both describe a **serving stack**
(profiles, engines) and a **measuring stack** (suites, runs, results). The filesystem showed
neither. Someone changing how activation works had no signal that two-thirds of the package
was irrelevant to them.

### What a dependency audit found

The graph was nearly clean, and its exceptions were the interesting part.

**Two library modules imported `cli`** — the entrypoint — which is backwards. Both were
lazy, in-function, and documented as deliberate:

- `suite.py` read the **Operate scope** from the CLI so a suite step could not name a
  privileged command: *"Read from the CLI at call time rather than duplicated, so the two
  cannot disagree."*
- `activate.py` pulled `SMOKE_REPORT` and `_smoke`: *"Imported at call time: the gate lives
  in `cli`, which imports this module."*

Both comments were honest about the inversion and neither was wrong about *why*. The scope
genuinely must not be duplicated, and the smoke gate genuinely runs during activation. What
was wrong is where those two things lived.

**And `FLEET_LOCK` was declared twice** — `ansible.py` and `runner.py` — with a test whose
only job was to catch the copies drifting apart. It existed because they *had*:

> *"They were different files until 2026-08-11, while a comment in `ansible.py` asserted they
> were the same. `deploy` took `fleet.lock`; the suite took `runner.lock`, which only ever
> excluded other suites. So nothing stopped a deploy from re-rendering engine files, pulling
> an image or evicting weights in the middle of a measurement — and the resulting numbers
> would belong to no configuration, invisibly."*

A duplicated constant guarded by a test is a workaround for having no shared home.

## Decision

**Three extractions first, then four tiers and a rule.** The extractions are defects in
their own right; doing them first made the layout a clean move rather than a rearrangement
of tangles.

### 1. Extract what does not belong in `cli`

- **The Operate scope** is a fact about the command surface, not a feature of the entrypoint.
  It moved to `scope.py`, which both `cli` and `suite` import. The anti-duplication property
  that motivated the inversion is preserved — one definition, two readers, and
  `tests/test_cli_surface.py` asserts the CLI's Operate commands equal the allowlist.
- **The smoke gate** moved out of `cli` into `smoke.py`. It is not a measurement — it asks
  "did serving come up right", not "how good is this model" — so it belongs beside the sanity
  probes it aggregates, in a tier `activate` is allowed to reach.

Both `→ cli` edges disappeared. Nothing in the package depends on the entrypoint.

### 2. Give the fleet lock a home

`FLEET_LOCK` and the mutual exclusion around it moved into `fleetlock.py`, taken from both
sides — `flock(1)` in the deploy shell, `fleetlock.hold()` in the runner. The duplicated
constant is gone; so is the test that compared two copies. This also removed the *only*
structural reason serving would depend on measuring — the lock belongs to neither, it is how
they refuse to interleave.

### 3. Four tiers

Three buckets were drafted — `core`, `serve`, `measure` — and the drafting exposed a
contradiction: the smoke gate is run *by* activation (`serve`) but was filed under
`measure`, which would force `serve → measure`, the exact edge the lock extraction removed.
The gate is not a measurement, so the fix was a fourth tier between them.

```
sparky/
  cli.py                  the entrypoint — imports every tier, imported by none
  foundation/             depends on nothing else in sparky
    api · topology · scope · fleetlock
  verify/                 "did serving come up right?" — the activation sanity checks
    text_sanity · vision_sanity · smoke
  serve/                  the tier that CHANGES the cluster
    fleet · activate · ansible
  measure/                "how good, how fast, how far?" — sub-grouped by role, below
    loop/                 suite · runner · suitectl          (run a suite)
    instruments/          bench · evals · coding · soak · tools · sandbox · reference
    record/               store · report · scoreboard        (persist + present)
```

`measure` is sub-grouped where the other tiers are flat, because it alone carries thirteen
modules — enough that a flat directory would just be the original pile one level down. The
three roles are how you would *explain* it: a **loop** runs regiments, the **instruments**
measure the model (`sandbox` and `reference` are `coding`'s confinement and yardstick), and
the **record** persists and presents what they found. The split is organizational only —
the modules barely import each other (`bench → store` is the sole intra-tier edge), so the
groups answer "where do I look", not "what depends on what".

Two moves fall out of the tiers:

- **`topology` is foundation, not serve.** Every tier reads a profile's shape, and it depends
  on nothing in `sparky` but a YAML file. In `serve` it would drag `verify` and `measure`
  into importing `serve` for a data structure; in `foundation` it is the base they share.
- **`quality` + `multiturn` merged into `verify/text_sanity`, and `vision` became
  `vision_sanity`.** The multiturn conversation is only the corruption heuristics applied
  turn by turn — one module, not two — and in the verify tier the honest name for the check
  is *sanity*, not *quality*: it flags garbage, it does not grade.

### 4. The rule, and a test that enforces it

> **A module may import its own tier and any tier BELOW it, never one above.**
> `foundation` < `verify` < `serve` < `measure`, with `cli` above all four.

`serve → verify` is legitimate and load-bearing: activation runs the sanity checks on the
way up. `measure → serve` is legitimate: a bench needs a profile's topology to record what it
measured, and the runner activates models. The reverse of either is not, and
`tests/test_module_layers.py` fails the suite when a module reaches up — naming the file, the
edge and the direction — in the spirit of ADR-0011's Layer 1. A comment describing an
architecture is a comment; an import-direction test is the architecture.

## Consequences

- **Scope becomes visible in the filesystem.** Working on activation means `serve/` — three
  modules, not twenty-two. A recovery-critical reboot path depends only on `foundation`.
- **The public import path changed.** `from sparky import topology, bench` became
  `from sparky.foundation import topology` / `from sparky.measure import bench`. The README
  advertises importability, so it changed with this.
- **Two lazy imports and a duplicated constant went away**, along with the test that guarded
  the duplication — its subject ceased to exist, so it was not deleted for convenience.
- **A new invariant is enforced rather than described.** Crossing a tier fails the suite
  instead of being noticed in review, or not.
- **~60 files changed their imports.** Mechanical; the 617-test suite is the check, and every
  module-relative path (`__file__`) gained one `.parent` for the extra directory.
- **`sparky.cli:app` stays the entry point**, so `sparky.sh`, the `vllm-suite` trigger and the
  installed harness venv are unaffected — they name the console script, never a module path.

## Alternatives rejected

**Leave it flat.** Defensible at 22 modules and indefensible at 30. The measuring stack is the
one that grows — ADR-0024 stages objectives, discrimination and a judge; ADR-0026 adds context
depth. Flat gets worse on a known trajectory.

**Three tiers, smoke in `measure`.** The drafted shape. It filed the activation gate under the
tier activation is forbidden to import, reintroducing `serve → measure` the moment `activate`
called it. The `verify` tier exists because the gate is a check, not a measurement.

**Split the packages without the extractions.** The two `cli` imports become cross-package
cycles and the duplicated lock a cross-package duplicate — both legal, both baked in by a
boundary drawn around them. The extractions are what make the boundary honest.

**A package per regiment** (`measure/bench/`, `measure/coding/`…). Most are one file. Depth
without benefit.

## Sequencing

Done after the suite/runner rename landed and deployed, on a clean baseline — so a deploy
failure would not have two candidate causes. This is a repo-only change with no runtime
surface (the entry point and every deployed path name the console script, not a module), so
it cost nothing by waiting for one.

## References

- [ADR-0010](0010-test-bench-harness.md) — the harness as a package; this arranges what that created
- [ADR-0011](0011-functional-tests.md) — the layered test regiment the import-direction test joins
- [ADR-0018](0018-provision-select-split.md) — why deploy and a run must not interleave, which is what the lock enforces
- [`docs/synchronization.md`](../synchronization.md) — the lock table, and the incident that produced the duplicated constant
- [`skills/development`](../../skills/development/SKILL.md) — the glossary these tier names follow
