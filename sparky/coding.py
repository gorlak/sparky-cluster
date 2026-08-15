"""The coding regiment — pass@1 against hidden tests, per problem set (ADR-0024).

The measurement the fleet was missing. Everything else on the scoreboard ranks models on
axes the operator does not primarily care about, and the cost of that showed up on
2026-08-11: `fast-tier` declared Qwen3-Coder-Next dominated on every measured axis, and the
verdict had to be manually softened to *park, do not evict* because its one real claim was
unmeasurable here.

**Correctness is decidable, so nothing else scores it.** The answer either passes hidden
tests or it does not — no judge model, no reference-similarity, no rubric.

**This module does not know what a programming language is** (ADR-0024 §2). It decides
which problem to ask, drives the stages, and works out what the numbers mean. What a
language *is* lives in two places and neither is here: a set folder declares its toolchain
and how its answers are fenced, and `vllm-sandbox` holds the fixed dict of toolchains that
can build and run one. A grep for any language name in this file should find nothing.

**Execution is INJECTED**, exactly as regiments are injected into the suite runner — which
is what lets the whole scorer be tested with no sandbox, no grant and no cluster.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

SETS_ROOT = Path(__file__).resolve().parent.parent / "benchmarks" / "coding" / "problems"
# The file that makes a directory a set. Its absence is how an unfetched submodule is told
# apart from a real set, so it is also excluded when globbing for problems.
SET_MANIFEST = "set.yml"
# The same, one level down: a directory holding this file is one problem, and its other
# files are the problem's own — its tests, its reference, the interface it declares.
PROBLEM_MANIFEST = "problem.yml"
# Generous: a `hard` problem with a reasoning model is genuinely slow, and a cap that
# clips thinking measures the cap. `evals` learned this the expensive way.
MAX_TOKENS = 4096
DEFAULT_CONCURRENCY = 4


class Verdict(str, Enum):
    """Where an answer stopped. Every stage of the pipeline has its own terminal state
    (ADR-0024 §7), and each is mechanically decidable.

    A toolchain runs a SUBSET of the stages, so it can only ever produce a subset of these:
    one with no link step never returns `LINK_ERROR`. The taxonomy is the union.
    """

    NO_ANSWER = "no_answer"           # nothing usable came back
    DECLINED = "declined"             # asserted the problem cannot be satisfied
    REJECTED = "rejected"             # broke a stated rule before it was ever built
    COMPILE_ERROR = "compile_error"   # did not build
    CONTRABAND = "contraband"         # built, but reaches outside its contract
    LINK_ERROR = "link_error"         # built, but does not match the contract's signatures
    CRASHED = "crashed"               # ran and died
    TIMEOUT = "timeout"               # ran past its budget
    FAILED = "failed"                 # ran cleanly, wrong answer
    PASSED = "passed"                 # the only success


# The one verdict that is as likely to be our fault as the model's: no code was ever
# obtained. A run made mostly of these is a broken harness, and recording it would put a
# fabricated cell on the scoreboard.
HARNESS_SUSPECT = frozenset({Verdict.NO_ANSWER})


@dataclass(frozen=True)
class ProblemSet:
    """A folder of problems plus the declaration of how to run them.

    `toolchain` is a KEY, never a program — see ADR-0024 §2. A set may arrive as a
    submodule from a repository this harness does not control, so nothing it contains may
    become program text in a privileged path.
    """

    name: str
    version: str
    toolchain: str
    path: Path
    fence_tags: tuple[str, ...] = ()
    answer_form: str = ""
    metrics: tuple[str, ...] = ("correctness",)
    # `draft` means the set is not yet large or balanced enough to rank anything, and the
    # rules a ranking set must meet are not applied to it. Declaring it is how a set says
    # so out loud rather than by being quietly small.
    status: str = "ranking"
    # Declared privacy. The structural check below is the safety net: forgetting to declare
    # it is the dangerous direction, so a set that LOOKS private is treated as private.
    private: bool = False

    @property
    def is_draft(self) -> bool:
        return self.status == "draft"

    @property
    def is_private(self) -> bool:
        """A set whose prompts must not leave the box without an explicit decision.

        A submodule carries a `.git` entry of its own — that is what makes a set private in
        practice, and detecting it structurally means a set that never says so is still
        protected.
        """
        return self.private or (self.path / ".git").exists()

    @property
    def scenario(self) -> str:
        """How the trend store names this set's results.

        Sets are never blended: a score against one is not comparable to a score against
        another, and averaging them would produce a number whose meaning changed depending
        on which submodules happened to be checked out.
        """
        return f"coding:{self.name}@{self.version}"


@dataclass(frozen=True)
class Problem:
    id: str
    track: str            # implement | repair
    difficulty: str
    prompt: str
    tests: str            # HIDDEN — never sent to a model
    broken: str = ""      # repair only: the code to fix
    failure: str = ""     # repair only: the failure it is shown
    # Files the problem supplies to whatever builds the answer, by name. What they ARE is
    # the toolchain's business — this module only carries them across. A set whose answers
    # are built against a declared interface puts that interface here; one whose answers
    # stand alone leaves it empty.
    support: dict[str, str] = field(default_factory=dict)
    # Shown to the model verbatim when present, so it can honour an interface it is
    # required to implement. Named support files are never shown unless listed here.
    shown_support: tuple[str, ...] = ()


@dataclass
class ItemResult:
    problem: str
    track: str
    verdict: Verdict
    seconds: float
    detail: str = ""
    # One row per case the toolchain ran: what it was called, what it is worth, whether it
    # held. Empty when a stage ended before any case could run.
    cases: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASSED

    @property
    def weight_total(self) -> int:
        return sum(int(c.get("weight", 1)) for c in self.cases)

    @property
    def weight_passed(self) -> int:
        return sum(int(c.get("weight", 1)) for c in self.cases if c.get("ok"))


@dataclass
class CodingResult:
    pset: ProblemSet
    items: list[ItemResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for i in self.items if i.passed)

    @property
    def accuracy(self) -> float:
        return self.passed / len(self.items) if self.items else 0.0

    @property
    def no_answer(self) -> int:
        return sum(1 for i in self.items if i.verdict in HARNESS_SUSPECT)

    @property
    def score(self) -> float:
        """Weighted partial credit: the share of test WEIGHT an answer satisfied.

        Reported beside pass@1, never folded into it. A problem passes only when every case
        holds — the bar is unchanged — and this adds resolution BELOW that bar, which is
        what makes a set still rank models when none of them pass. An item whose stage
        ended before any case ran contributes nothing to either side, because there is no
        evidence to weigh rather than evidence of failure.
        """
        total = sum(i.weight_total for i in self.items)
        return sum(i.weight_passed for i in self.items) / total if total else 0.0

    def by_verdict(self) -> dict[Verdict, int]:
        """The distribution, which is the diagnostic. It ranks models even at a 0% pass
        rate — `failed` beats `link_error` beats `compile_error` beats `rejected` — which
        is what keeps a high bar useful before any model can clear it (ADR-0024 §7)."""
        out: dict[Verdict, int] = {}
        for item in self.items:
            out[item.verdict] = out.get(item.verdict, 0) + 1
        return out

    def by_track(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = {}
        for item in self.items:
            slot = out.setdefault(item.track, [0, 0])
            slot[0] += int(item.passed)
            slot[1] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}

    def summary(self) -> str:
        tracks = " · ".join(f"{k} {p}/{n}" for k, (p, n) in sorted(self.by_track().items()))
        other = " · ".join(f"{v.value} {n}" for v, n in sorted(
            self.by_verdict().items(), key=lambda kv: kv[0].value) if v is not Verdict.PASSED)
        return (f"{self.passed}/{len(self.items)} passed ({self.accuracy:.1%}) · "
                f"{tracks}" + (f" · {other}" if other else ""))


def discover_sets(root: Path | None = None) -> list[ProblemSet]:
    """Every set that is PRESENT.

    A directory without a `set.yml` is an unfetched submodule — a cloner without access to
    a private set gets an empty folder — and that is a first-class state, not an error
    (ADR-0024 §1). The caller is expected to REPORT what it found, because a private set
    silently missing would otherwise turn a partial run into a complete-looking one.
    """
    root = root or SETS_ROOT
    if not root.is_dir():
        return []
    out = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest = path / SET_MANIFEST
        if not manifest.is_file():
            continue
        spec = yaml.safe_load(manifest.read_text()) or {}
        out.append(ProblemSet(
            name=spec.get("name") or path.name,
            version=str(spec.get("version") or "v0"),
            toolchain=spec.get("toolchain") or "",
            path=path,
            fence_tags=tuple(spec.get("fence_tags") or ()),
            answer_form=(spec.get("answer_form") or "").strip(),
            metrics=tuple(spec.get("metrics") or ("correctness",)),
            status=str(spec.get("status") or "ranking"),
            private=bool(spec.get("private", False)),
        ))
    return out


def missing_sets(root: Path | None = None) -> list[str]:
    """Directories that exist but hold no manifest — an absent submodule, named so a run
    can say what it could not measure."""
    root = root or SETS_ROOT
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not (p / SET_MANIFEST).is_file())


def _problem_from(spec: dict, ident: str, sidecars: dict[str, str]) -> Problem:
    """One problem from its declaration plus whatever files sit beside it.

    A field may be written inline or supplied as a sidecar file — `tests` in the manifest,
    or a `tests.*` file next to it. The file wins, and it is the form a real set should use:
    code belongs in a file its editor, linter and formatter understand.
    """
    def pick(field_name: str) -> str:
        for name, body in sidecars.items():
            if name.rsplit(".", 1)[0] == field_name:
                return body.strip()
        return (spec.get(field_name) or "").strip()

    reserved = {"tests", "reference", "broken"}
    # What the problem permits its answer to reach, forwarded under a reserved key. The
    # toolchain decides what a constraint MEANS; this module only carries it, which is what
    # keeps the rule out of the language-neutral layer.
    constraints = {f"constraints.{k}": ",".join(str(x) for x in v) if isinstance(v, list)
                   else str(v)
                   for k, v in (spec.get("constraints") or {}).items()}
    return Problem(
        id=spec.get("id") or ident,
        track=spec.get("track", "implement"),
        difficulty=spec.get("difficulty", "medium"),
        prompt=(spec.get("prompt") or "").strip(),
        tests=pick("tests"),
        broken=pick("broken"),
        failure=(spec.get("failure") or "").strip(),
        support={**{n: b for n, b in sidecars.items()
                    if n.rsplit(".", 1)[0] not in reserved}, **constraints},
        shown_support=tuple(spec.get("shown_support") or ()),
    )


def load_problems(pset: ProblemSet) -> list[Problem]:
    """Problems may be a single file or a directory.

    A directory keeps code in files rather than inside a manifest string, which is what
    lets an editor, a linter and a formatter see it — and what lets a problem carry an
    interface the answer is built against.
    """
    out = []
    for path in sorted(pset.path.iterdir()):
        if path.is_dir():
            manifest = path / PROBLEM_MANIFEST
            if not manifest.is_file():
                continue
            sidecars = {p.name: p.read_text() for p in sorted(path.iterdir())
                        if p.is_file() and p.name != PROBLEM_MANIFEST}
            out.append(_problem_from(yaml.safe_load(manifest.read_text()) or {},
                                     path.name, sidecars))
        elif path.suffix == ".yml" and path.name != SET_MANIFEST:
            out.append(_problem_from(yaml.safe_load(path.read_text()) or {},
                                     path.stem, {}))
    return out


def format_prompt(problem: Problem, pset: ProblemSet) -> str:
    """What the model sees. Never the tests.

    The answer form comes from the set, so the instruction naming a language is the set's
    text rather than this module's. The fence used to quote a repair problem's broken code
    is the set's first declared tag, for the same reason.
    """
    parts = [problem.prompt]
    for name in problem.shown_support:
        if name in problem.support:
            parts += [f"\n`{name}`:\n",
                      f"```\n{problem.support[name].strip()}\n```"]
    if problem.track == "repair":
        tag = pset.fence_tags[0] if pset.fence_tags else ""
        parts += ["\nThe current implementation:\n",
                  f"```{tag}\n{problem.broken}\n```",
                  "\nHow it fails:\n", f"```\n{problem.failure}\n```"]
    if pset.answer_form:
        parts.append(f"\nReply with {pset.answer_form}")
    return "\n".join(parts)


_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


# Every fenced block, with its tag captured. The tag is filtered below rather than built
# into the pattern: an optional tag inside the regex also matches a CLOSING fence, which
# consumes the next block's opening delimiter and desynchronises the whole scan.
_FENCE = re.compile(r"```([A-Za-z0-9_+.#-]*)[ \t]*\n(.*?)```", re.DOTALL)


def extract_code(text: str, pset: ProblemSet) -> str:
    """Pull the answer out of whatever the model wrapped it in.

    Forgiving about PACKAGING and strict about content: a model that fences its code, or
    narrates first, or reasons in `<think>`, is not worse at coding, and scoring it as such
    measures our parser. What it is NOT forgiving about is inventing code that was not
    there — no fence means the whole reply.

    **The LAST fenced block is the answer** (ADR-0024 §4). A model ends with its final
    offering: it reasons, echoes the header it was given, drafts, corrects, and closes with
    the code it stands behind. Taking that last block reads the model the way a human does.

    This reverses an earlier "unity build" rule that concatenated every block. Measured
    against a real model, unity was a parser bug wearing a design: an answer came back as
    impl + echoed-interface + impl, and concatenating the three redefined the interface and
    the function — a duplicate-definition error the model never made. Worse, the failure was
    toolchain-shaped: a compiling toolchain rejected the duplicate definitions while an
    interpreting one silently kept the last, so two sets scored the same model differently.
    Last-fence-wins does not depend on the toolchain and scores the answer the model gave.
    """
    if not text:
        return ""
    text = _THINK.sub("", text)               # drop reasoning, keep the answer
    blocks = _FENCE.findall(text)
    declared = {t.lower() for t in pset.fence_tags}
    # Blocks the set claims, or untagged — a tagged `text`/sample-output block is not the
    # answer. Fall back to every block for a model that used an unlisted dialect name.
    chosen = [body.strip() for tag, body in blocks
              if (not tag or tag.lower() in declared) and body.strip()]
    if not chosen:
        chosen = [body.strip() for _tag, body in blocks if body.strip()]
    return chosen[-1] if chosen else text.strip()


def run(client, model: str, *, execute, pset: ProblemSet,
        problems: list[Problem] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY, on_item=None) -> CodingResult:
    """Score `model` on one set.

    `execute(code, tests, toolchain=…) -> (verdict, detail, cases)`.

    Concurrent for the same reason `evals` is: a dozen problems at reasoning-model speed
    is otherwise long enough that the regiment gets skipped, and a regiment that gets
    skipped measures nothing.
    """
    problems = problems if problems is not None else load_problems(pset)
    started = time.monotonic()

    def one(problem: Problem) -> ItemResult:
        t0 = time.monotonic()
        messages = [{"role": "user", "content": format_prompt(problem, pset)}]
        try:
            if hasattr(client, "stream_text"):
                # Streamed, like the quality eval: a reasoning model that hits the cap
                # without closing `</think>` returns EMPTY content non-streaming, so the
                # answer is unreachable even though the tokens exist.
                text, _finish = client.stream_text(messages, model=model,
                                                   max_tokens=MAX_TOKENS, temperature=0.0)
            else:
                reply = client.chat(messages, model=model, max_tokens=MAX_TOKENS,
                                    temperature=0.0)
                text = reply.content or reply.reasoning_content or ""
        except Exception as exc:  # noqa: BLE001 - one bad request is a failed item
            return ItemResult(problem.id, problem.track, Verdict.NO_ANSWER,
                              time.monotonic() - t0, f"request failed: {exc}")
        code = extract_code(text, pset)
        if not code:
            return ItemResult(problem.id, problem.track, Verdict.NO_ANSWER,
                              time.monotonic() - t0, "empty answer")
        verdict, detail, cases = execute(code, problem.tests, toolchain=pset.toolchain,
                                        support=problem.support)
        try:
            verdict = Verdict(verdict)
        except ValueError:
            # A toolchain that reports something this harness does not know costs one item,
            # not the run — the same rule as a failed request. Said in the detail so a
            # mismatched trigger is diagnosable rather than silently scored as a failure.
            detail = f"unknown verdict {verdict!r}: {detail}"
            verdict = Verdict.NO_ANSWER
        result = ItemResult(problem.id, problem.track, verdict,
                            time.monotonic() - t0, detail, list(cases or []))
        if on_item:
            on_item(result)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        items = list(pool.map(one, problems))
    return CodingResult(pset=pset, items=items, seconds=time.monotonic() - started)
