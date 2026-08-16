"""Every problem set validates ITSELF (ADR-0024, ADR-0011 Layer 1).

A benchmark whose tests are wrong is worse than no benchmark: every model scores zero and
the number reads as a verdict on the models. So a set is only allowed to rank anything
once it has been proved solvable.

Two properties, and the second is the one that catches sloppy problems:

  1. every problem's hidden tests PASS against its reference solution;
  2. every `repair` problem's BROKEN code FAILS them — a repair task whose starting point
     already passes is not a task, and it scores luck as skill.

(2) fired the moment it was written: `repair-off-by-one` was authored with a stated
failure the broken code handles correctly.

**Validation runs through the same toolchain that scores a real answer**, because a
reference IS an answer — the only one whose verdict we already know. That keeps this file
language-neutral: it never execs anything itself, so a C set validates by exactly this
code path. Confinement is skipped (no root, no systemd in a unit test) but the program
text is the one `vllm-sandbox` would build, so the toolchain itself is under test too.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess

import pytest
import yaml

from sparky.measure.instruments import coding

SANDBOX_SRC = (pathlib.Path(__file__).resolve().parent.parent / "ansible" / "roles" /
               "activate" / "files" / "vllm-sandbox")


def _sandbox():
    """The trigger, imported as a module. It is a program, not a package — but its
    decisions are pure functions, which is what makes them testable with no grant."""
    spec = importlib.util.spec_from_loader(
        "vllm_sandbox", importlib.machinery.SourceFileLoader("vllm_sandbox", str(SANDBOX_SRC)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execute(code: str, tests: str, *, toolchain: str,
             support: dict | None = None) -> tuple[str, str]:
    """Build and run exactly what the sandbox would, minus the confinement."""
    sandbox = _sandbox()
    if toolchain not in sandbox.TOOLCHAINS:
        pytest.skip(f"no toolchain {toolchain!r} in this build")
    program = sandbox.build_program(toolchain, code, tests, support or {})
    done = subprocess.run(sandbox.TOOLCHAINS[toolchain]["argv"], input=program,
                          capture_output=True, text=True, timeout=60)
    verdict = sandbox.read_verdict(done.stdout)
    return verdict.get("verdict", "no_answer"), verdict.get("detail", "")


def _cases():
    """(set, problem, reference-text) for every problem of every PRESENT set."""
    out = []
    for pset in coding.discover_sets():
        for problem in coding.load_problems(pset):
            refs = (sorted((pset.path / "reference").glob(f"{problem.id}.*"))
                    or sorted((pset.path / problem.id).glob("reference.*")))
            out.append(pytest.param(pset, problem, refs[0] if refs else None,
                                    id=f"{pset.name}:{problem.id}"))
    return out


CASES = _cases()


def test_at_least_one_set_ships_with_the_repo():
    """A clone with no sets is a harness nobody can run. Private sets may be absent; the
    example set may not."""
    assert coding.discover_sets(), "no problem set found"


@pytest.mark.parametrize("pset,problem,reference", CASES)
def test_the_reference_passes_the_hidden_tests(pset, problem, reference):
    """If this fails, the PROBLEM is wrong, not the model."""
    assert problem.track in ("implement", "repair")
    assert problem.difficulty in ("easy", "medium", "hard")
    assert problem.prompt, "no prompt"
    assert problem.tests, "no tests"
    assert reference is not None, f"{problem.id} has no reference/{problem.id}.*"
    verdict, detail = _execute(reference.read_text(), problem.tests,
                               toolchain=pset.toolchain, support=problem.support)
    assert verdict == "passed", f"{problem.id}: reference did not pass — {verdict} {detail}"


@pytest.mark.parametrize("pset,problem,reference",
                         [c for c in CASES if c.values[1].track == "repair"])
def test_the_broken_code_actually_fails(pset, problem, reference):
    """A repair problem whose starting point already passes measures nothing."""
    assert problem.broken, "a repair problem needs `broken:`"
    assert problem.failure, "…and the failure output the model is given"
    verdict, _ = _execute(problem.broken, problem.tests, toolchain=pset.toolchain,
                          support=problem.support)
    assert verdict != "passed", f"{problem.id}: the broken code already passes"


@pytest.mark.parametrize("pset", coding.discover_sets(), ids=lambda s: s.name)
def test_a_set_is_big_enough_and_balanced(pset):
    """Too few problems and the score is noise; all one track and it measures half the
    ability. Deliberately loose — this catches a set that has drifted, not one that is
    merely small on purpose."""
    if pset.is_draft:
        pytest.skip(f"{pset.name} is a draft — it does not claim to rank anything")
    problems = coding.load_problems(pset)
    assert len(problems) >= 8, "fewer than 8 problems is noise, not a measurement"
    tracks = {t: sum(1 for p in problems if p.track == t)
              for t in ("implement", "repair")}
    assert min(tracks.values()) >= 3, f"tracks are lopsided: {tracks}"
    assert len({p.difficulty for p in problems}) >= 2, "no difficulty spread"


@pytest.mark.parametrize("pset", coding.discover_sets(), ids=lambda s: s.name)
def test_a_set_declares_what_it_needs(pset):
    """A set that names no toolchain cannot be run, and one with no fence tags reads every
    reply as raw code. Both fail as a wall of zeroes rather than as an error."""
    assert pset.toolchain, f"{pset.name} declares no toolchain"
    assert pset.fence_tags, f"{pset.name} declares no fence tags"
    assert pset.answer_form, f"{pset.name} declares no answer form"
    assert pset.version, f"{pset.name} declares no version"


@pytest.mark.parametrize("pset", coding.discover_sets(), ids=lambda s: s.name)
def test_problem_ids_are_unique_and_match_their_filenames(pset):
    """The trend store records the id; a mismatch makes a result untraceable."""
    ids = []
    for path in sorted(pset.path.iterdir()):
        if path.is_dir() and (path / coding.PROBLEM_MANIFEST).is_file():
            spec = yaml.safe_load((path / coding.PROBLEM_MANIFEST).read_text())
        elif path.suffix == ".yml" and path.name != coding.SET_MANIFEST:
            spec = yaml.safe_load(path.read_text())
        else:
            continue
        assert spec["id"] == path.stem, f"{path.name}: id does not match its directory"
        ids.append(spec["id"])
    assert ids, f"{pset.name} has no problems"
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("pset,problem,reference", CASES)
def test_the_hidden_tests_are_never_in_the_prompt(pset, problem, reference):
    """The one mistake that would silently invalidate everything: leak the tests into the
    text the model sees, and the benchmark measures copying."""
    shown = coding.format_prompt(problem, pset)
    for line in problem.tests.splitlines():
        line = line.strip()
        if line.startswith("assert ") and len(line) > 30:
            assert line not in shown, f"{problem.id}: a hidden test appears in the prompt"
