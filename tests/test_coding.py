"""The coding regiment's scoring logic (ADR-0024, ADR-0011 Layer 3) — no cluster, no sandbox.

Testable without either because execution is injected, which is the same seam the suite
runner uses for regiments. The tests here are about the three things that decide whether
the number means anything: what the model is SHOWN, how its reply is read, and which
stage a failure is attributed to.

**None of these tests name a language either.** The fixture set below declares fence tags
and an answer form the way a real `set.yml` does, so a test that passed only because
Python was hardcoded somewhere would fail here.
"""

from __future__ import annotations

import pathlib

from sparky import coding
from sparky.coding import Verdict

FIXTURE_SET = coding.ProblemSet(
    name="fixture", version="v0", toolchain="fixture-toolchain",
    path=pathlib.Path("/nonexistent"), fence_tags=("alpha", "a"),
    answer_form="the complete thing and nothing else.")


def _problem(**kw):
    base = dict(id="p", track="implement", difficulty="easy",
                prompt="Write `f(x)`.", tests="assert f(1) == 2")
    base.update(kw)
    return coding.Problem(**base)


def _local_exec(code, tests, **kw):
    """A stand-in for the sandbox. Fine in a test — the code is ours.

    Returns one synthetic case so the weighted score has something to weigh; a real
    toolchain reports one row per test it ran.
    """
    scope: dict = {}
    try:
        compiled = compile(code, "<answer>", "exec")
    except SyntaxError as exc:
        return Verdict.COMPILE_ERROR, f"SyntaxError: {exc}", []
    try:
        exec(compiled, scope)
    except Exception as exc:  # noqa: BLE001
        return Verdict.CRASHED, str(exc), []
    try:
        exec(compile(tests, "<tests>", "exec"), scope)
    except AssertionError as exc:
        return Verdict.FAILED, str(exc), [{"test": "all", "weight": 1, "ok": False}]
    except Exception as exc:  # noqa: BLE001
        return Verdict.CRASHED, str(exc), [{"test": "all", "weight": 1, "ok": False}]
    return Verdict.PASSED, "", [{"test": "all", "weight": 1, "ok": True}]


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)

    def stream_text(self, messages, **kw):
        self.last_prompt = messages[-1]["content"]
        return self.replies.pop(0), "stop"


# --- what the model is shown ------------------------------------------------

def test_the_hidden_tests_are_never_shown():
    """The single mistake that would invalidate the whole benchmark: leak the tests into
    the prompt and it measures copying."""
    problem = _problem(tests="assert f(1) == 2  # SECRET")
    assert "SECRET" not in coding.format_prompt(problem, FIXTURE_SET)


def test_a_repair_problem_is_shown_the_code_and_its_failure():
    """Without the failure output it is a rewrite task, not a repair task — and using an
    error message is most of what a coding assistant actually does."""
    text = coding.format_prompt(_problem(
        track="repair", broken="def f(x): return x", failure=">>> f(1)\n1\nexpected: 2"),
        FIXTURE_SET)
    assert "def f(x): return x" in text and "expected: 2" in text


def test_the_answer_form_comes_from_the_set():
    """The instruction that names a language is the SET's text. If this module ever grows
    its own, a set in another language starts being asked for the wrong thing."""
    text = coding.format_prompt(_problem(), FIXTURE_SET)
    assert "the complete thing and nothing else." in text


def test_a_repair_problem_is_fenced_with_the_sets_own_tag():
    text = coding.format_prompt(_problem(track="repair", broken="B", failure="F"),
                                FIXTURE_SET)
    assert "```alpha\nB\n```" in text


# --- reading the reply ------------------------------------------------------

def test_a_fenced_answer_is_unwrapped():
    assert coding.extract_code("Here:\n```alpha\ndef f(x):\n    return x + 1\n```\n",
                               FIXTURE_SET) == "def f(x):\n    return x + 1"


def test_a_fence_tagged_with_an_unlisted_name_is_still_unwrapped():
    """The failure this guards: an unrecognised tag falls through to 'no fence means the
    whole reply', so the fence MARKERS reach the executor and the answer cannot build —
    scoring our parser as the model's inability to code. Asserting the code is present is
    not enough; the markers must be gone."""
    got = coding.extract_code("```zeta\ndef f(x):\n    return x + 1\n```", FIXTURE_SET)
    assert got == "def f(x):\n    return x + 1"
    assert "```" not in got


def test_a_declared_tag_wins_over_an_undeclared_one():
    """Preferring the set's own tags is what stops a sample-output or prose block being
    read as code when the model fenced both."""
    reply = "```text\nnot code\n```\n```alpha\ndef f(x):\n    return x + 1\n```"
    assert coding.extract_code(reply, FIXTURE_SET) == "def f(x):\n    return x + 1"


