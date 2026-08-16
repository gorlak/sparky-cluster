"""The vision gate (ADR-0011 Layer 3) — no hardware, no model.

Two things need to be true before a vision verdict means anything: the image we send
must really contain what we claim, and the grader must be strict about the number while
being relaxed about prose. Both are checked here by decoding the PNG back with nothing
but the stdlib — if the encoder is wrong, every downstream "vision fail" is a lie about
the model.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from sparky.verify import vision_sanity as vision


def decode(png: bytes) -> tuple[int, int, bytes]:
    """Minimal PNG reader: IHDR + inflated IDAT. Deliberately not Pillow — the point is
    to verify our encoder against the spec, not against another implementation."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "bad signature"
    assert png[12:16] == b"IHDR"
    w, h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", png[16:29])
    assert (depth, ctype, comp, filt, inter) == (8, 2, 0, 0, 0), "not 8-bit truecolour"
    idat, i = b"", 8
    while i < len(png):
        length = struct.unpack(">I", png[i:i + 4])[0]
        tag = png[i + 4:i + 8]
        payload = png[i + 8:i + 8 + length]
        # every chunk must carry a valid CRC or a decoder will reject the image
        assert struct.unpack(">I", png[i + 8 + length:i + 12 + length])[0] == \
            zlib.crc32(tag + payload) & 0xFFFFFFFF, f"bad CRC on {tag!r}"
        if tag == b"IDAT":
            idat += payload
        i += 12 + length
    return w, h, zlib.decompress(idat)


def count_blue(png: bytes) -> int:
    w, h, raw = decode(png)
    stride = w * 3 + 1
    blue = sum(
        tuple(raw[y * stride + 1 + x * 3: y * stride + 4 + x * 3]) == vision.BLUE
        for y in range(h) for x in range(w)
    )
    assert blue % (vision.CELL ** 2) == 0, "blue pixels are not whole squares"
    return blue // (vision.CELL ** 2)


def test_the_image_contains_exactly_what_the_prompt_asks_about():
    """The prompt and the answer come from one constant; this proves the pixels agree."""
    assert count_blue(vision.png_bytes()) == vision.SQUARES


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_the_encoder_is_parameterised_not_hardcoded(n):
    assert count_blue(vision.png_bytes(n)) == n


def test_the_payload_stays_small_enough_to_inline():
    """It travels as a base64 data URI in a chat message on every activation. A fixture
    that bloated to megabytes would make the gate expensive enough to skip."""
    assert len(vision.data_uri()) < 4096


@pytest.mark.parametrize("answer", ["3", " 3 ", "three", "There are 3 blue squares.",
                                    "Three.", "I count 3."])
def test_grader_accepts_a_correct_count_in_any_shape(answer):
    assert vision.grade(answer).ok


@pytest.mark.parametrize("answer", ["5", "one", "", "   ", "I cannot see an image",
                                    "no idea", "5 blue squares, or maybe 3"])
def test_grader_rejects_a_wrong_or_absent_count(answer):
    """The last case matters: a model that leads with the wrong number has not seen the
    image, and must not be rescued by mentioning the right one later."""
    assert not vision.grade(answer).ok


def test_a_text_only_model_is_not_a_failure():
    """400 = the server refused multimodal content. Reported, never failed — otherwise
    every text-only profile would show red for a capability it never claimed."""
    class _Refuses:
        def chat(self, *a, **k):
            import httpx
            request = httpx.Request("POST", "http://x/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    result = vision.probe(_Refuses(), "m")
    assert result.unsupported and result.ok


def test_a_broken_vision_path_is_a_failure():
    """500 on an image from a model that accepted it is exactly what this gate exists
    to catch — it must not be waved through the way a 400 is."""
    class _Breaks:
        def chat(self, *a, **k):
            import httpx
            request = httpx.Request("POST", "http://x/v1/chat/completions")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    result = vision.probe(_Breaks(), "m")
    assert not result.ok and not result.unsupported and result.status == 500


def test_a_wrong_count_fails_even_though_the_call_succeeded():
    """The subtle case, and the reason the gate counts rather than describes: HTTP 200,
    fluent reply, wrong number — a model whose vision is quietly broken (ADR-0014 saw
    exactly this from MTP speculative decoding)."""
    class _Miscounts:
        def chat(self, *a, **k):
            return type("R", (), {"content": "There are 5 blue squares.",
                                  "status_code": 200})()

    result = vision.probe(_Miscounts(), "m")
    assert not result.ok and "saw 5" in result.detail


def test_the_token_budget_is_generous_enough_for_a_reasoning_model():
    """A 32-token cap returned an empty `content` from qwen3.6-35b and the gate scored it
    as a vision failure — on a model that answers correctly at 512. The budget must never
    be the thing under test."""
    import inspect
    source = inspect.getsource(vision.probe)
    assert "max_tokens=512" in source, "vision probe budget regressed"


def test_an_answer_that_lands_only_in_reasoning_content_still_counts_as_seeing():
    """Distinguishes 'the vision path is broken' from 'the chat surface put the answer in
    the wrong field' — different problems, and only the first is a vision failure."""
    class _ReasonsOnly:
        def chat(self, *a, **k):
            return type("R", (), {"content": "", "reasoning_content": "I count 3 squares.",
                                  "status_code": 200})()

    result = vision.probe(_ReasonsOnly(), "m")
    assert result.ok and "reasoning" in result.detail
