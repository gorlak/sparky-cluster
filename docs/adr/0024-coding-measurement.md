# ADR-0024: Measuring coding ability — problem sets, a compiled contract, and instrumented primitives

**Date:** 2026-08-14
**Status:** Accepted

## Context

The fleet is ranked on MMLU-Pro. The operator writes software. That gap has been the
largest one in the measurement loop since ADR-0016 shipped, and the `fast-tier` campaign
of 2026-08-11 turned it from a gap into an obstruction: Qwen3-Coder-Next came out
**dominated on every measured axis**, and the only honest verdict was *park it, do not
evict* — because its one surviving claim is coding and nothing here can measure it. The
same override now applies to Qwen3-VL, dominated on the scoreboard while being the only
model in the fleet with vision. **Two of six rows are carried by axes the scoreboard is
blind to**, and each blind spot has to be remembered by a human at the moment of decision.

### Every off-the-shelf set is contaminated, and this was verified rather than assumed

LiveCodeBench is the right idea — problems published after a model's training cutoff, so
memorization cannot help — and it is the one we would adopt if it worked. It does not:

| | |
|---|---|
| newest problem in `test6.jsonl` | **2025-04-06** |
| documented releases | v1–v5, newest problems Jan 2025 |
| our fleet | every checkpoint is a 2026 release |

Checked against the dataset on 2026-08-11 because secondary sources claim otherwise —
several describe monthly updates and a "v6 emphasising temporal generalisation" with
leaderboard results into February 2026. The 175 problems in the file span January to April
2025. **A benchmark whose headline property has expired is worse than none**, because the
number still looks authoritative, and this one now measures memorisation for every model
we serve.

HumanEval and MBPP are older and worse on the same axis. SWE-bench measures repository
navigation and patch application as much as coding, needs per-task container images, and
its instances are 2023-era GitHub issues.

### The first draft's premise did not survive review

That draft proposed a **private set**, contamination-resistant by never being published.
Two facts falsify it:

1. **This repository is public** — `github.com/gorlak/sparky-cluster`. Anything committed
   here is published by definition, so the ten Python problems written for the draft were
   never private and the instruction "do not publish these problems" was unenforceable.
2. **The problems are canonical anyway.** `merge-intervals` is LeetCode 56;
   `topo-order` is textbook Kahn; `repair-mutable-default` is *the* Python gotcha. Secrecy
   of the file buys nothing when the task underneath appears thousands of times in any
   crawl.

What saves those problems is not secrecy but their **tests** — `dedupe` asserts on
unhashable elements, which kills the memorised `set()` one-liner, and on `True == 1`
distinct-but-equal. A recalled answer *fails*. That is a real property, and it points at
the principle this ADR is built on, which the first draft did not state.

## Decision

**Measure coding by compiling an answer against a contract we define, executing it against
hidden tests, and instrumenting the primitives it is forced to use.** Contamination
resistance comes from **constraining the interface**, not from hiding the problem.

### 1. A problem set is a folder, and a set may be absent

`benchmarks/coding/problems/<set>/`, one folder per set, each with a `set.yml` declaring
its name, version and language defaults. Two exist at the outset:

| set | ships | what it is for |
|---|---|---|
| `python-basic` | in this repo, with the clone | proving the schema and exercising the harness end to end |
| `cpp-fundamentals` | a **submodule**, may be missing | the measurement that ranks the fleet |

A folder that is a submodule can simply not be there — a cloner without permission gets an
empty directory. **Absence is a first-class state, not an error.** `lint`, `test` and a
coding run all work without it, and any set actually present is used.

Three rules follow, and each closes a way the number could silently lie:

- **A run reports which sets it found.** A missing private set that quietly produces a
  smaller run is the failure mode [ADR-0009](0009-fail-safe-boot.md) exists to prevent: a
  thing that fails without saying so.
- **Sets are never blended into one score.** Each is its own scenario
  (`coding:<set>@<version>`). Averaging two sets produces a number whose meaning changes
  depending on whether a submodule happened to be checked out.
