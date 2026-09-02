"""The HTTP-native bench (ADR-0016, ADR-0011 Layer 3) — no hardware, no engine.

The old regiment could not be tested this way at all: it assembled a `sudo docker exec`
command line, so everything past that point needed root and a live container. Rebuilt
against the endpoint, the whole measurement is arithmetic over timestamps — and
arithmetic is testable, which is a second reason the rebuild was worth doing.

What matters here is that the METRICS are right. A bench that reports plausible-looking
wrong numbers is worse than no bench, because it will be believed.
"""

from __future__ import annotations

import time

import pytest

from sparky.measure.instruments import bench


class _FakeClient:
    """Emits deltas at controlled intervals so the timing maths has a known answer."""

    def __init__(self, ttft_s=0.10, itl_s=0.02, tokens=5, fail=False):
        self.ttft_s, self.itl_s, self.tokens, self.fail = ttft_s, itl_s, tokens, fail
        self.prompts: list[str] = []

    def stream_chat(self, messages, model, **kw):
        self.prompts.append(messages[0]["content"])
        if self.fail:
            raise RuntimeError("engine down")
        time.sleep(self.ttft_s)
        yield time.monotonic(), "first"
        for _ in range(self.tokens - 1):
            time.sleep(self.itl_s)
            yield time.monotonic(), "tok"


def test_ttft_and_itl_come_from_the_stream():
    """The reason streaming was added: a blocking response collapses these into one
    number, which is why the old bench had to run inside the container."""
    client = _FakeClient(ttft_s=0.15, itl_s=0.03, tokens=4)
    result = bench.run_one(client, "m", "hello", 16)
    assert result.ok
    assert 130 < result.ttft_ms < 260, result.ttft_ms
    assert result.output_tokens == 4
    assert len(result.itls_ms) == 3
    assert all(15 < x < 90 for x in result.itls_ms), result.itls_ms


def test_a_failed_request_is_recorded_not_raised():
    """One dead request must not abort a scenario — it is excluded from the metrics and
    counted, so a partial failure is visible rather than silently averaged in."""
    result = bench.run_one(_FakeClient(fail=True), "m", "hello", 16)
    assert not result.ok and "RuntimeError" in result.error


def test_metrics_exclude_failed_requests():
    good = bench.RequestResult(ttft_ms=100.0, itls_ms=[20.0, 20.0],
                                   output_tokens=3, total_s=0.14)
    bad = bench.RequestResult(ttft_ms=None, error="boom")
    metrics = bench.ScenarioResult("latency", [good, bad], wall_s=1.0).metrics()
    assert metrics["ttft_mean_ms"] == 100.0
    assert metrics["requests_s"] == 1.0          # one good request in one second
    assert metrics["output_toks_s"] == 3.0


def test_metrics_are_empty_when_everything_failed():
    """Better to record nothing than to record zeros that look like a slow model."""
    bad = bench.RequestResult(ttft_ms=None, error="boom")
    assert bench.ScenarioResult("latency", [bad], wall_s=1.0).metrics() == {}


def test_tpot_measures_decode_after_the_first_token():
    """TPOT must exclude TTFT — otherwise a slow prefill masquerades as slow decode."""
    r = bench.RequestResult(ttft_ms=1000.0, itls_ms=[10.0] * 4,
                                output_tokens=5, total_s=1.04)
    metrics = bench.ScenarioResult("latency", [r], wall_s=1.04).metrics()
    assert 5 < metrics["tpot_mean_ms"] < 15, metrics["tpot_mean_ms"]


def test_p99_is_index_based_not_interpolated():
    """With 20 samples an interpolated p99 is a fiction — it reports a value no request
    ever experienced."""
    values = [float(i) for i in range(1, 21)]
    assert bench._p99(values) in values


# --- prompts ----------------------------------------------------------------

def test_prompts_are_deterministic():
    """Two runs must measure the same work, or a comparison measures the prompts."""
    assert bench.make_prompt(64, 4.0, seed=3) == bench.make_prompt(64, 4.0, seed=3)


def test_prompt_length_tracks_the_requested_token_count():
    short = bench.make_prompt(32, 4.0)
    long = bench.make_prompt(512, 4.0)
    assert len(long) > 8 * len(short)


def test_the_shared_prefix_is_byte_identical_across_requests():
    """The whole point of the prefix_cache scenario: if the prefix varied, nothing would
    hit the cache and the scenario would silently measure the wrong thing."""
    client = _FakeClient(tokens=2)
    scenario = bench.Scenario("prefix_cache", prompt_tokens=8, max_tokens=4,
                                  requests=3, concurrency=1, shared_prefix_tokens=32,
                                  warmup=0)
    bench.run_scenario(client, "nonexistent-model", scenario)
    prefixes = {p.split("\n\n")[0] for p in client.prompts}
    assert len(prefixes) == 1, "shared prefix differed between requests"
    bodies = {p.split("\n\n")[1] for p in client.prompts}
    assert len(bodies) == 3, "request bodies should differ so each does real work"


