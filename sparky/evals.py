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
MAX_TOKENS = 3072
# When a model runs out of budget mid-calculation it has REASONED but not ANSWERED.
# Scoring that as wrong measures the cap, not the model — and it is not rare: on
# qwen3.6/Qwen3-VL, 3 of 14 questions hit the cap, all of them maths-and-physics where
# the working is long. So on truncation we ask once more, cheaply, for the verdict,
# feeding its own reasoning back. Raising MAX_TOKENS alone only makes the slow questions
# slower without bounding the problem.
FOLLOWUP = "Based on your analysis above, which option is correct? Reply with only the letter."
FOLLOWUP_MAX_TOKENS = 32

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
        try:
            reply = client.chat([{"role": "user", "content": prompt}],
                                model=model, max_tokens=MAX_TOKENS, temperature=0.0)
            text = reply.content or reply.reasoning_content or ""
            truncated = (reply.raw.get("choices", [{}])[0].get("finish_reason") == "length")
        except Exception:
            text = ""
        got = extract_answer(text)
        if got is None and truncated and text:
            # It reasoned but never concluded. Hand its own working back and ask for the
            # verdict — a short generation, so this costs little even when it fails.
            try:
                followup = client.chat(
                    [{"role": "user", "content": prompt},
                     {"role": "assistant", "content": text},
                     {"role": "user", "content": FOLLOWUP}],
                    model=model, max_tokens=FOLLOWUP_MAX_TOKENS, temperature=0.0)
                got = extract_answer(followup.content or "")
                rescued = got is not None
            except Exception:
                pass
        result = ItemResult(id=item["id"], category=item["category"],
                            expected=item["answer"], got=got,
                            ok=(got == item["answer"]), seconds=time.monotonic() - t0,
                            truncated=truncated, rescued=rescued)
        if on_item:
            on_item(result)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, items))
    return EvalResult(items=results, seconds=time.monotonic() - started)
