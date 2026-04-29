"""Render the 'Retrieval quality' section in README.md from bench JSON files.

Reads three bench JSONs (scales 80 / 1000 / 5000), generates a plain-language
section with one big simple table, and replaces the text between the markers:

    <!-- recall-quality:start -->
    ...auto-generated content...
    <!-- recall-quality:end -->

inside README.md. Idempotent. If markers don't exist, exits 0 with a warning
(README will be hand-edited on first integration to add them).

Used by .github/workflows/recall-bench-update.yml after a PR merges to main.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from pathlib import Path

START = "<!-- recall-quality:start -->"
END = "<!-- recall-quality:end -->"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find(results: list[dict], name: str) -> dict | None:
    for r in results:
        if r["name"] == name:
            return r
    return None


def fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render(b80: dict, b1000: dict, b5000: dict) -> str:
    timestamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")

    rows = []
    for label, bench in (("80 lessons (you today)", b80), ("1,000 lessons", b1000), ("5,000 lessons", b5000)):
        results = bench["results"]
        truncated = find(results, "Without recall (index, truncated 200 lines)")
        full = find(results, "Without recall (index, full)")
        hybrid = find(results, "With recall (hybrid, warm)")
        if not (truncated and full and hybrid):
            continue
        rows.append(
            f"| **{label}** | {fmt_pct(truncated['bucket_paraphrase'])} | "
            f"{fmt_pct(full['bucket_paraphrase'])} | "
            f"**{fmt_pct(hybrid['bucket_paraphrase'])}** | {hybrid['p50_ms']:.1f} ms |"
        )

    body = f"""### Retrieval quality (auto-updated by CI on every merge to main)

Last refresh: **{timestamp}** (scale 80 / 1,000 / 5,000 synthetic lessons).

The single number that matters: **how often does the retriever surface a
relevant lesson in the top 5 results, when the user asks a paraphrased
question** (a question that doesn't share words with the lesson title)?

| Brain size | Today<sup>1</sup> | Best case<sup>2</sup> | With `recall` (hybrid)<sup>3</sup> | Latency |
|---|---|---|---|---|
{chr(10).join(rows)}

<sup>1</sup> What you get if you only have `MEMORY.md` auto-loaded — the index
truncates at 200 lines, so past ~150 lessons most of your brain is invisible
to the LLM.
<sup>2</sup> Optimistic baseline: the LLM somehow has the *full* MEMORY.md in
context (e.g. you `Read` it explicitly). Even then, lexical matching tops out.
<sup>3</sup> Hybrid retrieval = BM25 keyword search + sentence-transformer
embeddings, fused with Reciprocal Rank Fusion. Indexes the full body of every
lesson, not just the description column.

**Latency** is per-query wall clock, warm-cache, on the CI runner. Add the
embedding-model load (~90 MB, one-time on first call) for cold start.

**Numbers come from `tests/recall/bench_e2e.py`** (synthetic corpus, fixed
seed — re-runs produce the same results). PRs that touch `recall/` are
gated on this metric: a PR fails CI if hybrid bucket-paraphrase recall@5
drops by more than 5 percentage points vs. the baseline checked in at
`tests/recall/bench_baseline.json`. See `tests/recall/BENCH_RESULTS.md`
for the full per-strategy breakdown."""
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-80", required=True, type=Path)
    parser.add_argument("--bench-1000", required=True, type=Path)
    parser.add_argument("--bench-5000", required=True, type=Path)
    parser.add_argument("--readme", required=True, type=Path)
    args = parser.parse_args()

    b80 = load(args.bench_80)
    b1000 = load(args.bench_1000)
    b5000 = load(args.bench_5000)

    body = render(b80, b1000, b5000)

    text = args.readme.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print(
            f"WARNING: markers '{START}' / '{END}' not found in {args.readme}; "
            "skipping. Add the markers to enable auto-updates.",
        )
        return 0

    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    new_block = f"{START}\n\n{body}\n\n{END}"
    new_text = pattern.sub(new_block, text)

    if new_text == text:
        print("README quality section is already up to date.")
        return 0

    args.readme.write_text(new_text, encoding="utf-8")
    print(f"Updated quality section in {args.readme}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