def test_chars_per_token_falls_back_when_the_tokenizer_is_missing(tmp_path):
    """Never fail a bench because a tokenizer could not be read — prompt sizing only
    needs to be consistent, not exact."""
    assert bench.chars_per_token("no-such-model", models_dir=tmp_path) == 4.0


def test_warmup_requests_are_excluded_from_the_measurement():
    """The first requests after an activation pay for cudagraph replay and a cold prefix
    cache; counting them would put a startup artefact in the p99."""
    client = _FakeClient(tokens=2)
    scenario = bench.Scenario("latency", prompt_tokens=8, max_tokens=4,
                                  requests=2, concurrency=1, warmup=3)
    result = bench.run_scenario(client, "m", scenario)
    assert len(result.results) == 2          # measured
    assert len(client.prompts) == 5          # 3 warmup + 2 measured


def test_scenarios_cover_the_core_shapes_and_the_prefill_sweep():
    assert {"latency", "throughput", "prefix_cache"} <= set(bench.SCENARIOS)
    assert bench.SCENARIOS["latency"].concurrency == 1
    assert bench.SCENARIOS["throughput"].concurrency > 1
    assert bench.SCENARIOS["prefix_cache"].shared_prefix_tokens > 0
    # the prefill sweep: growing prompt, a single output token so the timing is prefill, at
    # concurrency 1 so it is one session's wait — not aggregate.
    prefill = {n: s for n, s in bench.SCENARIOS.items() if n.startswith("prefill@")}
    assert set(prefill) == {"prefill@4k", "prefill@16k", "prefill@64k"}
    assert [s.prompt_tokens for s in
            (prefill["prefill@4k"], prefill["prefill@16k"], prefill["prefill@64k"])] \
        == [4096, 16384, 65536]
    assert all(s.max_tokens == 1 and s.concurrency == 1 for s in prefill.values())


def test_prefill_throughput_is_prompt_tokens_over_ttft():
    """The prefill metric is length-normalised: prompt tokens ingested per second, so a
    64k-token prompt at 100ms TTFT reads at ~640k tok/s regardless of the prompt size."""
    result = bench.ScenarioResult("prefill@64k", [
        bench.RequestResult(ttft_ms=100.0, itls_ms=[], output_tokens=1, total_s=0.1),
        bench.RequestResult(ttft_ms=100.0, itls_ms=[], output_tokens=1, total_s=0.1),
    ], wall_s=0.2, prompt_tokens=65536)
    m = result.metrics()
    assert m["prefill_toks_s"] == 65536 / (100.0 / 1000)     # = prompt_tokens / TTFT(s)
    # and it rides through to_run into the store row
    run = bench.to_run(result, label="lbl", model="m", profile="p")
    assert run.prefill_toks_s == m["prefill_toks_s"]


def test_to_run_maps_onto_the_trend_store():
    result = bench.ScenarioResult("latency", [
        bench.RequestResult(ttft_ms=100.0, itls_ms=[20.0], output_tokens=2, total_s=0.12)
    ], wall_s=1.0)
    run = bench.to_run(result, label="lbl", model="m", profile="p")
    assert run.scenario == "latency" and run.label == "lbl"
    assert run.ttft_mean_ms == 100.0


def test_streaming_counts_reasoning_deltas():
    """vLLM 0.24.0 streams `reasoning`; the non-streaming API returns `reasoning_content`.
    Reading only the latter made every request from a reasoning model look like a
    zero-token failure — which is exactly what the first live bench reported."""
    import json as _json

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_lines(self):
            for field in ("content", "reasoning", "reasoning_content"):
                yield "data: " + _json.dumps(
                    {"choices": [{"delta": {field: "tok"}}]})
            yield "data: [DONE]"

    class _Ctx:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    from sparky.foundation.api import VllmClient
    client = VllmClient.__new__(VllmClient)
    client._client = type("C", (), {"stream": lambda *a, **k: _Ctx()})()
    deltas = list(client.stream_chat([{"role": "user", "content": "x"}], model="m"))
    assert len(deltas) == 3, "all three delta field spellings must be counted"


def test_requests_address_the_served_name_not_the_weights_directory():
    """`engine["model"]` is a directory on disk; the API only answers to `served_as`.
    Sending the directory name 400s every request — the first live bench reported
    20/20 failures for exactly this."""
    seen = {}

    class _C:
        def stream_chat(self, messages, model, **kw):
            seen["model"] = model
            yield 0.0, "t"

    scenario = bench.Scenario("latency", prompt_tokens=8, max_tokens=4,
                              requests=1, concurrency=1, warmup=0)
    bench.run_scenario(_C(), "minimax-m2", scenario, model_dir="MiniMax-M2.7-NVFP4")
    assert seen["model"] == "minimax-m2"


# --- context capacity (the number long-context work is actually bound by) -----

class _Resp:
    def __init__(self, text="", payload=None):
        self.text, self._payload = text, payload

    def json(self):
        return self._payload


