#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyarrow", "huggingface-hub"]
# ///
"""Generate the committed MMLU-Pro subset used by the `quality` regiment (ADR-0016).

Run ONCE; the output is committed. Re-running with the same seed reproduces it exactly.

**Why a committed subset rather than the dataset at run time.**
- *Comparability.* A score only means something against other scores on the SAME items.
  Sampling per run would make every comparison noise.
- *No runtime dependency.* The eval needs no `pyarrow`, no network, no HF cache — it
  reads a JSONL from the repo. The cluster evaluates models offline.
- *Auditability.* The exact questions are in git, so a surprising score can be inspected
  rather than guessed at.

**Balanced, not proportional.** 20 per category rather than MMLU-Pro's natural weighting
(which is a third maths and physics). We want to rank *our* models across domains, and a
per-category breakdown is more useful than matching an upstream mix.

**Interleaved by category**, so any prefix of the file is still balanced — `--limit 140`
gives 10 per category for free, and a short run is as representative as a long one.

⚠️ Scores from this subset are NOT comparable to published MMLU-Pro numbers: different
items, different count, different prompt. They are comparable to each other, which is
what the sweep needs.

Source: TIGER-Lab/MMLU-Pro (MIT). Provenance recorded in the file's header record.
"""

from __future__ import annotations

import glob
import json
import random
import subprocess
import sys
from pathlib import Path

SEED = 20260809
PER_CATEGORY = 20
OUT = Path(__file__).resolve().parent.parent / "sparky" / "data" / "mmlu_pro_subset.jsonl"
REPO = "TIGER-Lab/MMLU-Pro"


def main() -> int:
    import pyarrow.parquet as pq

    files = [p for p in glob.glob(
        str(Path.home() / ".cache/huggingface/hub/datasets--TIGER-Lab--MMLU-Pro/**/*.parquet"),
        recursive=True) if "test" in p]
    if not files:
        print(f"downloading {REPO} …")
        subprocess.run(["hf", "download", REPO, "--repo-type", "dataset"], check=True)
        return main()

    table = pq.read_table(files[0]).to_pydict()
    rows = [dict(zip(table, values)) for values in zip(*table.values())]
    print(f"read {len(rows)} questions from {Path(files[0]).name}")

    by_category: dict[str, list[dict]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)

    rng = random.Random(SEED)
    picked = {c: rng.sample(items, min(PER_CATEGORY, len(items)))
              for c, items in sorted(by_category.items())}

    # interleave: one from each category in turn, so any prefix stays balanced
    interleaved = []
    for i in range(PER_CATEGORY):
        for category in sorted(picked):
            if i < len(picked[category]):
                interleaved.append(picked[category][i])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        fh.write(json.dumps({
            "_meta": True, "source": REPO, "license": "MIT", "split": "test",
            "seed": SEED, "per_category": PER_CATEGORY, "count": len(interleaved),
            "note": "Balanced + interleaved; any prefix is category-balanced. "
                    "NOT comparable to published MMLU-Pro scores.",
        }) + "\n")
        for row in interleaved:
            fh.write(json.dumps({
                "id": int(row["question_id"]),
                "category": row["category"],
                "question": row["question"],
                "options": list(row["options"]),
                "answer": row["answer"],
            }) + "\n")

    print(f"wrote {len(interleaved)} items to {OUT.relative_to(OUT.parent.parent.parent)}")
    print(f"  categories: {len(picked)} x {PER_CATEGORY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
