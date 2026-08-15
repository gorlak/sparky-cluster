"""The external reference (ADR-0024, ADR-0011 Layer 3) — no network, no key, no cluster.

A yardstick answers two questions the fleet cannot ask itself: how far from the ceiling its
scores are, and whether the SET is any good — a strong model scoring near zero means the
problems are ambiguous rather than hard.

The tests that matter most here are not about the API. They are about the boundary: the
cluster must hold no credential, and nothing it deploys or serves may be able to invoke
this.
"""

from __future__ import annotations

import ast
import pathlib
import re

import httpx
import pytest

from sparky import coding, reference
from sparky.coding import Verdict

REPO = pathlib.Path(__file__).resolve().parent.parent


def _client(handler) -> reference.AnthropicClient:
    transport = httpx.MockTransport(handler)
    return reference.AnthropicClient("test-key", client=httpx.Client(transport=transport))


# --- the boundary -----------------------------------------------------------------

def test_a_run_cannot_reach_the_reference():
    """The reference lives on the same command as the local measurement, so what keeps a
    suite local is the regiment PASSING `via="local"` rather than the file layout
    (ADR-0025 §5). A suite that could reach an external service would inherit its outages
    and its bill, and the reference is a constant that does not belong in a per-model run.

    Also asserts every parameter is passed: an unpassed typer option stays an `OptionInfo`
    and fails the first time it is used as the value it claims to be.
    """
    body = (REPO / "sparky" / "cli.py").read_text()
    call = re.search(r"def _coding\(job\).*?\n\n", body, re.DOTALL)
    assert call, "the coding regiment is gone or was renamed"
    text = call.group(0)
    assert 'via="local"' in text, "the suite regiment could reach an external service"
    for param in ("label=", "only=", "model=", "publish_prompts=", "concurrency=", "record="):
        assert param in text, f"the regiment leaves {param} as an OptionInfo"


def test_a_private_sets_prompts_are_not_published_by_accident():
    """A set carried by a submodule is precisely the asset that was kept private, and its
    PROMPTS are what an external call would send. Refusal is the default; publishing is a
    flag someone has to type."""
    body = (REPO / "sparky" / "cli.py").read_text()
    assert "refusing to send private prompts" in body
    assert "publish_prompts" in body


def test_a_submodule_set_is_private_even_if_it_never_says_so(tmp_path):
    """Forgetting to declare it is the dangerous direction, so privacy is detected
    structurally: a submodule carries a `.git` entry of its own."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "set.yml").write_text("name: sub\nversion: v1\ntoolchain: t\n")
    (tmp_path / "sub" / ".git").write_text("gitdir: ../../.git/modules/sub\n")
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "set.yml").write_text("name: pub\nversion: v1\ntoolchain: t\n")
    found = {s.name: s for s in coding.discover_sets(tmp_path)}
    assert found["sub"].is_private is True
    assert found["pub"].is_private is False


def test_the_key_is_never_read_from_a_file():
    """It lives in the environment of whoever ran the script. Any file — under
    /opt/cluster or anywhere else — is a credential at rest that a backup or a disk image
    carries off the box.

    Asserted as "opens nothing at all" rather than "mentions no path", because the module
    legitimately NAMES /opt/cluster in the message explaining that it never uses it.
    """
    tree = ast.parse((REPO / "sparky" / "reference.py").read_text())
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "open" not in called, "the reference module opens a file"
    assert "Path" not in called, "the reference module names a path"
    assert "ANTHROPIC_API_KEY" in (REPO / "sparky" / "reference.py").read_text()


def test_a_missing_key_explains_itself_rather_than_prompting(monkeypatch):
    """This runs unattended as often as not; a prompt that never returns is worse than a
    message naming the variable to set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(reference.MissingKey) as exc:
        reference.api_key()
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_a_reference_row_is_not_a_fleet_member():
    """The scoreboard exists to decide which of OUR models should serve. A yardstick
    cannot serve, cannot be activated and must never draw a retirement verdict."""
    assert reference.REFERENCE_PROFILE == "reference"


# --- the client -------------------------------------------------------------------

def test_it_speaks_the_messages_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}],
                                         "stop_reason": "end_turn"})

    text, stop = _client(handler).stream_text(
        [{"role": "user", "content": "hi"}], model="m", max_tokens=16)
    assert (text, stop) == ("hello", "end_turn")
    assert seen["url"] == reference.API_URL
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["headers"]["anthropic-version"] == reference.API_VERSION


def test_several_text_blocks_are_joined():
    """A reply arrives as a list of blocks. Taking only the first would silently truncate
    an answer and score it as unbuildable."""
    def handler(request):
        return httpx.Response(200, json={"content": [
            {"type": "text", "text": "def f(x):\n"},
            {"type": "thinking", "thinking": "ignore me"},
            {"type": "text", "text": "    return x + 1"}]})

    text, _ = _client(handler).stream_text([{"role": "user", "content": "hi"}],
                                           model="m", max_tokens=16)
    assert text == "def f(x):\n    return x + 1"
    assert "ignore me" not in text


def test_an_error_keeps_the_reason():
    """A bad key, a rate limit and an unknown model are different problems, and collapsing
    them to 'request failed' costs an hour to re-diagnose."""
    def handler(request):
        return httpx.Response(429, text='{"error": {"message": "rate limited"}}')

    with pytest.raises(RuntimeError, match="429"):
        _client(handler).stream_text([{"role": "user", "content": "hi"}],
                                     model="m", max_tokens=16)


# --- it is the same measurement ---------------------------------------------------

def test_the_reference_is_scored_by_the_same_path_as_the_fleet():
    """Not a parallel implementation: same prompts, same hidden tests, same sandbox, same
    verdicts. A yardstick measured differently from the thing it calibrates is not a
    yardstick."""
    pset = coding.ProblemSet(name="fixture", version="v0", toolchain="t",
                             path=pathlib.Path("/nonexistent"), fence_tags=("alpha",),
                             answer_form="code only.")

    def handler(request):
        return httpx.Response(200, json={"content": [
            {"type": "text", "text": "```alpha\ndef f(x):\n    return x + 1\n```"}]})

    def execute(code, tests, **kw):
        return Verdict.PASSED, "", [{"test": "t", "weight": 2, "ok": True}]

    result = coding.run(_client(handler), "claude-x", execute=execute, pset=pset,
                        concurrency=1, problems=[coding.Problem(
                            id="p", track="implement", difficulty="easy",
                            prompt="Write f.", tests="assert f(1) == 2")])
    assert result.passed == 1
    assert result.score == 1.0
