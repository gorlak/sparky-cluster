"""The quality regiment's scorer (ADR-0011 Layer 3) — no hardware, no model.

Accuracy is only meaningful if the extraction is right, and extraction is where the
subtle bias lives: too lenient and prose inflates the score, too strict and a correct
answer phrased politely is marked wrong. Both would silently corrupt every model
comparison the regiment exists to make, so the parser is tested against the shapes real
models actually produce.
"""

from __future__ import annotations

import json

import pytest

from sparky.measure.instruments import evals


# --- the committed subset ---------------------------------------------------

def test_subset_is_present_and_well_formed():
    items = evals.load_items(limit=None)
    assert len(items) == 280
    for item in items:
        assert len(item["options"]) <= len(evals.LETTERS)
        assert item["answer"] in evals.LETTERS[:len(item["options"])]
        assert item["question"] and item["category"]


def test_metadata_line_is_not_scored_as_a_question():
    """The first line is provenance. Treating it as an item would poison every run."""
    raw = evals.SUBSET.read_text().splitlines()
    assert json.loads(raw[0]).get("_meta") is True
    assert all(not i.get("_meta") for i in evals.load_items(limit=None))


def test_any_prefix_stays_category_balanced():
    """The file is interleaved so a short run is as representative as a long one — if
    it were grouped by category, `--limit 140` would score only half the domains."""
    for limit in (14, 70, 140):
        cats = {}
        for item in evals.load_items(limit=limit):
            cats[item["category"]] = cats.get(item["category"], 0) + 1
        assert len(cats) == 14, f"limit {limit} covered {len(cats)} categories"
        assert max(cats.values()) - min(cats.values()) <= 1, cats


# --- extraction: the risky part ---------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("C", "C"),
    ("Answer: C", "C"),
    ("answer is C", "C"),
    ("The answer is (C).", "C"),
    ("**Answer:** C", "C"),
    ("Let me think.\n\nAnswer: C", "C"),
    ("...therefore the correct choice.\n\nC.", "C"),
    ("answer: c", "C"),
])
def test_extracts_the_letter_from_real_reply_shapes(text, expected):
    assert evals.extract_answer(text) == expected


def test_the_last_explicit_answer_wins():
    """Reasoning models reconsider out loud: 'A looks right… actually the answer is C'.
    Scoring the first mention would mark a correct final answer wrong."""
    assert evals.extract_answer(
        "Option A looks plausible at first. But on reflection, the answer is C.") == "C"


def test_an_explicit_statement_beats_a_stray_letter():
    """Prose is full of bare capitals. An explicit answer statement must outrank them."""
    assert evals.extract_answer(
        "Vitamin A is a red herring here.\nAnswer: D") == "D"


@pytest.mark.parametrize("text", ["", "   ", "I don't know.",
                                  "This question is ambiguous and I cannot choose."])
def test_returns_none_rather_than_guessing(text):
    """None is reported as unparseable and counted wrong. Guessing would invent
    accuracy — a 10-option question guessed at is 10% free score per unparseable item."""
    assert evals.extract_answer(text) is None


# --- scoring ----------------------------------------------------------------

def _res(pairs):
    return evals.EvalResult(items=[
        evals.ItemResult(id=i, category="math", expected=e, got=g, ok=(g == e), seconds=0.0)
        for i, (e, g) in enumerate(pairs)])


def test_unparseable_answers_count_as_wrong():
    """Otherwise a model that rambles on hard questions outscores one that answers
    clearly — dropping them from the denominator rewards evasion."""
    result = _res([("A", "A"), ("B", None), ("C", "C"), ("D", None)])
    assert result.accuracy == 0.5
    assert result.unparseable == 2


def test_per_category_breakdown():
    result = evals.EvalResult(items=[
        evals.ItemResult(0, "math", "A", "A", True, 0.0),
        evals.ItemResult(1, "math", "B", "C", False, 0.0),
        evals.ItemResult(2, "law", "A", "A", True, 0.0),
    ])
    assert result.by_category() == {"law": (1, 1), "math": (1, 2)}


