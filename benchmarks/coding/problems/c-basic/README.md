# `c-basic` — the language-neutrality proof

Two problems, and its first job is not measurement. It exists to test a claim: that the
harness does not know what a language is.

The claim held, but not as "nothing changed". Adding this set needed three additions to
`sparky/coding.py` — problems that are a directory rather than one file, files a problem
supplies alongside its tests, and a `draft` status — and **not one of them names a
language**. That is the actual property, it is pinned by a test that greps the module for
language names, and stating it as "zero lines changed" would have been the easier and less
true version.

What the set itself contributes is a `set.yml`, a toolchain entry in `vllm-sandbox`, and
the problems.

## What an answer may reach

Three layers, and they answer different questions (ADR-0024 §6):

- **`-nostdinc`** with only the compiler's own include directory bound, so
  `#include <stdio.h>` cannot RESOLVE — the rule stated in `set.yml` is a property of the
  environment rather than something detected afterwards.
- **The admission gate.** `-nostdinc` stops an include; it does not stop a hand-written
  `extern int system(const char *);`, and linking still reaches libc. So the answer's
  object is listed with `nm` before it is linked, and any undefined symbol not named by the
  problem's own header — beyond what the compiler emits on its own — is `contraband`. A raw
  `svc` carries no symbol at all, so the object is disassembled too.
- **Isolation.** An empty root with only the toolchain bound back, so `/opt/cluster` and
  every set's hidden tests are not merely unreadable but absent.

The sources are also unlinked after compiling and before the binary runs: `fopen("tests.c")`
was a working read of the hidden tests from inside the sandbox until it wasn't.

`status: draft`, so it is exempt from the size and balance rules a ranking set must meet.
A two-problem set cannot rank anything and must not pretend to.

## What makes a C problem different

**No standard library.** The answer may include only the header the problem declares. That
is the contamination defence: "implement this against *this* interface" has no crawled
answer, unlike the canonical exercises in [`python-basic`](../python-basic/). It is also
the security posture — with no stdlib, an answer's undefined-symbol surface collapses to
what its header declares (ADR-0024 §6).

**Two translation units.** The answer and the tests compile separately and link. The linker
therefore checks the contract: a signature that does not match the header produces a
distinct `link_error` rather than a mysterious wrong answer, because a C symbol encodes
what the function is called and — via the header both sides include — what it must accept.

**The harness owns `main`.** A problem writes `run_tests()` and calls `report(name, weight,
ok)` per case; the toolchain supplies `main` and the reporting shape. An answer cannot
define `main`, and cannot forge a result.

## Layout

```
<problem-id>/
  problem.yml    id, track, difficulty, prompt, and which support files the model is shown
  problem.h      the interface — the ONLY thing the answer may include
  tests.c        hidden cases; each calls report(name, weight, ok)
  reference.c    the known-good answer, proved to pass before the set may rank anything
```

`weight` is carried per test so severity can be tallied rather than every assertion
counting the same. Today the toolchain aggregates it to one verdict; the per-test tally is
the next step, and the data is already flowing.