- **A submodule's commit SHA is its version stamp** — unforgeable, and it moves exactly
  when the set moves. The in-repo set carries a hand-maintained version in `set.yml`,
  which is strictly worse and is the price of shipping it with the clone.

`python-basic` is **an example set, not a ranking instrument**, and its README says so.
Its job is to make the harness runnable for anyone who clones the repo.

### 2. The harness knows stages, not languages

**Nothing outside a set's own folder may know what language that set uses.**
`sparky/coding.py` decides what to ask, drives the stages and computes the metrics; it
contains no fence tag, no `exec`, and no mention of Python. The first draft failed this
plainly — a `(?:python|py)?` fence regex, a prompt hardcoding *"the complete Python
function"*, and a runner that `exec`s two strings into a shared scope. Each of those is a
Python detail sitting in the language-neutral layer.

The seam is already half-built: `coding.run` takes `execute` **injected**. This extends
that principle to everything else language-shaped.

**A set declares; it never supplies code to the privileged path.** `set.yml` names a
**toolchain** and parameterises it:

```yaml
name: python-basic
version: v1
toolchain: python3-isolated        # a key, not a program
fence_tags: [python, py]
answer_form: "the complete function (or functions) and nothing else"
```

`vllm-sandbox` holds a **fixed dict of toolchains**, deploy-written — exactly the shape
[ADR-0019](0019-bounded-image-probe.md) established for the image probe, which takes a key
into a fixed dict of programs. This is not stylistic: a set folder may be a submodule from
a repository the harness does not control, and if set data could become program text in a
root-invoked program, the object gate in §6 would be decorative — the harness itself would
be the injection path.

**So adding a language is a deploy**, exactly as adding a probeable image or a startable
suite is. That is the established price of widening a bounded program, and it is the
right one to pay here.

**Stages are a superset; a toolchain declares which of them it runs.** `python3-isolated`
runs extract → execute. `cpp-freestanding` runs extract → source scan → compile → object
gate → link → execute. The verdict taxonomy (§7) is the union of every stage's terminal
states, and a toolchain with no link step simply never produces `link_error`.

**Metrics work the same way.** The empirical exponent (§8) needs instrumented primitives,
which `python-basic` does not provide — so it reports correctness and nothing else. That is
a declared property of the set, not a missing feature.

What remains in the harness is what is genuinely language-neutral: which problem to ask,
under which objective, the stage sequence, the confusion matrix, the trend store, and the
arithmetic that turns counters into an exponent.

### 3. Contamination resistance comes from fundamentals, not from secrecy

The C/C++ sets provide the primitives and forbid everything else. A problem hands over a
header — an allocator interface, a comparator, whatever it chooses to offer — and the
answer may include **nothing but that header**. No `<vector>`, no `<string>`, no
`<algorithm>`.

This is the load-bearing decision, and it pays three ways at once:

- **Measurement.** "Implement a growable array against *this* allocator" has no crawled
  answer. It tests memory discipline and algorithmic choice rather than STL API recall,
  which for C/C++ is the axis worth measuring.
- **Security.** The undefined-symbol surface collapses to almost nothing (§6).
- **Instrumentation.** Every primitive the problem provides is a measurement probe (§8).

Restricting the interface was proposed as a security control. It turns out to be the
contamination answer and the instrumentation mechanism as well.

### 4. Two tracks, and exactly one attempt

- **`implement`** — a contract plus hidden tests. Can it produce correct code from a spec.
- **`repair`** — code that fails a test it should pass, plus the failure output. Can it use
  an error message, which is most of what a coding assistant does and what a single-shot
  generation benchmark never sees.

**One attempt, and the bar is high. The last fenced block is the answer.** A model reasons,
echoes the interface it was given, drafts, corrects, and closes with the code it stands
behind; the last block is that closing answer, read the way a human reads it. `<think>`
sections are stripped first, because that is parsing rather than forgiveness.