def test_prompt_lists_options_as_letters():
    item = evals.load_items(limit=1)[0]
    prompt = evals.format_prompt(item)
    assert "\nA. " in prompt and "\nB. " in prompt
    assert item["question"][:40] in prompt


def test_the_token_budget_is_generous():
    """A tight cap scores the budget, not the model — the vision gate learned this the
    expensive way with max_tokens=32 on a reasoning model."""
    assert evals.MAX_TOKENS >= 1024


# --- truncation and the follow-up rescue (2026-08-09) -----------------------

class _Client:
    """Minimal stand-in: first call truncates, the follow-up answers."""

    def __init__(self, first_text, finish, followup_text=None):
        self.first_text, self.finish, self.followup_text = first_text, finish, followup_text
        self.calls = 0

    def chat(self, messages, model, **kw):
        self.calls += 1
        text = self.first_text if self.calls == 1 else (self.followup_text or "")
        return type("R", (), {
            "content": text, "reasoning_content": None, "status_code": 200,
            "raw": {"choices": [{"finish_reason": self.finish if self.calls == 1 else "stop"}]},
        })()


def test_a_truncated_answer_is_rescued_by_the_followup(monkeypatch):
    """3 of 14 questions hit the cap mid-calculation on a real model. The model had
    reasoned but not concluded; scoring that wrong measures our token cap, not it."""
    monkeypatch.setattr(evals, "load_items", lambda limit=None, path=None: [
        {"id": 1, "category": "math", "question": "q", "options": ["x"] * 4, "answer": "C"}])
    client = _Client("long working, no conclusion", "length", followup_text="Answer: C")
    result = evals.run(client, "m", limit=1, concurrency=1)
    item = result.items[0]
    assert item.truncated and item.rescued and item.ok
    assert client.calls == 2


def test_no_followup_when_the_model_answered_normally(monkeypatch):
    """The rescue must not fire on healthy replies — it would double every request."""
    monkeypatch.setattr(evals, "load_items", lambda limit=None, path=None: [
        {"id": 1, "category": "math", "question": "q", "options": ["x"] * 4, "answer": "C"}])
    client = _Client("Answer: C", "stop")
    result = evals.run(client, "m", limit=1, concurrency=1)
    assert client.calls == 1
    assert result.items[0].ok and not result.items[0].truncated


def test_a_failed_rescue_still_counts_as_wrong(monkeypatch):
    """If it cannot state an answer even when asked directly, that is a real failure."""
    monkeypatch.setattr(evals, "load_items", lambda limit=None, path=None: [
        {"id": 1, "category": "math", "question": "q", "options": ["x"] * 4, "answer": "C"}])
    client = _Client("working…", "length", followup_text="I'm not sure.")
    result = evals.run(client, "m", limit=1, concurrency=1)
    item = result.items[0]
    assert item.truncated and not item.rescued and not item.ok
    assert result.unparseable == 1


def test_the_rescue_fires_when_truncation_left_no_text_at_all(monkeypatch):
    """The bug this fixes: a reasoning model that hits the cap without closing its
    </think> block returns EMPTY content — vLLM cannot split an unclosed block. The
    rescue required text to feed back, so it skipped exactly the case it existed for
    (14 of 28 items on 2026-08-09)."""
    monkeypatch.setattr(evals, "load_items", lambda limit=None, path=None: [
        {"id": 1, "category": "math", "question": "q", "options": ["x"] * 4, "answer": "C"}])
    client = _Client("", "length", followup_text="C")
    result = evals.run(client, "m", limit=1, concurrency=1)
    item = result.items[0]
    assert item.truncated and item.rescued and item.ok
    assert client.calls == 2


