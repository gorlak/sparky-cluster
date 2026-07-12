"""Unit tests for the multiturn quality runner (ADR-0012) — injected fake client.

The gate applies content-based checks only (CJK bleed, runaway thinking); TPOT is
deferred to a streaming implementation (see multiturn.py), so no timing here.
"""

from dataclasses import dataclass

from sparky.multiturn import DEFAULT_TURNS, run_multiturn


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
