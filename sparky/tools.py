"""The `tools` regiment (ADR-0016) — all four `tool_choice` shapes, not just liveness.

The smoke gate sends **one** shape, `tool_choice: "auto"`, because that is what Open WebUI
sends on an ordinary chat, and its job is to stop a profile that would 400 the moment a
user types. That check is right for a gate and far too narrow for a measurement.

**DEF-0011 is the whole argument for this file.** `qwen3.6-35b-nvfp4-mtp3-single` passed
the smoke gate every time while `tool_choice: "required"` and named-function calls died
mid-generation with `Failed to advance FSM … grammar rejected tokens`. MTP's speculative
decoding breaks `</think>` detection in structured output, so exactly the two shapes an
*agent* depends on were broken, on a profile we were serving, invisibly — because nothing
measured them. It took a hand-run experiment to find, and a second one on the sibling
profile to prove MTP was the cause rather than a correlate.

Two shapes are cheap and two are not, which is why this is a regiment rather than gate
material: `required` and named-function force constrained decoding, and a model that
reasons before answering can spend real time inside a grammar.

**A 200 is not a pass.** `qwen3_xml` on Qwen3-VL returned HTTP 200 with `{}` and
`{"city": "<value=Paris>"}` — a valid-looking response carrying garbage, which a
status-code check reports as success. So the shapes that are supposed to produce a call
are checked for a *parseable* call with the argument we asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# One tool, one required string argument. Small on purpose: the point is the CALLING
# CONVENTION, not whether the model can pick between tools.
TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather in a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string",
                                    "description": "The city name, e.g. Paris"}},
            "required": ["city"],
        },
    },
}
PROMPT = "What is the weather in Paris? Use the tool."
EXPECT_ARG = "paris"


@dataclass
class ShapeResult:
    shape: str
    status: int | None
    ok: bool
    detail: str = ""


@dataclass
class ToolsResult:
    shapes: list[ShapeResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.shapes if s.ok)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.shapes)

    def summary(self) -> str:
        return f"{self.passed}/{len(self.shapes)} shapes · " + " ".join(
            f"{s.shape}={'ok' if s.ok else (s.detail or s.status or 'fail')}"
            for s in self.shapes)


def _first_call(payload: dict) -> dict | None:
    try:
        return (payload["choices"][0]["message"].get("tool_calls") or [None])[0]
    except (KeyError, IndexError, TypeError):
        return None


def check(client, model: str, *, timeout: float = 180.0) -> ToolsResult:
    """Exercise every `tool_choice` shape a caller can send.

    `none` is the odd one out: it must NOT produce a call. Including it is not padding —
    a model that emits a tool call when told not to breaks any caller doing its own
    routing, and it is the cheapest of the four to check.
    """
    result = ToolsResult()
    shapes = [
        ("auto", "auto", True),
        ("none", "none", False),
        ("required", "required", True),
        ("named", {"type": "function", "function": {"name": "get_weather"}}, True),
    ]
    for name, choice, expect_call in shapes:
        try:
            response = client.post_chat(
                model=model, messages=[{"role": "user", "content": PROMPT}],
                tools=[TOOL], tool_choice=choice, max_tokens=512, timeout=timeout)
            status = response.status_code
        except Exception as exc:
            result.shapes.append(ShapeResult(name, None, False, f"error:{type(exc).__name__}"))
            continue
        if status != 200:
            body = ""
            try:
                body = str(response.json())[:120]
            except Exception:
                body = (response.text or "")[:120]
            result.shapes.append(ShapeResult(name, status, False, body))
            continue

        call = _first_call(response.json())
        if not expect_call:
            # `none` passes by NOT calling. Content may be anything.
            result.shapes.append(
                ShapeResult(name, status, call is None,
                            "" if call is None else "called a tool despite tool_choice=none"))
            continue
        if call is None:
            result.shapes.append(ShapeResult(name, status, False, "no tool_call in response"))
            continue
        # HTTP 200 with a call is still not a pass — `qwen3_xml` on Qwen3-VL returned
        # `{}` and `{"city": "<value=Paris>"}` here, which a status check calls success.
        args = str((call.get("function") or {}).get("arguments") or "")
        ok = EXPECT_ARG in args.lower()
        result.shapes.append(
            ShapeResult(name, status, ok, "" if ok else f"arguments look wrong: {args[:80]}"))
    return result