# vLLM 0.24's real shape: cache_config_info carries the BLOCK numbers and nothing else.
_METRICS = ('vllm:cache_config_info{block_size="16",num_gpu_blocks="33761",'
            'cache_dtype="auto"} 1.0\n'
            'vllm:num_requests_running 0.0\n')


def _fake_http(monkeypatch, metrics=_METRICS, cards=None):
    import httpx

    def get(url, **_kw):
        if url.endswith("/metrics"):
            return _Resp(text=metrics)
        return _Resp(payload={"data": cards if cards is not None else []})

    monkeypatch.setattr(httpx, "get", get)


def test_max_model_len_comes_from_the_model_card_not_the_metric(monkeypatch):
    """THE BUG (2026-08-10, qwen3-vl-235b): max_model_len was read as a label of
    `vllm:cache_config_info`, which vLLM 0.24 does not publish. It defaulted to 0, so the
    bench reported "0 usable tokens" beside a perfectly healthy 540,176-token cache —
    a plausible-looking wrong number in the one column long-context work depends on."""
    _fake_http(monkeypatch, cards=[{"id": "sparky", "max_model_len": 262144},
                                   {"id": "qwen3-vl-235b", "max_model_len": 262144}])
    out = bench.context_capacity("http://x")
    assert out["kv_tokens"] == 33761 * 16          # 540,176
    assert out["context_length"] == 262144         # vLLM's `max_model_len` card field, stored as context_length
    assert out["usable_context"] == 262144         # the cache is the larger of the two
    assert out["full_slots"] == pytest.approx(540176 / 262144)


def test_usable_context_is_capped_by_the_cache_not_the_promise(monkeypatch):
    """A max_model_len larger than the whole KV cache is a promise the engine cannot
    keep for a single request — reporting it as usable context would overstate what the
    profile can actually read."""
    _fake_http(monkeypatch, cards=[{"id": "m", "max_model_len": 1_000_000}])
    out = bench.context_capacity("http://x")
    assert out["usable_context"] == 540176


def test_a_missing_model_card_does_not_fabricate_a_context(monkeypatch):
    """Better to report nothing than a zero that reads as "this model has no context"."""
    _fake_http(monkeypatch, cards=[])
    out = bench.context_capacity("http://x")
    assert out["kv_tokens"] == 540176
    assert "usable_context" not in out and "context_length" not in out


def test_kv_capacity_falls_back_to_the_sglang_metric(monkeypatch):
    """SGLang has no vllm:cache_config_info; it publishes total KV directly as
    sglang:max_total_num_tokens. Without this fallback the whole sglang engine's context
    column was blank (2026-08-31, qwen3.8) — context_length came through from /v1/models, but
    usable_context = min(kv, ctx) never computed with kv_tokens missing."""
    metrics = 'sglang:max_total_num_tokens{model_name="sparky"} 607040.0\n'
    _fake_http(monkeypatch, metrics=metrics, cards=[{"id": "sparky", "max_model_len": 262144}])
    out = bench.context_capacity("http://x")
    assert out["kv_tokens"] == 607040                 # from the sglang metric, not cache_config_info
    assert out["context_length"] == 262144            # from the model card, engine-agnostic
    assert out["usable_context"] == 262144            # min(607040, 262144) — now computes


# --- a measurement must know if the fleet moved under it (2026-08-13) -----

def test_a_result_knows_whether_the_fleet_moved(monkeypatch):
    """A number is only meaningful if ONE engine produced all of it.

    Several things move the fleet mid-run: scale-to-zero unloading an idle model
    (ADR-0022), a deploy's `fleet-state` converging the selection, a manual activate, an
    engine that died and came back. `suite` holds the fleet lock so the unloader refuses,
    but a bare `bench` holds nothing — so it must DETECT rather than assume.

    Deliberately not scale-to-zero-specific: `activated_at` changes for every cause, so one
    comparison covers them all, including the next cause nobody has thought of.
    """
    same = ("qwen3.6", "2026-08-13T17:00:00+0000")
    later = ("qwen3.6", "2026-08-13T17:20:00+0000")     # re-activated mid-run
    other = ("minimax", "2026-08-13T17:00:00+0000")     # different model entirely

    steady = bench.ScenarioResult("s", [], 1.0, activation_before=same, activation_after=same)
    assert steady.fleet_moved is False

    for before, after in ((same, later), (same, other), (same, None), (None, same)):
        moved = bench.ScenarioResult("s", [], 1.0,
                                     activation_before=before, activation_after=after)
        assert moved.fleet_moved is True, f"{before} -> {after} must count as moved"


def test_an_unknown_fingerprint_counts_as_moved():
    """Unknown is not stable. An unreadable topology is not evidence that nothing changed,
    and the failure being guarded against is publishing numbers from two engines as one."""
    r = bench.ScenarioResult("s", [], 1.0, activation_before=None, activation_after=None)
    assert r.fleet_moved is True