def test_an_unknown_verdict_costs_one_item_not_the_run():
    """A trigger reporting something this harness does not know must not raise out of the
    worker and lose every other problem."""
    result = coding.run(FakeClient("```alpha\nx\n```"), "m", concurrency=1,
                        problems=[_problem()], pset=FIXTURE_SET,
                        execute=lambda c, t, **kw: ("banana", "from the future", []))
    assert result.items[0].verdict is Verdict.NO_ANSWER
    assert "banana" in result.items[0].detail


def test_an_unfenced_answer_is_taken_whole():
    """Being strict here would score a model's markdown habits, not its code."""
    assert coding.extract_code("def f(x):\n    return x + 1", FIXTURE_SET) \
        == "def f(x):\n    return x + 1"


def test_reasoning_is_stripped_before_the_answer_is_read():
    """A `<think>` block routinely contains a WRONG first attempt, and it must not be read
    as the answer."""
    reply = ("<think>maybe return x, no — x+1</think>\n"
             "```alpha\ndef f(x):\n    return x + 1\n```")
    assert coding.extract_code(reply, FIXTURE_SET) == "def f(x):\n    return x + 1"


def test_the_last_fenced_block_is_the_answer():
    """The model ends with what it stands behind (ADR-0024 §4). A real model returned an
    impl, then echoed the header, then the impl again; concatenating them redefined the
    function and the header's typedef — an ODR error the model never made. The last block
    is the answer."""
    reply = ("```alpha\nDRAFT\n```\n"
             "and here is the header again:\n```alpha\nHEADER\n```\n"
             "final:\n```alpha\ndef f(x):\n    return x + 1\n```")
    assert coding.extract_code(reply, FIXTURE_SET) == "def f(x):\n    return x + 1"


def test_an_unclosed_think_block_yields_no_answer():
    """It reasoned past the cap and never answered. That is a real failure, not something
    to salvage — salvaging it would score fragments of reasoning as code."""
    assert coding.extract_code("<think>thinking and thinking", FIXTURE_SET) == ""


# --- what the number means --------------------------------------------------

def test_each_failure_is_attributed_to_its_own_stage():
    """`failed` and `compile_error` are different findings about a model, and the
    distribution is what ranks models before any of them can pass."""
    client = FakeClient("```alpha\ndef f(x):\n    return x + 1\n```",   # passes
                        "```alpha\ndef f(x):\n    return x\n```",       # wrong
                        "```alpha\ndef f(x:\n```")                      # will not build
    result = coding.run(client, "m", execute=_local_exec, concurrency=1, pset=FIXTURE_SET,
                        problems=[_problem(id="a"), _problem(id="b"), _problem(id="c")])
    assert result.by_verdict() == {Verdict.PASSED: 1, Verdict.FAILED: 1,
                                   Verdict.COMPILE_ERROR: 1}
    assert result.passed == 1
    assert result.accuracy == 1 / 3


def test_no_answer_is_the_only_verdict_that_suspects_the_harness():
    """A compile error is the model's problem. Never getting code at all may well be
    ours, and only that one gates recording."""
    assert coding.HARNESS_SUSPECT == frozenset({Verdict.NO_ANSWER})


def test_an_empty_reply_never_reaches_the_executor():
    """Sending an empty string to a sandbox is a wasted unit start, and 'empty answer' is
    a more useful detail than whatever a shell would say about it."""
    called = []

    def spy(code, tests, **kw):
        called.append(code)
        return Verdict.PASSED, "", []

    result = coding.run(FakeClient(""), "m", concurrency=1, problems=[_problem()],
                        pset=FIXTURE_SET, execute=spy)
    assert not called
    assert result.items[0].verdict is Verdict.NO_ANSWER
    assert result.items[0].detail == "empty answer"


def test_the_set_decides_which_toolchain_executes_an_answer():
    """The harness passes the key through and never interprets it — the whole of ADR-0024
    §2 in one assertion."""
    seen = {}

    def spy(code, tests, **kw):
        seen["toolchain"] = kw.get("toolchain")
        return Verdict.PASSED, "", []

    coding.run(FakeClient("```alpha\nx\n```"), "m", concurrency=1, problems=[_problem()],
               pset=FIXTURE_SET, execute=spy)
    assert seen["toolchain"] == "fixture-toolchain"


