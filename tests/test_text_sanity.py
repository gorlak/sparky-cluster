"""Unit tests for the text-sanity check (ADR-0012) — heuristics + the multiturn runner.

The two used to be `quality.py` and `multiturn.py`; ADR-0027 merged them into one
verify-tier module (`text_sanity`), since the conversation is only the heuristics applied
turn by turn. The gate applies content-based checks only (CJK bleed, runaway thinking);
TPOT is deferred to a streaming implementation, so no timing here.
"""

from dataclasses import dataclass

from sparky.verify.text_sanity import (
    DEFAULT_TURNS,
    check_turn,
    is_cjk_bleed,
    non_latin_ratio,
    run_multiturn,
    runaway_thinking,
    tpot_spike,
)


# --- the heuristics (was test_quality.py) -----------------------------------------------

def test_non_latin_ratio():
    assert non_latin_ratio("hello world") == 0.0
    assert non_latin_ratio("") == 0.0
    assert non_latin_ratio("你好世界") == 1.0
    assert 0.4 < non_latin_ratio("hi 你好") < 0.6  # punctuation/space don't count


def test_is_cjk_bleed():
    assert is_cjk_bleed("The answer is 42.") is False
    assert is_cjk_bleed("答案是四十二这是中文乱码输出") is True
    assert is_cjk_bleed("") is False
    assert is_cjk_bleed("1234 !!! ???") is False  # non-alpha ignored


def test_runaway_thinking_inline_tags():
    assert runaway_thinking("<think> " + "word " * 300) is True
    assert runaway_thinking("<think> short </think> the answer") is False
    assert runaway_thinking("a normal answer") is False


def test_runaway_thinking_reasoning_parser():
    assert runaway_thinking(content="", reasoning_content="think " * 300) is True
    assert runaway_thinking(content="the answer", reasoning_content="think " * 300) is False
    assert runaway_thinking(content="", reasoning_content="brief thought") is False


def test_tpot_spike():
    assert tpot_spike(110, 10) is True   # 11x baseline
    assert tpot_spike(20, 10) is False   # 2x
    assert tpot_spike(100, 0) is False   # no baseline


def test_check_turn_clean():
    assert check_turn("A normal English answer.").ok is True


def test_check_turn_flags_multiple():
    v = check_turn("乱码乱码乱码乱码乱码", itl_ms=200, baseline_itl_ms=10)
    assert v.ok is False
    assert set(v.reasons) == {"cjk_bleed", "tpot_spike"}


def test_check_turn_runaway_only():
    v = check_turn("<think> " + "x " * 300)
    assert v.ok is False
    assert v.reasons == ["runaway_thinking"]


# --- the conversation (was test_multiturn.py) -------------------------------------------

@dataclass
class FakeRes:
    content: str
    reasoning_content: str | None = None


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat(self, messages, model, **kw):
        self.calls.append([dict(m) for m in messages])  # snapshot (run mutates the list)
        return self._responses.pop(0)


def test_all_clean_conversation_passes():
    client = FakeClient([FakeRes(f"clean answer {i}") for i in range(len(DEFAULT_TURNS))])
    result = run_multiturn(client, "m")
    assert result.ok is True
    assert len(result.turns) == len(DEFAULT_TURNS)
    assert result.failures == []


def test_short_clean_replies_pass():
    # Regression: a terse reply (e.g. the closing "done") must not fail. A latency
    # proxy once flagged these as TPOT spikes — a false positive on a healthy engine.
    client = FakeClient([FakeRes(r) for r in ["A two-node cluster serves big models.",
                                              "42", "red, green, blue", "Tuesday", "done"]])
    result = run_multiturn(client, "m", turns=DEFAULT_TURNS[:5])
    assert result.ok is True


def test_cjk_bleed_turn_fails():
    responses = [FakeRes("fine") for _ in DEFAULT_TURNS]
    responses[3] = FakeRes("乱码乱码乱码乱码乱码乱码")
    client = FakeClient(responses)
    result = run_multiturn(client, "m")
    assert result.ok is False
    assert len(result.failures) == 1
    assert "cjk_bleed" in result.failures[0].verdict.reasons


def test_runaway_thinking_turn_fails():
    responses = [FakeRes("fine") for _ in DEFAULT_TURNS]
    responses[2] = FakeRes(content="", reasoning_content="think " * 300)
    client = FakeClient(responses)
    result = run_multiturn(client, "m")
    assert result.ok is False
    assert "runaway_thinking" in result.failures[0].verdict.reasons


def test_history_accumulates_across_turns():
    client = FakeClient([FakeRes(f"a{i}") for i in range(len(DEFAULT_TURNS))])
    run_multiturn(client, "m")
    last = client.calls[-1]
    assert len(last) == 2 * len(DEFAULT_TURNS) - 1  # all prior user/assistant pairs + this prompt
    assert last[0]["content"] == DEFAULT_TURNS[0]
    assert last[1]["content"] == "a0"  # first reply threaded back as assistant
    assert last[-1]["content"] == DEFAULT_TURNS[-1]
