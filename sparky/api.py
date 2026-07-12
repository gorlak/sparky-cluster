"""vLLM OpenAI-compatible API client (ADR-0010 shared substrate).

A thin `httpx` wrapper over a served engine. It's what the request-shape smoke
and multiturn quality checks (ADR-0011 / ADR-0012) and the benchmark runner
(ADR-0012) use to talk to an engine: poll readiness, list models, send a chat,
and probe the tool-call shape Open WebUI actually sends.
"""

from __future__ import annotations

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
