"""Score an EXTERNAL model on the same sets, as a yardstick (ADR-0024).

The fleet's scores are only comparable to each other — that is the price of a set nobody
else runs, and it was always the right trade. What it costs is any sense of scale: "62%
weighted" says nothing about whether 62% is close to the ceiling or nowhere near it.

A frontier model answers that, and it answers a second question the fleet cannot:
**whether the SET is any good.** If a strong model scores near zero, the problems are
probably ambiguous rather than hard; if it scores 100%, the set cannot discriminate at the
top. Without a known-strong reference there is no way to tell "my models are weak here"
from "my problem is badly written".

**Reached only through `sparky coding --via anthropic`, and never on its own** (ADR-0025).
It is one command with the fleet path, differing only in which endpoint answers — but the
external path is opt-in per call, refuses to send a private set's prompts without
`--publish-prompts`, and the suite regiment is pinned to `via="local"` so no campaign can
reach it. The credential lives in the environment of whoever runs it
(`ANTHROPIC_API_KEY`) and nowhere else: no deploy writes it, no service reads it, nothing
under `/opt/cluster` holds it. The only capability it borrows is the sandbox, already
deployed and already bounded.

The reference is a CONSTANT for a given set: it moves when the set moves, or when a new
model ships. It is not part of any campaign and does not run on a schedule.
"""

from __future__ import annotations

import json
import os

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-1"
# A reference row is not a fleet member: it cannot be activated, cannot be retired, and
# must never be read as a candidate for serving. The profile column says so in a word.
REFERENCE_PROFILE = "reference"


class MissingKey(RuntimeError):
    """Raised rather than prompting: this runs unattended as often as not, and a prompt
    that never returns is worse than a message that says what to set."""


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise MissingKey(
            "ANTHROPIC_API_KEY is not set.\n"
            "This is deliberately NOT stored on the cluster — no deploy writes it, no "
            "service can read it, and it never touches /opt/cluster. Export it in the "
            "shell you are running from:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...")
    return key


class AnthropicClient:
    """The one method `coding.run` needs, over the Messages API.

    Matches the shape of `VllmClient` rather than the other way round — the scorer is
    already written against a client, so an external model is a client and nothing else
    changes: same prompts, same hidden tests, same sandbox, same verdicts, same weights.
    """

    def __init__(self, key: str | None = None, *, timeout: float = 300.0,
                 client: httpx.Client | None = None):
        self._key = key or api_key()
        self._client = client or httpx.Client(timeout=timeout)

    def __enter__(self) -> "AnthropicClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def stream_text(self, messages, *, model: str, max_tokens: int,
                    temperature: float = 0.0) -> tuple[str, str]:
        """`(text, stop_reason)`. Named for the interface, not the transport — the scorer
        checks for this method, and whether it streams underneath is not its business."""
        body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": m["role"], "content": m["content"]}
                             for m in messages]}
        reply = self._client.post(
            API_URL, json=body,
            headers={"x-api-key": self._key, "anthropic-version": API_VERSION,
                     "content-type": "application/json"})
        if reply.status_code != 200:
            # The body carries the reason — a bad key, a rate limit, an unknown model —
            # and losing it would make every failure look identical.
            raise RuntimeError(f"HTTP {reply.status_code}: {reply.text[:200]}")
        data = reply.json()
        text = "".join(block.get("text", "") for block in data.get("content", [])
                       if block.get("type") == "text")
        return text, str(data.get("stop_reason", ""))


def summarise(model: str, results: list) -> str:
    """One line per set, for a human who ran this by hand and is waiting on it."""
    lines = [f"reference: {model}"]
    for pset, result in results:
        lines.append(f"  {pset.name}@{pset.version}: "
                     f"pass@1 {100 * result.accuracy:.1f}% "
                     f"({result.passed}/{len(result.items)}) · "
                     f"weighted {100 * result.score:.1f}%")
    return "\n".join(lines)
