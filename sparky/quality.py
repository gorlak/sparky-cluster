"""Multiturn output-corruption heuristics (ADR-0012 quality check).

Detects the specific catastrophic failure modes seen on this cluster — CJK bleed,
runaway (never-ending) thinking, and inter-token-latency spikes. Deliberately
conservative / false-negative tolerant: they flag garbage, not mild degradation.
The heuristics inspect `reasoning_content` when a reasoning-parser engine splits
it out, and fall back to scanning `content` for literal `<think>` tags otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
