"""`bench`, rebuilt HTTP-native (ADR-0016) — throughput and latency with no privilege.

The old regiment shelled `sudo docker exec <container> vllm bench serve`. That cost two
things the sweep cannot pay, and neither was essential:

  * **root** — ADR-0018 retired the passwordless `docker` grant, because a `docker`
    grant *is* a root grant. Bench needed root only by accident, since `vllm bench serve`
    happens to live inside the container.
  * **head-locality** — the container is reachable only on its own node, so bench refused
    every `-single` profile. It could not measure the model that had actually been
    serving.

Both dissolve against the stable model endpoint. What is left is arithmetic over
timestamps, which is what a benchmark is.

**Fidelity is the risk, and is stated rather than hidden** (ADR-0018's errata). Timings
here are taken client-side, so they include network time and this client's own overhead.
That is the right measurement for "what does a user experience", and the wrong one for
"what is the engine's ceiling". Numbers are comparable to *other runs of this harness* —
the same rule as the quality regiment — not to `vllm bench serve` output.
"""

from __future__ import annotations

import json
import statistics

from sparky import topology
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sparky.store import Run

MODELS_DIR = Path("/opt/vllm/models")


@dataclass
class Scenario:
    """One measurement shape. `prompt_tokens` is approximate — see `make_prompt`."""

    name: str
    prompt_tokens: int
    max_tokens: int
    requests: int
    concurrency: int
    shared_prefix_tokens: int = 0
    warmup: int = 2


SCENARIOS: dict[str, Scenario] = {
    # One request at a time: TTFT and per-token speed with zero queueing. This is the
    # number a single interactive user feels.
    "latency": Scenario("latency", prompt_tokens=512, max_tokens=256,
                        requests=20, concurrency=1),
    # Flood it: peak tokens/s once the batcher is saturated. The number that matters when
    # several people (or a sweep) are using the cluster at once.
    "throughput": Scenario("throughput", prompt_tokens=512, max_tokens=256,
                           requests=64, concurrency=16),
    # Long SHARED prefix across requests. With prefix caching, TTFT collapses after the
    # first; without it, TTFT stays flat. That contrast is the measurement — and it is
    # newly relevant, since prefix caching turned out to be ON by default everywhere
    # (DEF-0007) without anyone choosing it.
    "prefix_cache": Scenario("prefix_cache", prompt_tokens=256, max_tokens=128,
                             requests=32, concurrency=8, shared_prefix_tokens=1024),
}


@dataclass
class RequestResult:
    ttft_ms: float | None
    itls_ms: list[float] = field(default_factory=list)
    output_tokens: int = 0
    total_s: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.ttft_ms is not None


@dataclass
class ScenarioResult:
    scenario: str
    results: list[RequestResult]
    wall_s: float
    # The activation this ran against, sampled BEFORE and AFTER. A number is only
    # meaningful if one engine produced all of it, and several things move the fleet
    # underneath a run: scale-to-zero unloading an idle model (ADR-0022), a deploy's
    # `fleet-state` converging the selection, a manual activate, a crashed engine coming
    # back. `sweep` holds the fleet lock so the unloader refuses; a bare `bench` holds
    # nothing, so it must DETECT rather than assume.
    activation_before: tuple | None = None
    activation_after: tuple | None = None

    @property
    def fleet_moved(self) -> bool:
        """Did the thing being measured change identity mid-run?

        Unknown counts as moved: an unreadable fingerprint is not evidence of stability,
        and the failure this guards against is silently publishing numbers from two
        different engines as if they were one.
        """
        return (self.activation_before is None
                or self.activation_after is None
                or self.activation_before != self.activation_after)

    @property
    def good(self) -> list[RequestResult]:
        return [r for r in self.results if r.ok]

    def metrics(self) -> dict:
        """Map to the trend store's fields, so `sparky report` compares these against
        historical `vllm bench serve` rows on the same axes (with the fidelity caveat)."""
        good = self.good
        if not good:
            return {}
        ttfts = [r.ttft_ms for r in good]
        itls = [x for r in good for x in r.itls_ms]
        out_tokens = sum(r.output_tokens for r in good)
        # TPOT: per-request decode time per token AFTER the first — the steady-state
        # speed a user sees once generation starts.
        tpots = [((r.total_s * 1000) - r.ttft_ms) / max(1, r.output_tokens - 1)
                 for r in good if r.output_tokens > 1]
        return {
            "output_toks_s": out_tokens / self.wall_s if self.wall_s else None,
            "total_toks_s": out_tokens / self.wall_s if self.wall_s else None,
            "requests_s": len(good) / self.wall_s if self.wall_s else None,
            "ttft_mean_ms": statistics.fmean(ttfts),
            "ttft_p99_ms": _p99(ttfts),
            "tpot_mean_ms": statistics.fmean(tpots) if tpots else None,
            "tpot_p99_ms": _p99(tpots) if tpots else None,
            "itl_mean_ms": statistics.fmean(itls) if itls else None,
            "itl_p99_ms": _p99(itls) if itls else None,
        }