> **Reversed after first contact with a real model (2026-08-15).** This section originally
> specified a *unity build* — concatenate every fenced block — on the theory that a model
> emitting a draft and a correction should hit a one-definition-rule violation and fail. The
> first real run proved that a parser bug: a model returned an implementation, then echoed
> the header, then the implementation again, and concatenating the three redefined the
> function and the header's typedef — a duplicate-definition error the model never made.
> `c-basic` scored 0/8, all `compile_error`. Worse, the failure was toolchain-shaped: a
> compiling toolchain rejected the duplicates while an interpreting one silently kept the
> last, so the two sets scored the *same model* differently — breaking the comparability §2
> exists to protect. Last-fence-wins is toolchain-neutral and scores the answer the model
> actually gave; `c-basic` went to 7/8 on the same model, same run. The lesson is the one
> the whole ADR is built on: a rule that manufactures a failure the model did not commit is
> measuring the harness.

### 5. C/C++ is a compiled contract across two translation units

The answer TU and the test TU are compiled **separately** and linked against a
problem-provided header. The tests are never visible to the answer, and the header is the
contract.

**C++ name mangling makes the linker a signature checker.** `vec_push(IntVec*, int)`
mangles to `_Z8vec_pushP6IntVeci`. A model that writes `vec_push(IntVec*, long)` produces a
different symbol, the link fails, and that is a distinct and informative verdict rather
than a mysterious wrong answer. Contract conformance is checked for free by a tool that
cannot be argued with.

Measured on sparky before committing to this (a representative TU with real STL headers,
for the worst case):

| | wall | peak RSS |
|---|---|---|
| `g++ -O0 -std=c++20`, compile + link | 0.35 s | 91 MB |
| `g++ -O1 -fsanitize=address,undefined` | 0.51 s | 128 MB |
| running the sanitized binary | ~0 s | 7 MB |
| `gcc -O0` (plain C) | 0.06 s | 20 MB |

Compilation costs under a second of the 10 s budget and peaks well inside `MemoryMax=512M`.
**No confinement property has to be loosened to add compiled languages**, which was the
main thing that could have made this a bad idea.

Sanitizers inject their own symbols, so the pipeline **analyses a clean object and executes
a separately sanitized build** — two compiles, ~0.9 s, still inside budget.

### 6. Three layers: what it can reach, what it can use, what it may execute

The first draft had one control and called it the boundary. There are three, they answer
different questions, and confusing them is how a benchmark ends up trusting a check that
was never a security control.

| layer | question | mechanism |
|---|---|---|
| **isolation** | what can the code *reach*? | the mount namespace and the confinement properties |
| **scope** | what can the code *use*? | an import gate, or headers that are simply absent |
| **admission** | may this artifact execute *at all*? | analysis of the compiled object, before linking |

#### The finding that forced this

`ProtectSystem=strict` makes the filesystem **read-only, not invisible**, and `/opt/cluster`
is mode 3775. So under the original confinement an answer could read the entire published
tree — the harness, the trend store, and every set's `tests.py` and `reference.py`.

**The hidden tests were readable by the code being tested.** A model need never have seen
them: an answer only has to open the path, parse the expected values and return them, and
the layout is in a public repository. Every other control in this ADR is worth nothing
against that, because the benchmark's entire premise is that the tests are hidden.

Read-only was never the property that mattered. *Absent* is.

#### …and isolation alone does not close it

Adversarial review of the fix found the same leak **inside** the sandbox, where a mount
namespace cannot reach, in both toolchains. The tests must be in there — they have to run —
so the question is only whether the answer can get at them. Both times, it could:

- **Interpreted.** The injected `@weight` decorator is a function defined in the runner's
  own module, so `weight.__globals__['TESTS']` hands the answer the entire test source. An
  answer that returned the right value *only* when it found the test text scored `passed`.
- **Compiled.** The driver writes `tests.c` beside `answer.c` and runs the binary in that
  directory. An answer that returned the right value *only* after `fopen("tests.c")`
  succeeded scored `passed`.

Neither is exotic and neither needs a privilege. So the rule is stronger than "hide the
tests from the model":

> **Nothing handed to the answer may transitively reference the tests, and no artifact
> containing them may outlive the build.** Injected helpers are constructed with empty
> globals; sources are unlinked after compilation and before the binary runs.

