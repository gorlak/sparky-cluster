"""vLLM OpenAI-compatible API client (ADR-0010 shared substrate).

A thin `httpx` wrapper over a served engine. It's what the request-shape smoke
and multiturn quality checks (ADR-0011 / ADR-0012) and the benchmark runner
(ADR-0012) use to talk to an engine: poll readiness, list models, send a chat,
and probe the tool-call shape Open WebUI actually sends.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

# A no-op function tool in the exact shape Open WebUI attaches to ordinary chats.
_DUMMY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "does nothing",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


@dataclass
class ChatResult:
    content: str
    reasoning_content: str | None
    raw: dict
    status_code: int


class VllmClient:
    """Client for one engine's API base URL (e.g. ``http://10.0.200.12:8000``)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VllmClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- readiness ---------------------------------------------------------

    def is_ready(self) -> bool:
        """True iff ``/v1/models`` answers 200 — the deploy-gate marker (ADR-0011)."""
        try:
            return self._client.get("/v1/models").status_code == 200
        except httpx.HTTPError:
            return False

    def wait_ready(
        self,
        *,
        timeout: float = 1200.0,
        interval: float = 5.0,
        _sleep=time.sleep,
        _now=time.monotonic,
    ) -> bool:
        """Poll ``/v1/models`` until ready or the timeout elapses."""
        deadline = _now() + timeout
        while _now() < deadline:
            if self.is_ready():
                return True
            _sleep(interval)
        return self.is_ready()

    def models(self) -> list[str]:
        r = self._client.get("/v1/models")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    # --- chat --------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        max_tokens: int = 256,
        temperature: float = 0.0,
        tools: list | None = None,
        tool_choice: str | None = None,
        extra: dict | None = None,
    ) -> ChatResult:
        """POST a chat completion; splits `reasoning_content` when the engine emits it."""
        body: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if extra:
            body.update(extra)
        r = self._client.post("/v1/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        return ChatResult(
            content=msg.get("content") or "",
            reasoning_content=msg.get("reasoning_content"),
            raw=data,
            status_code=r.status_code,
        )

    def stream_chat(self, messages: list[dict], model: str, *, max_tokens: int = 128,
                    temperature: float = 0.0, extra: dict | None = None):
        """Yield `(monotonic_time, text)` per streamed delta.

        **Benchmarking needs this and non-streaming cannot provide it.** TTFT is the wait
        for the FIRST token and inter-token latency is the gap between consecutive ones;
        a single blocking response collapses both into one number, which is why the old
        regiment had to shell into the container and let `vllm bench serve` do it.
        Against the stable endpoint (ADR-0016) an SSE stream gives the same timings with
        no root, no docker, and no head-locality.

        The timestamps are taken as each chunk arrives, so they include network time —
        which is correct: we are measuring what a client experiences, not what the engine
        privately achieved.
        """
        body: dict = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True,
        }
        if extra:
            body.update(extra)
        with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"]
                except (ValueError, KeyError, IndexError):
                    continue
                # vLLM 0.24.0 streams `reasoning` where the non-streaming API returns
                # `reasoning_content` — checking only the latter made a reasoning
                # model's entire output invisible and every bench request "fail" with
                # zero tokens. Reasoning tokens ARE decode work, so they must be timed.
                text = (delta.get("content") or delta.get("reasoning")
                        or delta.get("reasoning_content") or "")
                if text:
                    yield time.monotonic(), text

    def stream_text(self, messages: list[dict], model: str, *, max_tokens: int = 128,
                    temperature: float = 0.0, extra: dict | None = None
                    ) -> tuple[str, str | None]:
        """Collect a streamed reply as `(text, finish_reason)`.

        **Why the quality regiment must stream.** When a reasoning model hits the token
        cap without closing its ``</think>`` block, the non-streaming API returns EMPTY
        content — vLLM cannot split a block that never ended — even though thousands of
        reasoning tokens were generated. The text is not missing, it is unsplittable.

        Measured on 2026-08-10: the truncation rescue succeeded **3 of 3** times when it
        had that text to feed back and **11 of 101** when it did not, which is the whole
        difference between Step-3.5 and MiniMax being rankable and not. Streaming sees
        every delta as it arrives, so an unclosed block costs nothing.

        `stream_chat` stays the timing generator that bench needs; this is the blocking
        convenience wrapper, and it is the only one that reports why generation stopped.
        """
        body: dict = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True,
            # ask for the terminal chunk that carries finish_reason
            "stream_options": {"include_usage": False},
        }
        if extra:
            body.update(extra)
        parts: list[str] = []
        finish: str | None = None
        with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    choice = json.loads(payload)["choices"][0]
                except (ValueError, KeyError, IndexError):
                    continue
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                parts.append(delta.get("content") or delta.get("reasoning")
                             or delta.get("reasoning_content") or "")
        return "".join(parts), finish

    def post_chat(self, *, model: str, messages: list[dict], tools: list | None = None,
                  tool_choice: str | dict | None = None, max_tokens: int = 256,
                  temperature: float = 0.0, timeout: float | None = None) -> httpx.Response:
        """The raw response, WITHOUT `raise_for_status()`.

        `chat()` raises on a non-200, which is right for callers that want a reply and
        wrong for anything measuring the endpoint's behaviour: the `tools` regiment has to
        tell a 400 (engine started without tool flags) from a 200 carrying a malformed
        call, and report the body either way. An exception erases both.

        `tool_choice` may be a dict — a named-function choice is
        `{"type": "function", "function": {"name": ...}}`, one of the two shapes DEF-0011
        broke while `auto` kept passing the smoke gate.
        """
        body: dict = {"model": model, "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature}
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        return self._client.post("/v1/chat/completions", json=body,
                                 timeout=timeout or self._client.timeout)

    def probe_tool_support(self, model: str) -> httpx.Response:
        """Send the tool shape Open WebUI uses on ordinary chats — a dummy `tools`
        array plus ``tool_choice: "auto"``.

        An engine started without ``--enable-auto-tool-choice`` / ``--tool-call-parser``
        passes ``/v1/models`` readiness but **400s** here the moment a user types
        (this shipped on `minimax-m2.7-awq`). The caller checks ``.status_code``:
        200 = serves the UI it's wired to; 400 = missing the tool flags (ADR-0012).
        """
        return self._client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0,
                "tools": _DUMMY_TOOLS,
                "tool_choice": "auto",
            },
        )