def _p99(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # index-based, not interpolated: with 20 samples an interpolated p99 is a fiction
    return ordered[min(len(ordered) - 1, int(round(0.99 * (len(ordered) - 1))))]


def chars_per_token(model: str, models_dir: Path = MODELS_DIR) -> float:
    """Rough chars-per-token for this model, read from its tokenizer on disk.

    ADR-0016 calls for input lengths controlled from `tokenizer.json` — readable without
    privilege, unlike anything inside the container. We only need prompt sizes to be
    *comparable between runs*, not exact, so a vocabulary-derived estimate beats adding a
    tokenizer dependency. Falls back to 4.0, the usual English average.
    """
    path = models_dir / model / "tokenizer.json"
    try:
        vocab = json.loads(path.read_text())["model"]["vocab"]
        lengths = [len(tok) for tok in list(vocab)[:20000] if tok.isascii()]
        if lengths:
            # subword vocabularies skew short; the mean token length understates real
            # text, so nudge toward the observed English ratio
            return max(2.0, min(6.0, statistics.fmean(lengths)))
    except Exception:
        pass
    return 4.0


def make_prompt(tokens: int, cpt: float, seed: int = 0) -> str:
    """Deterministic filler of roughly `tokens` tokens.

    Deterministic so two runs measure the same work. Word-like rather than random
    characters, because a stream of gibberish tokenises very differently from prose and
    would make prompt sizes incomparable across models.
    """
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
             "mike november oscar papa quebec romeo sierra tango").split()
    target_chars = int(tokens * cpt)
    out, i = [], seed
    length = 0
    while length < target_chars:
        word = words[i % len(words)]
        out.append(word)
        length += len(word) + 1
        i += 1
    return " ".join(out)


def run_one(client, model: str, prompt: str, max_tokens: int) -> RequestResult:
    """One streamed request, timed. Client-side timestamps by design (see module docs)."""
    started = time.monotonic()
    ttft: float | None = None
    stamps: list[float] = []
    try:
        for stamp, _text in client.stream_chat(
                [{"role": "user", "content": prompt}], model=model,
                max_tokens=max_tokens, temperature=0.0):
            if ttft is None:
                ttft = (stamp - started) * 1000
            stamps.append(stamp)
    except Exception as exc:  # noqa: BLE001
        return RequestResult(ttft_ms=None, error=f"{type(exc).__name__}: {exc}"[:120])
    itls = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
    return RequestResult(ttft_ms=ttft, itls_ms=itls, output_tokens=len(stamps),
                         total_s=time.monotonic() - started)


