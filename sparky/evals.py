"""The `quality` regiment — a real accuracy number, so models can be RANKED (ADR-0016).

Everything else this cluster measures is throughput or liveness. Nothing answers "is
this model any good", which makes every sourcing decision a judgement call: whether
Nemotron beats MiniMax, whether a bigger model is worth 156 GiB of disk, whether MTP's
2.3x decode costs accuracy. This turns those into numbers.

**Deliberately sized.** Full MMLU-Pro is 12,032 questions — hours per model, which means
it never runs. The committed subset is 280 items, balanced 20-per-category and
interleaved so any prefix stays balanced; the default `limit` of 140 runs in a few
minutes under concurrency. A regiment that is too expensive to run is worth nothing.

⚠️ **Not comparable to published MMLU-Pro scores** — different items, count and prompt.
Comparable to *other runs of this subset*, which is what ranking needs.

**Scoring is the risky part**, so it is separated and unit-tested. A reasoning model
answers with paragraphs of thinking before its letter; a lenient parser inflates scores
by finding a stray "A" in prose, and a strict one deflates them by missing a correct
answer that was phrased politely. `extract_answer` takes the LAST explicit answer
statement, then falls back to the last standalone letter — and returns None rather than
guessing, so unparseable responses are counted and reported instead of silently wrong.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

SUBSET = Path(__file__).resolve().parent / "data" / "mmlu_pro_subset.jsonl"
LETTERS = "ABCDEFGHIJ"
DEFAULT_LIMIT = 140
DEFAULT_CONCURRENCY = 16
# vLLM batches continuously, so concurrency is close to free until KV runs out: this
# fleet holds ~450k KV tokens and a question costs a few thousand, so 16 is nowhere near
# the limit. At 8 a 140-item run took ~40 min, dominated by waiting on the slowest
# question in each wave — long enough that the regiment would simply get skipped.

# Generous on purpose. A reasoning model spends its budget getting to the answer, and a
# tight cap scores the BUDGET rather than the model — the same mistake that made the
# vision gate false-fail qwen3.6 with max_tokens=32.
MAX_TOKENS = 4096
# When a model runs out of budget mid-calculation it has REASONED but not ANSWERED.
# Scoring that as wrong measures the cap, not the model — and it is not rare: on
# qwen3.6/Qwen3-VL, 3 of 14 questions hit the cap, all of them maths-and-physics where
# the working is long. So on truncation we ask once more, cheaply, for the verdict,
# feeding its own reasoning back. Raising MAX_TOKENS alone only makes the slow questions
# slower without bounding the problem.
FOLLOWUP = "Based on your analysis above, which option is correct? Reply with only the letter."
# 512, not 64. Models ignore "do not explain" and write a full worked solution anyway —
# observed directly: the retry died mid-sentence at 64 tokens with `finish=length`, on
# text that was clearly heading for an answer. Fighting the verbosity with instructions
# does not work; giving it room does. Still cheap next to the 4096-token first attempt.
#
# Constrained decoding (`guided_choice`) would guarantee a valid letter and is the
# textbook answer — but DEF-0011 makes it 500 on the MTP profile, which is in this very
# sweep. A regiment that crashes on one of the models it is ranking is worse than a
# verbose retry.
# 2048, not 512. The retry disables thinking via `chat_template_kwargs`, which is a
# QWEN convention — Step-3.5 (StepFun, `step3p5` parser) ignores it, keeps reasoning,
# and blew through 512 on all 58 of its truncated items, so the rescue rescued nothing.
# A budget that assumes the model obeys is not a fallback. At 2048 a model that still
# cannot state a letter has genuinely failed the task rather than hit our ceiling.
FOLLOWUP_MAX_TOKENS = 2048
BRIEF = ("Answer immediately with the letter of the correct option, in the form "
         "\"Answer: X\". Do not show your working.")
# The rescue must not run away the same way the first attempt did. Where the model
# supports it, thinking is switched off for the retry only — the first attempt keeps
# full reasoning, so what is measured is still the model reasoning, with a terse
# fallback rather than a zero.
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}

PROMPT = """{question}

Options:
{options}