def test_a_failed_request_is_a_failed_item_not_a_failed_run():
    """One engine hiccup must not lose the other eleven problems."""
    class Boom:
        def stream_text(self, messages, **kw):
            raise RuntimeError("connection reset")

    result = coding.run(Boom(), "m", execute=_local_exec, concurrency=1,
                        pset=FIXTURE_SET, problems=[_problem()])
    assert result.items[0].verdict is Verdict.NO_ANSWER
    assert "connection reset" in result.items[0].detail


def test_the_score_breaks_down_by_track():
    """`implement` and `repair` are different abilities, and a single number hides a model
    that writes fine code but cannot read an error message."""
    client = FakeClient("```alpha\ndef f(x):\n    return x + 1\n```",
                        "```alpha\ndef f(x):\n    return x\n```")
    result = coding.run(client, "m", execute=_local_exec, concurrency=1, pset=FIXTURE_SET,
                        problems=[
                            _problem(id="a", track="implement"),
                            _problem(id="b", track="repair", broken="x", failure="x")])
    assert result.by_track() == {"implement": (1, 1), "repair": (0, 1)}


# --- sets -------------------------------------------------------------------

def test_a_set_scores_under_its_own_scenario():
    """Two sets are two measurements. Blending them yields a number that changes meaning
    with whichever submodules happen to be checked out."""
    assert FIXTURE_SET.scenario == "coding:fixture@v0"


def test_a_directory_without_a_manifest_is_an_unfetched_submodule(tmp_path):
    """Absence is a first-class state (ADR-0024 §1): a cloner without access to a private
    set must still get a working harness."""
    (tmp_path / "present").mkdir()
    (tmp_path / "present" / "set.yml").write_text(
        "name: present\nversion: v1\ntoolchain: t\nfence_tags: [x]\n")
    (tmp_path / "absent").mkdir()
    assert [s.name for s in coding.discover_sets(tmp_path)] == ["present"]
    assert coding.missing_sets(tmp_path) == ["absent"]


def test_the_manifest_is_not_mistaken_for_a_problem(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "set.yml").write_text("name: s\nversion: v1\ntoolchain: t\n")
    (tmp_path / "s" / "only-problem.yml").write_text(
        "id: only-problem\ntrack: implement\ndifficulty: easy\nprompt: p\ntests: assert 1\n")
    pset = coding.discover_sets(tmp_path)[0]
    assert [p.id for p in coding.load_problems(pset)] == ["only-problem"]


# --- the shipped set --------------------------------------------------------

def test_the_shipped_set_loads_and_hides_its_tests():
    sets = coding.discover_sets()
    assert sets, "no problem set ships with the repo"
    for pset in sets:
        problems = coding.load_problems(pset)
        assert problems, f"{pset.name} has no problems"
        for problem in problems:
            assert problem.tests, f"{problem.id} has no tests"
            assert problem.tests not in coding.format_prompt(problem, pset)


def test_the_harness_never_names_a_language():
    """ADR-0024 §2, enforced rather than trusted. Language belongs to a set folder and to
    the sandbox's toolchain dict; this module coordinates stages and nothing else."""
    source = pathlib.Path(coding.__file__).read_text().lower()
    body = source.split('"""', 2)[-1]      # the module docstring may discuss the rule
    for name in ("python", "cpp", "c++", "javascript", "rust", "golang"):
        assert name not in body, f"{name!r} leaked into the language-neutral harness"


def test_the_weighted_score_grades_by_severity_without_moving_the_bar():
    """pass@1 stays all-or-nothing; the weighted score adds resolution BELOW it. A model
    that satisfies the ordinary cases and misses the severe one has not passed, and saying
    only that discards most of what was measured."""
    def execute(code, tests, **kw):
        return Verdict.FAILED, "", [
            {"test": "basic", "weight": 1, "ok": True},
            {"test": "ordinary", "weight": 2, "ok": True},
            {"test": "severe", "weight": 3, "ok": False},
        ]

    result = coding.run(FakeClient("```alpha\nx\n```"), "m", concurrency=1,
                        problems=[_problem()], pset=FIXTURE_SET, execute=execute)
    assert result.passed == 0            # the bar is unchanged
    assert result.accuracy == 0.0
    assert result.score == 3 / 6         # …and the detail below it survives


def test_a_stage_that_ran_no_cases_weighs_nothing():
    """A compile error is absence of evidence, not evidence of failure. Counting it as
    zero-out-of-something would let one unbuildable answer drag a weighted score down as
    though every case had been tried and lost."""
    def execute(code, tests, **kw):
        return Verdict.COMPILE_ERROR, "SyntaxError", []

    result = coding.run(FakeClient("```alpha\nx\n```"), "m", concurrency=1,
                        problems=[_problem()], pset=FIXTURE_SET, execute=execute)
    assert result.items[0].weight_total == 0
    assert result.score == 0.0