That is a *validity* control, not a safety one, and it is why the two concerns stopped being
separable here: an answer that can read the answer key does not need to be malicious to make
the number meaningless.

#### Isolation — an empty root, not a locked one

```
systemd-run --pipe --wait --collect
  --property=DynamicUser=yes           # ephemeral uid, owns nothing
  --property=PrivateNetwork=yes        # cannot reach the endpoint, the panel, or the LAN
  --property=TemporaryFileSystem=/     # start from NOTHING…
  --property=BindReadOnlyPaths=…       # …and mount back only what the toolchain needs
  --property=ProtectHome=yes
  --property=PrivateTmp=yes --property=PrivateDevices=yes
  --property=PrivateUsers=yes
  --property=ProtectProc=invisible --property=ProcSubset=pid
  --property=NoNewPrivileges=yes --property=RestrictSUIDSGID=yes
  --property=RestrictNamespaces=yes --property=LockPersonality=yes
  --property=SystemCallArchitectures=native
  --property=RestrictAddressFamilies=AF_UNIX
  --property=SystemCallFilter=@system-service
  --property=MemoryMax=512M --property=TasksMax=64 --property=RuntimeMaxSec=10
```

`TemporaryFileSystem=/` is the change that matters: the answer starts with an empty root and
receives back only the interpreter or compiler it needs. `/opt`, the repository and the
problem sets are not read-protected — they are **not in the mount namespace at all**.

**Why not a container.** A `docker` grant *is* a root grant:
[ADR-0018](0018-provision-select-split.md) retired it and
[ADR-0019](0019-bounded-image-probe.md) built the bounded probe specifically to avoid
re-opening it. Reversing that to sandbox a benchmark would trade a large hole for a small
one, and per-problem container startup costs more than the code under test takes to run.
systemd provides the same isolation natively, in the invocation that already exists, with no
new grant and no new dependency.

And by *shape*: the job arrives on **stdin** as JSON, and every path, flag and property is
a constant composed here. Nothing the caller sends is ever a path, an argument or a shell
word.

#### Scope — and an asymmetry between compiled and interpreted

Isolation does not answer the scope question, and it is easy to assume it does. Inside an
empty root, an answer still has whatever its language handed it: `import socket` still
succeeds, it simply cannot reach anything. So a rule we state to a model — *"include only
the header you were given"* — is worthless unless something enforces it, and a stated,
unenforced rule is worse than no rule, because the contamination argument in §3 then rests
on the model's cooperation.

The two families are not equally enforceable, and this is a real asymmetry rather than an
implementation gap:

**Compiled languages can be made structural.** Compile with `-nostdinc` and bind only the
compiler's own internal include directory, and `#include <stdio.h>` is not *detected* — it
**cannot resolve**. The rule stops being a check and becomes a property of the environment.
Linking still reaches libc, so an answer that skips the header and writes
`extern int system(const char *);` still links; that is what the admission layer is for.

**Interpreted languages cannot.** The interpreter needs its standard library present in
order to start, so the modules are always reachable. An import scan over the compiled
bytecode finds `IMPORT_NAME` opcodes, including inside nested functions — but
`__import__`, `importlib`, and gadget chains through `object.__subclasses__()` all reach the
same modules without emitting one.