def run_scenario(client, served_as: str, scenario: Scenario, *, model_dir: str = "",
                 on_progress=None) -> ScenarioResult:
    """`served_as` is what the API answers to; `model_dir` is the weights directory,
    used only to size prompts from the tokenizer on disk. Conflating them sends the
    directory name as the API's `model` and every request 400s — which is precisely how
    the first live run reported 20/20 failures."""
    cpt = chars_per_token((model_dir or served_as).split("/")[-1])
    shared = make_prompt(scenario.shared_prefix_tokens, cpt, seed=7) if scenario.shared_prefix_tokens else ""

    def prompt_for(i: int) -> str:
        # A SHARED prefix must be byte-identical across requests or the cache cannot hit;
        # the suffix varies so each request still does real decode work.
        body = make_prompt(scenario.prompt_tokens, cpt, seed=i)
        return f"{shared}\n\n{body}" if shared else body

    # Warm up off the clock: the first requests after an activation pay for CUDA graph
    # replay and an empty prefix cache, which would otherwise land in the p99.
    for i in range(scenario.warmup):
        run_one(client, served_as, prompt_for(1000 + i), 16)

    # Sampled around the measured window, not around the warmup: a fleet that moved
    # during warmup is fine (the numbers had not started), one that moved during the
    # window is not.
    before = topology.activation_fingerprint()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=scenario.concurrency) as pool:
        futures = [pool.submit(run_one, client, served_as, prompt_for(i), scenario.max_tokens)
                   for i in range(scenario.requests)]
        results = []
        for future in futures:
            results.append(future.result())
            if on_progress:
                on_progress(len(results), scenario.requests)
    return ScenarioResult(scenario.name, results, time.monotonic() - started,
                          activation_before=before,
                          activation_after=topology.activation_fingerprint())


def context_capacity(api_url: str, timeout: float = 8.0) -> dict:
    """How much CONTEXT this configuration can actually hold.

    Throughput and latency say how fast a model answers; neither says how much it can
    READ, and for long-document and whole-codebase work that is the binding constraint.
    vLLM publishes the numbers on /metrics — no root, no log scraping:

        kv_tokens        total KV capacity, in tokens (blocks x block_size)
        max_model_len    the per-request ceiling the profile sets
        usable_context   min(the two) — the real answer to "how much can ONE request
                         hold", because a max_model_len larger than the whole cache is
                         a promise the engine cannot keep
        full_slots       kv_tokens / max_model_len — simultaneous max-length requests

    `usable_context` is the number that matters for a single long session; `full_slots`
    is the concurrency story, which is the less interesting half here.
    """
    import httpx
    out: dict = {}
    try:
        body = httpx.get(f"{api_url}/metrics", timeout=timeout).text
    except Exception:
        return out
    labels: dict[str, str] = {}
    for line in body.splitlines():
        if line.startswith("vllm:cache_config_info"):
            inner = line[line.index("{") + 1:line.rindex("}")]
            for pair in inner.split(","):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    labels[k.strip()] = v.strip().strip('"')
            break
    try:
        blocks = int(float(labels.get("num_gpu_blocks", 0)))
        block_size = int(float(labels.get("block_size", 0)))
        if blocks and block_size:
            out["kv_tokens"] = blocks * block_size
    except (TypeError, ValueError):
        pass
    # max_model_len is NOT a label of cache_config_info — vLLM 0.24 does not carry it
    # there, so reading it from the labels silently yielded 0 and made `usable_context`
    # and `full_slots` both 0 while kv_tokens looked perfectly healthy (2026-08-10,
    # qwen3-vl-235b). The model card on /v1/models is the authoritative source; the label
    # is kept only as a fallback in case a future image does publish it.
    try:
        cards = httpx.get(f"{api_url}/v1/models", timeout=timeout).json().get("data", [])
        lens = [int(c["max_model_len"]) for c in cards if c.get("max_model_len")]
        if lens:
            # every card is the same engine under a different served name (the `sparky`
            # alias), so they agree; max() is just a total function over the list
            out["max_model_len"] = max(lens)
    except Exception:
        pass
    if "max_model_len" not in out:
        try:
            out["max_model_len"] = int(float(labels["max_model_len"]))
        except (KeyError, TypeError, ValueError):
            pass
    if "kv_tokens" in out and "max_model_len" in out:
        out["usable_context"] = min(out["kv_tokens"], out["max_model_len"])
        out["full_slots"] = out["kv_tokens"] / out["max_model_len"]
    return out


def to_run(result: ScenarioResult, *, label: str, model: str, profile: str) -> Run:
    return Run(label=label, model=model, profile=profile,
               scenario=result.scenario, harness="http", **result.metrics())