Answer with the letter of the correct option. End your reply with "Answer: X"."""

# "Answer: C", "the answer is (C)", "**Answer:** C" — the explicit forms, checked first
# and last-match-wins so a model that reconsiders mid-reasoning is scored on its verdict.
_EXPLICIT = re.compile(r"answer\s*(?:is)?\s*[:\-]?\s*\**\s*\(?([A-J])\)?\b", re.IGNORECASE)
# a bare letter standing alone, e.g. a final line that is just "C"
_STANDALONE = re.compile(r"(?:^|\n)\s*\**\(?([A-J])\)?\**\s*[.)]?\s*$", re.MULTILINE)


@dataclass
class ItemResult:
    id: int
    category: str
    expected: str
    got: str | None
    ok: bool
    seconds: float
    truncated: bool = False      # hit the token cap; reported separately from "no answer"
    rescued: bool = False        # truncated, then answered on the follow-up turn
    # Kept so a bad score can be INSPECTED rather than re-run. MiniMax returned 34%
    # unparseable on 2026-08-09 and there was no way to tell truncation from a format
    # the extractor missed — the run had to be thrown away. An aggregate number that
    # cannot be audited is not evidence.
    finish: str | None = None
    reply_tail: str | None = None
    # The RESCUE's own reply. Omitting it meant a failed rescue was as opaque as the
    # failure it was meant to fix — two diagnostic rounds spent on the same blind spot.
    rescue_tail: str | None = None
    rescue_finish: str | None = None


@dataclass
class EvalResult:
    items: list[ItemResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def scored(self) -> list[ItemResult]:
        return [i for i in self.items if i.got is not None]

    @property
    def unparseable(self) -> int:
        return len(self.items) - len(self.scored)

    @property
    def timed_out(self) -> int:
        """Requests that never returned. Distinct from truncation and from a bad answer:
        this one is OUR fault — the client gave up — and it is indistinguishable from a
        wrong answer in the score unless it is counted separately."""
        return sum(1 for i in self.items if (i.finish or "").startswith("error:"))

    @property
    def truncated(self) -> int:
        """Ran out of budget. Distinct from "gave no answer" — one is our cap, the other
        is the model, and conflating them hides which knob to turn."""
        return sum(1 for i in self.items if i.truncated)

    @property
    def rescued(self) -> int:
        return sum(1 for i in self.items if i.rescued)

    @property
    def accuracy(self) -> float:
        """Over ALL items. An unparseable answer counts as wrong — a model that cannot
        state its answer in the requested form has failed the task, and treating those
        as absent would let a rambling model outscore a clear one."""
        return (sum(1 for i in self.items if i.ok) / len(self.items)) if self.items else 0.0

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = {}
        for item in self.items:
            slot = out.setdefault(item.category, [0, 0])
            slot[0] += int(item.ok)
            slot[1] += 1
        return {c: (v[0], v[1]) for c, v in sorted(out.items())}


def load_items(limit: int | None = DEFAULT_LIMIT, path: Path = SUBSET) -> list[dict]:
    """Read the committed subset. The first line is provenance metadata, not an item."""
    items = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("_meta"):
            continue
        items.append(row)
    return items[:limit] if limit else items


def format_prompt(item: dict) -> str:
    options = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(item["options"]))
    return PROMPT.format(question=item["question"], options=options)


def extract_answer(text: str) -> str | None:
    """The model's chosen letter, or None if it never gave one.

    Explicit statements win over bare letters, and the LAST match wins over the first:
    reasoning models routinely say "option A looks plausible… actually the answer is C",
    and scoring the first mention would mark that wrong.
    """
    if not text:
        return None
    explicit = _EXPLICIT.findall(text)
    if explicit:
        return explicit[-1].upper()
    standalone = _STANDALONE.findall(text.strip())
    if standalone:
        return standalone[-1].upper()
    return None


def run(client, model: str, *, limit: int | None = DEFAULT_LIMIT,
        concurrency: int = DEFAULT_CONCURRENCY, on_item=None) -> EvalResult:
    """Score `model` against the subset. Concurrent because 280 sequential requests at
    several seconds each is half an hour — long enough that the regiment gets skipped."""
    items = load_items(limit)
    started = time.monotonic()

    def one(item: dict) -> ItemResult:
        t0 = time.monotonic()
        prompt = format_prompt(item)
        truncated = rescued = False
        finish = rescue_finish = rescue_tail = None
        try:
            messages = [{"role": "user", "content": prompt}]
            if hasattr(client, "stream_text"):
                # STREAM the first attempt. A reasoning model that hits the cap without
                # closing `</think>` returns empty content non-streaming — the tokens
                # were generated, they are just unsplittable — and the rescue then has
                # nothing to feed back. That is the difference between rescuing 3/3 and
                # 11/101 (2026-08-10, MiniMax + Step-3.5). See `api.stream_text`.
                text, finish = client.stream_text(messages, model=model,
                                                  max_tokens=MAX_TOKENS, temperature=0.0)
            else:
                reply = client.chat(messages, model=model,
                                    max_tokens=MAX_TOKENS, temperature=0.0)
                text = reply.content or reply.reasoning_content or ""
                finish = reply.raw.get("choices", [{}])[0].get("finish_reason")
            truncated = (finish == "length")
        except Exception as exc:
            text, finish = "", f"error:{type(exc).__name__}"
        got = extract_answer(text)
        if got is None and truncated:
            # It reasoned past the cap without concluding. Two shapes, and requiring text
            # here was a bug: a reasoning model that never closes its </think> block
            # returns 3072 generated tokens and EMPTY content — vLLM's parser cannot
            # split an unclosed block — so the rescue skipped precisely the case it
            # exists for (14 of 28 items, 2026-08-09).
            if text:
                # hand its own working back and ask only for the verdict
                messages = [{"role": "user", "content": prompt},
                            {"role": "assistant", "content": text},
                            {"role": "user", "content": FOLLOWUP}]
            else:
                # nothing survived the truncation; re-ask, demanding brevity so the
                # second attempt cannot run away the same way
                messages = [{"role": "user", "content": prompt + "\n\n" + BRIEF}]
            try:
                # The rescue must stream for exactly the reason the first attempt does.
                # Streaming only the first attempt fixed nothing measurable: on 2026-08-10
                # it took Step-3.5's empty truncations from 58/59 to 0/8 while the RESCUE
                # then returned empty 8 times out of 8, because a model that reasons past
                # the cap once will do it again on the retry — and `NO_THINKING` is a Qwen
                # convention StepFun ignores. Same defect, second call site.
                if hasattr(client, "stream_text"):
                    rescue_text, rescue_finish = client.stream_text(
                        messages, model=model, max_tokens=FOLLOWUP_MAX_TOKENS,
                        temperature=0.0, extra=NO_THINKING)
                else:
                    followup = client.chat(messages, model=model,
                                           max_tokens=FOLLOWUP_MAX_TOKENS, temperature=0.0,
                                           extra=NO_THINKING)
                    rescue_text = followup.content or followup.reasoning_content or ""
                    rescue_finish = followup.raw.get("choices", [{}])[0].get("finish_reason")
                rescue_tail = rescue_text[-200:]
                got = extract_answer(rescue_text)
                rescued = got is not None
            except Exception as exc:
                rescue_finish = f"error:{type(exc).__name__}"
        result = ItemResult(id=item["id"], category=item["category"],
                            expected=item["answer"], got=got,
                            ok=(got == item["answer"]), seconds=time.monotonic() - t0,
                            truncated=truncated, rescued=rescued,
                            finish=finish, reply_tail=(text or "")[-400:],
                            rescue_tail=rescue_tail, rescue_finish=rescue_finish)
        if on_item:
            on_item(result)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, items))
    return EvalResult(items=results, seconds=time.monotonic() - started)


def dump_items(result: EvalResult, path) -> None:
    """Write every item's outcome as JSONL, so an unexpected score can be diagnosed
    without re-running a 25-minute eval against a model that may no longer be live."""
    from pathlib import Path as _Path
    with _Path(path).open("w") as fh:
        for item in result.items:
            fh.write(json.dumps({
                "id": item.id, "category": item.category, "expected": item.expected,
                "got": item.got, "ok": item.ok, "finish": item.finish,
                "truncated": item.truncated, "rescued": item.rescued,
                "seconds": round(item.seconds, 1), "tail": item.reply_tail,
                "rescue_finish": item.rescue_finish, "rescue_tail": item.rescue_tail,
            }) + "\n")
