"""Unit tests for the multiturn corruption heuristics (ADR-0012)."""

from sparky.quality import (
    check_turn,
    is_cjk_bleed,
    non_latin_ratio,
    runaway_thinking,
    tpot_spike,
)


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