> **The import gate is a rule-compliance check, not a security control.** It measures
> whether a model obeyed a stated constraint, which is a real and interesting signal
> (§7's `rejected`). It must never be described as containment. For interpreted sets the
> boundary is, and remains, the isolation layer.

Writing that down is the point of this section: a future reader who mistakes the import scan
for a sandbox will build on sand.

#### Admission — analysis before execution

**The object gate** exists because confinement only helps *while hostile code is already
running*. Candidate code is compiled to a `.o` and analysed **before it is linked or
executed** — so rejected code never runs at all. That ordering is the point, and it is what
makes an agent-authored or compromised candidate a bounded problem.

Three findings from testing the toolchain on this hardware drove its design:

**A denylist cannot work.** A TU calling `fopen` and `system` exposes them verbatim as
undefined symbols. But `std::ifstream in("/etc/shadow")` opens a file with **no POSIX
symbol anywhere** — it appears only as
`std::basic_ifstream<char, ...>::basic_ifstream(char const*, std::_Ios_Openmode)`. Same
capability, different name. Any denylist is a list of the tricks its author thought of.

**With the std library gone, the allowlist stops being a list.** The same growable-array
implementation compiled `-fno-exceptions -fno-rtti -nostdinc++` against a
problem-supplied header yields, at both `-O0` and `-O2`, an undefined surface of exactly:

```
_Z8pa_allocm   U        // pa_alloc(size_t)
_Z7pa_freePv   U        // pa_free(void*)
```

Both declared by the problem's own header. So the rule is derived rather than maintained:

- **UND ⊆ (the header's declared externals) ∪ (a small fixed toolchain set)** —
  `memcpy`/`memmove`/`memset`/`__stack_chk_*`, which the compiler may emit regardless of
  what the model wrote
- **DEF ⊇ the contract**, by exact mangled name

**The problem header is the security policy.** There is no separate allowlist to drift.

**Raw syscalls carry no symbol, and are still catchable.** An inline `asm volatile("svc #0")`
produced zero undefined symbols; `objdump -d` found `d4000001 svc #0x0` immediately. The
gate therefore scans `.text` for `svc`/`hvc`/`smc` as well, and rejects source-level
`asm`/`__asm__` outright — no algorithmic problem has a legitimate use for either.

Two further rules:

- **Section-size caps.** A model can dodge the allocator with `static int buf[1<<20]` —
  zero allocations, a perfect memory score, passing at small N. The `.bss`/`.data` sizes
  are in the object already, so capping them closes it.
- **The gate is per-objective, not global.** Under a *"fastest, no memory limit"* objective
  (§9) a large static table is legitimate strategy rather than evasion. Same artifact,
  different policy.

**The honest boundary.** This gate reduces scope; it is not containment. It cannot reason
about a computed call through a pointer the answer legitimately holds. The isolation layer
remains the actual boundary and holds regardless of what the analysis missed. What the gate
buys is that the executed surface is one we **enumerated** rather than one we hoped was
safe — and that hostile code is rejected before it ever executes.

**None of this is verified under real confinement yet.** Every property above is asserted
from documentation and from unconfined measurement on this hardware. Whether `gcc` runs
under `SystemCallFilter=@system-service` inside a minimal mount namespace, and which paths
it actually needs bound back, cannot be tested without root — so the first deploy carrying
this is an experiment, and the toolchains must fail loudly rather than silently degrade if a
bind is missing. A build that cannot run must report `no_answer`, never `compile_error`:
attributing our missing mount to the model is precisely the confusion §7 exists to prevent.

### 7. A verdict is a stage, not a boolean

Every stage of the pipeline has its own terminal state, and each is mechanically decidable:

| verdict | stage | what it says about the model |
|---|---|---|
| `no_answer` | extraction | produced nothing usable |
| `declined` | extraction | asserted the problem is impossible (§9) |
| `rejected` | source scan | broke a stated rule — disallowed include, inline asm |
| `compile_error` | compile | did not build |
| `contraband` | object scan | reached outside the header contract, or emitted a syscall |
| `link_error` | link | signature does not match the contract |
| `crashed` | execute | SIGSEGV/SIGABRT or a sanitizer report |
| `timeout` | execute | exceeded budget — infinite loop or bad complexity |
| `failed` | execute | ran cleanly, wrong answer |
| `passed` | — | the only success |

`rejected` and `contraband` are deliberately separate: one is a model that **disobeyed** a
stated rule, the other is a compiled artifact reaching outside its contract despite the
source looking clean. Disobedient and evasive are different findings.

Only `passed` counts toward pass@1. The distribution is the diagnostic, and it is what
gives the set **dynamic range at the floor**: with a 0% pass rate, the verdicts still rank
models strictly — `failed` beats `link_error` beats `compile_error` beats `rejected`. A
high bar means near-zero pass rates until models improve, and this is what keeps the set
useful in that window.

This supersedes the first draft's single "runnable rate". That framing — *not-runnable is
usually our bug* — was right for Python and is wrong here, where a compile failure under
one-attempt rules is a genuine model failure.

#### A budget the answer never fits in is a verdict about us, not the model

The taxonomy only works if each verdict is attributed to the right cause, and one
attribution is currently wrong.

`MAX_TOKENS` has to cover **the thinking and the answer** — a reasoning model emits its
`<think>` block into the same completion. A model that spends the budget reasoning returns
nothing, which scores `no_answer`, which is classified as harness-suspect. So a model
defeated by the cap is indistinguishable from a harness that failed to extract its code,
and on a hard problem the number may be measuring the cap rather than the model.

Two signals would separate them, and both are being discarded:

- **The tokens actually spent.** Not recorded at all — `api.py` sets
  `include_usage: False`.
- **The finish reason.** Fetched and thrown away: `stream_text` returns it and the scorer
  keeps only the text.

So: **record what each answer spent, keep the finish reason, and treat truncation as its
own outcome.** An answer cut off by the cap is a distinct finding from one that produced
nothing — the first says the budget is too small, the second says extraction failed, and
collapsing them hides whichever is really happening.

The cap itself should then be a backstop rather than a variable: set well clear of what any
problem needs, with the recorded spend showing whether that is still true. A number chosen
by guesswork and never measured is how this went wrong in the first place — the comment
calling 4096 "generous" was written without any way to check.

Recording spend has a second use, in [ADR-0025](0025-external-reference.md): it is what
makes thinking comparable across models whose mechanisms for it do not correspond.

### 8. Provided primitives are measurement probes

Because every allocation goes through an interface we implement, we can count it. The
instrumentation is **deterministic and machine-independent** — which is decisive on this
cluster, where a benchmark shares a box with a serving model. Wall-clock timing would be
noise; allocation and copy counts are identical whether the GPU is idle or saturated, and
reproducible months later.

The headline metric is the **empirical exponent**, `log2(metric(2N) / metric(N))`. Measured
against two *correct* growable-array implementations differing only in growth policy:

| policy | N | allocs | copies | total bytes |
|---|---|---|---|---|
| doubling | 1000 → 8000 | 9 → 12 | 1,020 → 8,188 | 8,176 → 65,520 |
| linear | 1000 → 8000 | 1,000 → 8,000 | 499,500 → **31,996,000** | 2,002,000 → **128,016,000** |

Copies ratio 2.00 → **exponent 1.0** for doubling; 4.00 → **exponent 2.0** for linear,
stable across all three doublings. Both answers pass every functional test. **Only the
instrumentation separates them**, and it separates them cleanly from a single measurement.

Two rules:

- **Normalise against the reference solution.** Raw counts are not comparable between
  problems; ratios are. The reference profile was already required to validate the set, so
  this is a second job for an existing artifact.
- **Keep it orthogonal to pass@1.** A quadratic-but-correct answer is `passed`, with its
  exponent reported separately. Folding efficiency into the pass rate yields a headline
  number nobody can interpret.

This is also what gives the set **dynamic range at the ceiling**: when pass@1 eventually
saturates, exponents and allocation ratios keep separating the top of the fleet. HumanEval
died of saturation, not contamination. This set has headroom at both ends.

### 9. Axes that follow, once the above works

Recorded here because they shaped the schema, and because building them cheaply later
depends on decisions taken now.

**Objectives.** The same problem asked with a different directive — *fastest outright*,
*most compact*, *most maintainable* — scored differently, with the contract unchanged. Two
are already objective: fastest is the exponent and counts; compact is `.text` size from the
object, which is deterministic and formatting-proof. Every axis must appear in the scenario
key or scores blend across incomparable conditions.

**Impossible problems, mixed in blind.** A minority of problems that cannot be satisfied,
scored on whether the model recognises it. Decidable, contamination-proof (there is no
solution to memorise), and cheap — no reference solution, no tests, and a declined answer
skips compilation entirely. Two design rules make or break it:

- **Near-misses, not absurdities.** Delete one line from `chunk-evenly` — the rule that
  earlier chunks take the extra elements — and demand exactly-equal chunks, and it becomes
  impossible whenever `n ∤ len(items)` with no famous name to pattern-match. Ship both
  variants as a pair: everything is identical except the resolution rule, so the reasoning
  step under test is isolated.
- **Score it as a confusion matrix, and watch the false-refusal rate.** A model that flags
  everything impossible scores perfectly on the impossible cell and is worthless. The
  metric that matters is false refusals on *solvable* problems.

A sanctioned refusal channel is needed to keep the verdict decidable (`declined`, §7).

**Discrimination, and the generation gap.** Show a model several correct solutions,
unlabelled, and ask which scales best. **This is decidable** — the instrumentation already
established ground truth (exponent 1.0 vs 2.0). A model that correctly identifies the
doubling solution but writes the linear one when asked cold knows something it cannot
deploy, and that **generation/discrimination gap** says directly whether scaffolding or a
self-critique pass would recover real quality. Calibration problems must be dedicated —
never also used for generation scoring, or the model has been handed a worked answer.

**A judge model, for the one axis that is not decidable.** Maintainability has no
mechanical truth, so the founding constraint — *never introduce opinion where truth is
available* — does not forbid a judge there. Comment-to-line ratio is not a substitute; it
is gamed by one verbose header and inverts in practice. If a judge is adopted it needs all
of: a **fixed** judge held constant across the fleet, **blind** to authorship, its identity
in the scenario key, and — before any model judges its own work — a **calibration pass**
against solutions whose quality was measured. That calibration must contain **known-bad as
well as known-good**, or a model that praises everything passes it, which is exactly the
failure being guarded against. Calibration measures competence; blinding measures bias;
both are needed, and the accuracy is worth keeping as a weight rather than collapsing to a
gate.

**Adopting a judge reverses a founding constraint of this ADR and deserves its own.**

## Consequences

- **The scoreboard gains its most decision-relevant column**, and two standing manual
  overrides go away: Coder-Next and Qwen3-VL stop being rows a human must remember to
  argue with.
- **`activator` holds a fourth grant, and the first that executes caller-supplied code.**
  Every previous widening was validated input to a fixed program; this one is not, and it
  should be read as the boundary's most significant change since ADR-0018.
- **Rejection precedes execution.** Unlike a sandbox-only design, a candidate that reaches
  outside its contract never runs — which is what makes agent-authored or compromised
  candidate code a bounded risk rather than a trusted input.
- **The confinement is now load-bearing for correctness, not just safety.** Before the
  isolation layer, an answer could read the hidden tests off disk; a benchmark whose
  answers can read its own answer key measures nothing. Security and validity stopped being
  separate concerns at that point.
- **Scores are ours alone, per set.** No cross-referencing against published leaderboards,
  and no comparing a score from one set against another.
- **`python-basic` measures nothing about the fleet.** It is public and canonical by
  construction. Treating its number as a ranking is a misuse the README must forbid.
- **Problem authoring gets more expensive.** A C/C++ problem needs a header contract, a
  test TU, a reference solution and a reference counter profile. That cost is the reason
  the set starts small and grows against observed failures.
- **Adding a language is a deploy, and the harness stays blind to all of them.** The cost
  is a refactor of working code — the fence regex, the prompt, and the runner all move out
  of the harness — and the benefit is that a set can be added without the measurement layer
  learning anything about it.
- **`sparky test` now compiles.** Set self-validation builds every C/C++ reference
  (~0.35 s each), and the toolchain becomes a declared dependency of the role rather than
  something incidentally present on sparky.
- **Near-zero pass rates are expected, and are not a failure of the instrument.** The
  verdict distribution ranks models before any of them can pass. Report per difficulty
  tier so a bimodal set does not hide the signal behind one blended number.

## Alternatives rejected

**A private set as the contamination defence.** The original proposal. It cannot work in a
public repository, and it was solving the wrong problem: canonical tasks are contaminated
whether or not the file is. Privacy survives as a *deployment* option — a set may be a
submodule — but not as the argument.

**Adopt LiveCodeBench anyway, and note the caveat.** The caveat does not survive contact
with a table. A number in a column gets compared; the footnote explaining that it measures
memorisation does not travel with it.

**Generate problems procedurally each run.** Contamination-proof by construction and
genuinely tempting. Rejected as the *primary* source because template-shaped problems
measure a template-shaped ability, and scores would move when the generator changed rather
than when the models did — which destroys the trend store's only purpose. Worth revisiting
as a supplement once a set has established a baseline.

**Score correctness with a judge model.** The one domain where correctness is mechanically
decidable is the last place to introduce a model's opinion, and the judge would be a fleet
member grading its competitors. Unchanged. §9 permits a judge only for maintainability,
which has no decidable truth, and only under calibration.

**A single translation unit with tests appended.** Simpler, and it was the first proposal.
Rejected because separate compilation makes the linker enforce the contract by mangled
name, keeps the tests invisible to the answer, and turns a signature mismatch into a
distinct verdict.

**Allow the standard library and denylist the dangerous calls.** Falsified by measurement:
`std::ifstream` opens a file with no POSIX symbol in the object. The denylist would have
passed it.

**Wall-clock timing as the efficiency metric.** Meaningless on a box that is concurrently
serving a model. Deterministic counters through provided primitives are reproducible and
hardware-independent.

**Wait for a contamination-free public benchmark to appear.** This has been the position
since ADR-0016, and in the meantime the fleet made two retirement decisions it could not
justify on evidence.

## Staging

Roughly in dependency order; each step is useful on its own.

**Done.**

1. Problem sets as folders; `python-basic` moved, absence handled, per-set scenarios.
2. The toolchain seam — every language detail out of the harness and into the set. Done
   early because everything below assumes the harness is language-blind, and because a
   second language is the only way to know whether it is.
3. The verdict taxonomy, replacing the runnable-rate boolean.
4. Two-TU compilation with the header contract, proved by `c-basic`. Each stage —
   `compile_error`, `link_error`, `crashed`, `timeout`, `failed` — verified to fire and be
   distinguishable.
5. Per-test cases carrying a `weight`, emitted by both toolchains.

6. **Isolation** — an empty root, the toolchain bound back, `/opt` absent. Plus the two
   in-sandbox leaks found by attacking it: injected helpers rebuilt with empty globals, and
   sources unlinked before the binary runs.
7. **Scope** — `-nostdinc` with a bound include path for compiled sets; the bytecode import
   scan for interpreted ones, recorded as rule-compliance rather than containment.
8. **Admission** — undefined symbols checked against the problem's own header, and the
   object disassembled for `svc`/`hvc`/`smc`. Section caps are still to do, and only matter
   once a set instruments memory (step 10).
9. **`--selftest`**, run by the deploy: the isolation properties cannot be verified without
   root, so this is where they stop being an assumption.

**Next, in this order.**

10. **The weighted tally** — the data already flows and is discarded; needs a nullable
   `score` on `store.Run`, which is free while no coding rows exist.
11. Instrumented primitives and the empirical exponent, with the section caps that stop a
    static buffer faking a perfect memory score.
12. Impossible problems (cheapest of the remaining axes; needs only §7's `declined`).
13. Objectives, discrimination, and — separately argued — a judge.

**No model output has been executed through any of this yet.** That is why 6–8 come before
anything that makes the benchmark more useful: the first real run is the moment the
threat model stops being hypothetical.

## References

- [ADR-0016](0016-continuous-evaluation-outer-loop.md) — the measurement loop this fills the
  last gap in
- [ADR-0018](0018-provision-select-split.md) — why a `docker` grant is a root grant
- [ADR-0019](0019-bounded-image-probe.md) — the bounded-program pattern this follows
- [ADR-0009](0009-fail-safe-boot.md) — why a component that fails silently is the worst failure
- `benchmarks/coding/` — the sets, and the rules for writing a problem
- `ansible/roles/activate/files/vllm-sandbox` — the confinement and the object gate
- `sparky/coding.py` — the regiment: what to ask, how to read an answer, what the number means
