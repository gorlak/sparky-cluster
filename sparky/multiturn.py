"""Multiturn quality smoke — the ADR-0012 deploy-gate check.

Drives a fixed, benign English conversation and applies the content-based
corruption heuristics (`quality.check_turn`) per turn, holding chat history across
turns. ~8 turns, ~2 minutes against a live engine. The client is injected, so the
conversation logic is unit-tested without hardware.

This catches the exact failure class that shipped on `step-3.5-fp8` (Nth-turn
garbage / nonstop thinking) — a throughput benchmark would pass it blind.

**TPOT-spike detection is deliberately not applied here.** It needs a real
inter-token latency, and a non-streaming `total_time / tokens` proxy is
TTFT-dominated — a short reply (e.g. the closing "reply with just the word: done")
looks like a 10x spike versus a long-answer turn, a false positive that flunked a
healthy minimax on the deploy gate. `quality.tpot_spike()` stays available for a
future streaming-ITL implementation; the reliable content checks (CJK bleed,
runaway thinking) are what gate here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sparky.quality import QualityVerdict, check_turn

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

    `client` matches `sparky.api.VllmClient.chat`, returning an object with
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
