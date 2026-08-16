"""Unit tests for the vLLM API client (ADR-0010) — no live server.

`httpx.MockTransport` lets us assert the exact request shapes and response
parsing without a running engine. The live smoke against a real engine is the
ADR-0012 Layer 5 job.
"""

from __future__ import annotations

import json

import httpx

from sparky.foundation.api import VllmClient


def make_client(handler) -> VllmClient:
    transport = httpx.MockTransport(handler)
    return VllmClient("http://engine", client=httpx.Client(base_url="http://engine", transport=transport))


def test_models_lists_ids():
    def handler(req):
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "minimax-m2"}, {"id": "x"}]})

    with make_client(handler) as c:
        assert c.models() == ["minimax-m2", "x"]


def test_is_ready_true_on_200():
    with make_client(lambda req: httpx.Response(200, json={"data": []})) as c:
        assert c.is_ready() is True


def test_is_ready_false_on_non200_and_transport_error():
    with make_client(lambda req: httpx.Response(503)) as c:
        assert c.is_ready() is False

    def boom(req):
        raise httpx.ConnectError("connection refused", request=req)

    with make_client(boom) as c:
        assert c.is_ready() is False


def test_wait_ready_polls_until_ready():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] >= 3 else 503)

    clock = {"t": 0.0}
    slept: list[float] = []
    with make_client(handler) as c:
        ok = c.wait_ready(
            timeout=100, interval=5,
            _now=lambda: clock["t"],
            _sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)),
        )
    assert ok is True
    assert calls["n"] == 3
    assert slept == [5, 5]


def test_wait_ready_times_out():
    clock = {"t": 0.0}
    with make_client(lambda req: httpx.Response(503)) as c:
        ok = c.wait_ready(
            timeout=10, interval=5,
            _now=lambda: clock["t"],
            _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        )
    assert ok is False


def test_chat_builds_body_and_splits_reasoning():
    seen = {}

    def handler(req):
        assert req.url.path == "/v1/chat/completions"
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "answer", "reasoning_content": "thinking"}}]
        })

    with make_client(handler) as c:
        res = c.chat([{"role": "user", "content": "q"}], model="m", max_tokens=8)
    assert seen["body"]["model"] == "m"
    assert seen["body"]["max_tokens"] == 8
    assert seen["body"]["messages"] == [{"role": "user", "content": "q"}]
    assert "tools" not in seen["body"]
    assert res.content == "answer"
    assert res.reasoning_content == "thinking"


def test_chat_handles_null_content_and_absent_reasoning():
    with make_client(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": None}}]})) as c:
        res = c.chat([{"role": "user", "content": "q"}], model="m")
    assert res.content == ""
    assert res.reasoning_content is None


def test_probe_tool_support_sends_auto_tool_choice():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with make_client(handler) as c:
        r = c.probe_tool_support("m")
    assert r.status_code == 200
    assert seen["body"]["tool_choice"] == "auto"
    assert seen["body"]["tools"][0]["type"] == "function"
    assert seen["body"]["tools"][0]["function"]["name"] == "noop"


def test_probe_tool_support_surfaces_400_from_missing_flags():
    handler = lambda req: httpx.Response(400, json={"error": "'auto' tool choice requires --enable-auto-tool-choice"})
    with make_client(handler) as c:
        assert c.probe_tool_support("m").status_code == 400
