"""The vision gate — does this model actually see?

Every model the fleet has staged since 2026-08 is vision-capable, and **not one of them
had its vision path tested**. The smoke gate checked readiness, tool-shape and multiturn
text quality; no image was ever sent. So a VL model could load, serve text perfectly,
pass the gate, and fail on the first image a user pasted into Open WebUI.

The image is **generated, not shipped**: a few coloured squares encoded as a PNG by hand
(stdlib `zlib` only — no Pillow, no fixture file, no binary in git). That keeps the test
deterministic and the repo clean, and it means the prompt and the answer are derived from
the same constant, so they cannot drift apart.

**Known limit, measured 2026-08-08.** The gate's image is small and its subject fills a
good fraction of the frame, deliberately. Detail below the vision encoder's effective
resolution is lost during downscaling and the model answers confidently anyway: three
24px squares in a 2048px frame returned HTTP 200 and the wrong count. So this gate
proves the vision *path* works, not that the model can read fine detail — those are
different claims and only the first is checked on every activation.

**Counting, not describing.** The question is "how many blue squares", because counting is
the thing that measurably breaks here: ADR-0014 found MTP speculative decoding corrupts
image *number-reads* specifically while leaving prose descriptions plausible. A gate that
asked "what colour is this?" would pass a model whose vision is quietly wrong.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from dataclasses import dataclass

import httpx

# Deliberately awkward to guess: not 1, not 2, and not a number a model would
# volunteer by chance. Small enough to be unambiguous at a glance.
SQUARES = 3
CELL = 24            # square edge, px; squares sit on one band at 2×CELL pitch
BLUE = (32, 64, 220)
WHITE = (255, 255, 255)

PROMPT = ("How many blue squares are in this image? "
          "Reply with only the number, as a digit.")

_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
          "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(squares: int = SQUARES) -> bytes:
    """A `squares`-blue-squares-on-white PNG, built from scratch.

    Truecolour (type 2), 8-bit, filter 0 on every scanline — the simplest legal PNG,
    which is the point: fewer ways for the encoder to be the thing under test.
    """
    # Canvas is DERIVED from the count, not a constant: a fixed 120px canvas silently
    # clipped the 4th square, so the image and the prompt disagreed. Deriving it means
    # the picture cannot contradict the question.
    width, height = squares * CELL * 2, CELL * 3
    rows = []
    for y in range(height):
        row = bytearray([0])                      # filter byte: none
        for x in range(width):
            # squares laid left-to-right on one band, one CELL gap between them
            inside = (CELL <= y < CELL * 2) and (x % (CELL * 2)) < CELL
            row += bytes(BLUE if inside else WHITE)
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _chunk(b"IEND", b""))


def data_uri(squares: int = SQUARES) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes(squares)).decode()


@dataclass(frozen=True)
class VisionResult:
    ok: bool
    detail: str            # short, for the smoke table
    status: int | None = None
    answer: str | None = None

    @property
    def unsupported(self) -> bool:
        """The model refused images — reported, never counted as a failure. A text-only
        model is not broken for lacking a vision tower."""
        return self.detail == "n/a"


def grade(answer: str, expected: int = SQUARES) -> VisionResult:
    """Did it read the count? Lenient about form, strict about the number.

    Accepts a digit or an English word anywhere in the reply, because instruction-
    following on "reply with only the number" varies and is not what is being tested.
    The FIRST number wins: a model that says "3" then rambles has still seen it, while
    one that opens with "5" has not, whatever it says afterwards.
    """
    text = (answer or "").strip().lower()
    if not text:
        return VisionResult(False, "empty reply", answer=answer)
    match = re.search(r"\d+|" + "|".join(_WORDS), text)
    if not match:
        return VisionResult(False, f"no number in {text[:40]!r}", answer=answer)
    token = match.group(0)
    got = int(token) if token.isdigit() else _WORDS[token]
    if got == expected:
        return VisionResult(True, "pass", answer=answer)
    return VisionResult(False, f"saw {got}, expected {expected}", answer=answer)


def probe(client, model: str, squares: int = SQUARES) -> VisionResult:
    """Send the generated image and grade the count.

    `VllmClient.chat` raises on non-2xx, so the interesting cases arrive as exceptions:
    a **400** means the server rejected multimodal content, i.e. a text-only model —
    reported as `n/a`, never a failure, because lacking a vision tower is not a defect.
    Any other status is real: the model advertised vision and then broke on an image,
    which is the whole reason this gate exists.
    """
    content = [{"type": "text", "text": PROMPT},
               {"type": "image_url", "image_url": {"url": data_uri(squares)}}]
    # 512, not 32. A REASONING model spends its budget getting to the answer, and a
    # 32-token cap returned an empty `content` for qwen3.6-35b — which the gate scored as
    # a vision failure on a model that sees perfectly well (it answers "3" at 512). Same
    # class of artifact as a truncated tool call: the budget measured the test, not the
    # model. Generous is cheap; the reply is one digit either way.
    try:
        result = client.chat([{"role": "user", "content": content}], model=model,
                             max_tokens=512, temperature=0.0)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 400:
            return VisionResult(True, "n/a", status=400)
        return VisionResult(False, f"HTTP {code}", status=code)
    except Exception as exc:                                  # transport, not verdict
        return VisionResult(False, f"error: {type(exc).__name__}", answer=str(exc)[:120])
    answer = result.content or ""
    verdict = grade(answer, squares)
    if not verdict.ok and not answer.strip() and result.reasoning_content:
        # It saw, but everything landed in reasoning_content and nothing in content.
        # Worth distinguishing: the vision path works, the chat surface is misbehaving.
        verdict = grade(result.reasoning_content, squares)
        if verdict.ok:
            return VisionResult(True, "pass (reasoning only)", result.status_code,
                                result.reasoning_content[:120])
    return VisionResult(verdict.ok, verdict.detail, result.status_code, answer[:120])
