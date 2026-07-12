"""Multiturn quality smoke — the ADR-0012 deploy-gate check.

Drives a fixed, benign English conversation and applies the corruption heuristics
(`quality.check_turn`) per turn, holding chat history across turns and recording a
per-turn per-token latency proxy for the TPOT-spike check. ~8 turns, ~2 minutes
against a live engine. The client is injected, so the conversation logic is
unit-tested without hardware.

This catches the exact failure class that shipped on `step-3.5-fp8` (Nth-turn
garbage / nonstop thinking) — a throughput benchmark would pass it blind.
"""

from __future__ import annotations

import time
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
    per_token_ms: float
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
    _now=time.monotonic,
) -> MultiturnResult:
    """Run the conversation on `client` (anything with a `.chat(messages, model, …)`).

    `client` matches `sparky.api.VllmClient.chat`, returning an object with
    `.content` and `.reasoning_content`.
    """
    messages: list[dict] = []
    baseline_ms: float | None = None
    results: list[TurnResult] = []
    ok = True

    for i, prompt in enumerate(turns):
        messages.append({"role": "user", "content": prompt})
        t0 = _now()
        res = client.chat(messages, model=model, max_tokens=max_tokens)
        elapsed_ms = (_now() - t0) * 1000.0
        # Per-token latency proxy (non-streaming): total time / tokens emitted.
        n_tokens = max(1, len((res.content or "").split()))
        per_token_ms = elapsed_ms / n_tokens
        if i == 0:
            baseline_ms = per_token_ms

        verdict = check_turn(
            res.content, res.reasoning_content,
            itl_ms=per_token_ms, baseline_itl_ms=baseline_ms,
        )
        results.append(TurnResult(prompt, res.content, res.reasoning_content, per_token_ms, verdict))
        messages.append({"role": "assistant", "content": res.content})
        ok = ok and verdict.ok

    return MultiturnResult(ok=ok, turns=results)