def test_timeouts_are_counted_separately_from_wrong_answers():
    """41 of MiniMax's 140 items were ReadTimeouts at a flat 600s client ceiling on
    2026-08-09 — scored as wrong, costing ~29 points that were never about the model.
    A harness failure must be visible as one."""
    result = evals.EvalResult(items=[
        evals.ItemResult(0, "math", "A", None, False, 600.0, finish="error:ReadTimeout"),
        evals.ItemResult(1, "math", "B", "B", True, 3.0, finish="stop"),
    ])
    assert result.timed_out == 1
    assert result.unparseable == 1


def test_the_rescue_budget_assumes_nothing_about_thinking_being_disabled():
    """The retry asks for thinking off via `chat_template_kwargs` — a Qwen convention.
    StepFun's Step-3.5 ignores it and kept reasoning, so a 512-token retry truncated on
    all 58 of its truncated items and rescued none. The budget must survive a model that
    does not obey."""
    assert evals.FOLLOWUP_MAX_TOKENS >= 2048


# --- streaming the first attempt (2026-08-10) --------------------------------

class _StreamClient:
    """A reasoning model that blows the cap without closing `</think>`.

    Non-streaming, vLLM hands back EMPTY content for this; streaming yields every delta.
    """

    def __init__(self, streamed, finish="length", followup_text="Answer: C"):
        self.streamed, self.finish, self.followup_text = streamed, finish, followup_text
        self.seen_followup = None

    def stream_text(self, messages, model, **kw):
        if len(messages) > 1:                       # the rescue turn
            self.seen_followup = messages
            return self.followup_text, "stop"
        return self.streamed, self.finish

    def chat(self, messages, model, **kw):
        self.seen_followup = messages
        return type("R", (), {
            "content": self.followup_text, "reasoning_content": None, "status_code": 200,
            "raw": {"choices": [{"finish_reason": "stop"}]}})()


def _one_item(monkeypatch):
    monkeypatch.setattr(evals, "load_items", lambda limit=None, path=None: [
        {"id": 1, "category": "math", "question": "q", "options": ["x"] * 4, "answer": "C"}])


def test_streaming_preserves_reasoning_that_non_streaming_would_drop(monkeypatch):
    """THE FIX: 58 of Step-3.5's 59 truncations returned empty content non-streaming,
    starving the rescue that works 100% of the time when it has text."""
    _one_item(monkeypatch)
    client = _StreamClient("long unclosed <think> reasoning, no verdict")
    result = evals.run(client, "m", limit=1, concurrency=1)
    item = result.items[0]
    assert item.truncated and item.rescued and item.ok
    # the rescue got the model's own working handed back, not a bare re-ask
    assert any(m["role"] == "assistant" for m in client.seen_followup)


def test_the_reply_tail_is_captured_from_the_stream(monkeypatch):
    """Diagnosis depends on it: an aggregate that cannot be audited is not evidence."""
    _one_item(monkeypatch)
    client = _StreamClient("partial working here")
    result = evals.run(client, "m", limit=1, concurrency=1)
    assert "partial working here" in (result.items[0].reply_tail or "")


def test_a_client_without_streaming_still_works(monkeypatch):
    """The fallback keeps the regiment runnable against any client shape."""
    _one_item(monkeypatch)
    client = _Client("Answer: C", "stop")
    assert evals.run(client, "m", limit=1, concurrency=1).items[0].ok


def test_the_rescue_streams_too(monkeypatch):
    """Streaming ONLY the first attempt fixed nothing measurable (2026-08-10): empty
    truncations went 58/59 -> 0/8, and then the rescue returned empty 8 times out of 8.
    A model that reasons past the cap once does it again on the retry, so both call
    sites need the stream."""
    _one_item(monkeypatch)

    class _RescueStreams(_StreamClient):
        def __init__(self):
            super().__init__("unclosed reasoning")
            self.chat_calls = 0

        def chat(self, messages, model, **kw):      # must NOT be reached
            self.chat_calls += 1
            raise AssertionError("rescue fell back to non-streaming chat")

    client = _RescueStreams()
    result = evals.run(client, "m", limit=1, concurrency=1)
    assert client.chat_calls == 0
    assert result.items[0].rescued and result.items[0].ok
