"""Text sanity — the verify-tier check that a live engine writes coherent prose (ADR-0012).

A basic check, not a measurement: it asks "is the output garbage", not "how good is this
model". Two halves, merged here because the second is the first applied turn by turn:

- **the heuristics** (`check_turn`) — the specific catastrophic failure modes seen on this
  cluster: CJK bleed, runaway (never-ending) thinking, inter-token-latency spikes.
  Deliberately conservative / false-negative tolerant: they flag garbage, not mild
  degradation. They inspect `reasoning_content` when a reasoning-parser engine splits it
  out, and fall back to scanning `content` for literal `<think>` tags otherwise.

- **the conversation** (`run_multiturn`) — drives a fixed, benign English conversation and
  applies the heuristics per turn, holding chat history across turns. ~8 turns, ~2 minutes
  against a live engine. The client is injected, so the logic is unit-tested with no
  hardware. This catches the exact failure class that shipped on `step-3.5-flash-fp8`
  (Nth-turn garbage / nonstop thinking) — a throughput benchmark would pass it blind.

**TPOT-spike detection is deliberately not applied in the conversation.** It needs a real
inter-token latency, and a non-streaming `total_time / tokens` proxy is TTFT-dominated — a
short reply (the closing "reply with just the word: done") looks like a 10x spike versus a
long-answer turn, a false positive that flunked a healthy minimax on the deploy gate.
`tpot_spike()` stays available for a future streaming-ITL implementation; the reliable
content checks (CJK bleed, runaway thinking) are what gate here.

The verdict type keeps the name `QualityVerdict` — it is the reasons list every turn
carries, and renaming it buys nothing but churn.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --- the heuristics (was sparky.quality) ------------------------------------------------

def non_latin_ratio(text: str) -> float:
    """Fraction of *alphabetic* characters that are non-ASCII (the CJK-bleed signal)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) > 127) / len(letters)


def is_cjk_bleed(text: str, threshold: float = 0.30) -> bool:
    """>threshold non-Latin letters in a reply to an English prompt = multilingual garbage."""
    return non_latin_ratio(text) > threshold


def _words(s: str) -> int:
    return len(s.split())


def runaway_thinking(
    content: str, reasoning_content: str | None = None, *, limit: int = 200
) -> bool:
    """The nonstop-thinking failure: long reasoning that never yields an answer.

    Reasoning-parser engine (reasoning split out): reasoning exceeds `limit` words
    while `content` (the answer) is empty. No parser: an opening ``<think>`` with no
    closing ``</think>`` and more than `limit` words after it.
    """
    if reasoning_content is not None:
        return _words(reasoning_content) > limit and _words(content) == 0
    if "<think>" in content and "</think>" not in content:
        return _words(content.split("<think>", 1)[1]) > limit
    return False


def tpot_spike(turn_itl_ms: float, baseline_itl_ms: float, *, factor: float = 10.0) -> bool:
    """Turn's inter-token latency > factor× the turn-1 baseline = the model is spinning."""
    return baseline_itl_ms > 0 and turn_itl_ms > factor * baseline_itl_ms


@dataclass
class QualityVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_turn(
    content: str,
    reasoning_content: str | None = None,
    *,
    itl_ms: float | None = None,
    baseline_itl_ms: float | None = None,
) -> QualityVerdict:
    """Apply every heuristic to one turn; `ok` iff none fired."""
    reasons: list[str] = []
    if is_cjk_bleed(content):
        reasons.append("cjk_bleed")
    if runaway_thinking(content, reasoning_content):
        reasons.append("runaway_thinking")
    if itl_ms is not None and baseline_itl_ms is not None and tpot_spike(itl_ms, baseline_itl_ms):
        reasons.append("tpot_spike")
    return QualityVerdict(ok=not reasons, reasons=reasons)


# --- the conversation (was sparky.multiturn) --------------------------------------------

# Short prompts a healthy model answers in plain English — no prompt should elicit
# CJK output or unbounded thinking, so a flag means the engine is corrupt.
DEFAULT_TURNS: tuple[str, ...] = (
    "Hi! In one sentence, what is a good use for a two-node GPU cluster?",
    "Nice. Now expand on that in two short bullet points.",
    "Summarize what you just said in five words.",
    "What is 17 times 4? Reply with just the number.",
    "List three primary colors.",
    "Translate 'good morning' into French.",
    "What day comes after Monday?",
    "Thanks — reply with just the word: done.",
)


@dataclass
class TurnResult:
    prompt: str
    content: str
    reasoning_content: str | None
    verdict: QualityVerdict


@dataclass
class MultiturnResult:
    ok: bool
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def failures(self) -> list[TurnResult]:
        return [t for t in self.turns if not t.verdict.ok]


def run_multiturn(
    client,
    model: str,
    *,
    turns: tuple[str, ...] = DEFAULT_TURNS,
    max_tokens: int = 256,
) -> MultiturnResult:
    """Run the conversation on `client` (anything with a `.chat(messages, model, …)`).

    `client` matches `sparky.foundation.api.VllmClient.chat`, returning an object with
    `.content` and `.reasoning_content`.
    """
    messages: list[dict] = []
    results: list[TurnResult] = []
    ok = True

    for prompt in turns:
        messages.append({"role": "user", "content": prompt})
        res = client.chat(messages, model=model, max_tokens=max_tokens)
        verdict = check_turn(res.content, res.reasoning_content)
        results.append(TurnResult(prompt, res.content, res.reasoning_content, verdict))
        messages.append({"role": "assistant", "content": res.content})
        ok = ok and verdict.ok

    return MultiturnResult(ok=ok, turns=results)
