"""Unit tests for the multiturn quality runner (ADR-0012) — injected fake client.

Timing is controlled via `_now` so the TPOT-spike heuristic is deterministic and
the other tests never trip it spuriously (`_now` returns a constant → 0 latency).
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


def _scripted(values):
    it = iter(values)
    return lambda: next(it)


def test_all_clean_conversation_passes():
    client = FakeClient([FakeRes(f"clean answer {i}") for i in range(len(DEFAULT_TURNS))])
    result = run_multiturn(client, "m", _now=lambda: 0.0)
    assert result.ok is True
    assert len(result.turns) == len(DEFAULT_TURNS)
    assert result.failures == []


def test_cjk_bleed_turn_fails():
    responses = [FakeRes("fine") for _ in DEFAULT_TURNS]
    responses[3] = FakeRes("乱码乱码乱码乱码乱码乱码")
    client = FakeClient(responses)
    result = run_multiturn(client, "m", _now=lambda: 0.0)
    assert result.ok is False
    assert len(result.failures) == 1
    assert "cjk_bleed" in result.failures[0].verdict.reasons


def test_runaway_thinking_turn_fails():
    responses = [FakeRes("fine") for _ in DEFAULT_TURNS]
    responses[2] = FakeRes(content="", reasoning_content="think " * 300)
    client = FakeClient(responses)
    result = run_multiturn(client, "m", _now=lambda: 0.0)
    assert result.ok is False
    assert "runaway_thinking" in result.failures[0].verdict.reasons


def test_history_accumulates_across_turns():
    client = FakeClient([FakeRes(f"a{i}") for i in range(len(DEFAULT_TURNS))])
    run_multiturn(client, "m", _now=lambda: 0.0)
    last = client.calls[-1]
    assert len(last) == 2 * len(DEFAULT_TURNS) - 1  # all prior user/assistant pairs + this prompt
    assert last[0]["content"] == DEFAULT_TURNS[0]
    assert last[1]["content"] == "a0"  # first reply threaded back as assistant
    assert last[-1]["content"] == DEFAULT_TURNS[-1]


def test_tpot_spike_turn_fails():
    # turn0: 1000 ms / 10 tokens = 100 ms baseline; turn1: 2000 ms / 1 token = 2000 ms (>10x)
    client = FakeClient([FakeRes("a b c d e f g h i j"), FakeRes("one")])
    now = _scripted([0.0, 1.0, 1.0, 3.0])
    result = run_multiturn(client, "m", turns=("q1", "q2"), _now=now)
    assert result.ok is False
    assert "tpot_spike" in result.turns[1].verdict.reasons
