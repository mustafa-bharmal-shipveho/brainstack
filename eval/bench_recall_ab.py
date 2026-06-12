#!/usr/bin/env python3
"""Auto-recall A/B benchmark: does recall surface the memory that answers a question?

The honest precondition for "memory makes the agent better" is "memory
surfaces the RIGHT note when asked." This harness measures exactly that on a
labeled set, comparing two conditions:

  A (no memory):  the baseline an agent has with an empty brain. By
                  construction it surfaces nothing, so every memory-grounded
                  metric is 0. Stated explicitly rather than run, because the
                  contrast is the point.
  B (with recall): build a HybridRetriever over the labeled corpus and query
                  it with each question.

Metrics (condition B):
  - recall@1 / recall@3 / recall@5 : fraction of questions whose supporting
                  doc is in the top-k.
  - MRR : mean reciprocal rank of the first supporting doc.
  - answer-coverage@5 : fraction of questions whose answer substring appears
                  in the concatenated top-5 bodies. This is the
                  "re-explanation avoided" proxy: if the fact is in the
                  injected context, the user does not have to re-explain it.

This is a retrieval-grounded benchmark, NOT an end-to-end task-success score
with an LLM judge. It is reproducible offline (ships its own labeled set) and
runs in seconds once the embedding model is cached. The same harness ingests
LongMemEval-style JSON via --dataset so the public benchmark can be run with
the identical code path (see eval/README.md).

Usage:
  python eval/bench_recall_ab.py                 # ships labeled set
  python eval/bench_recall_ab.py --dataset X.json
  python eval/bench_recall_ab.py --k 5 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_dataset(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "corpus" not in data or "questions" not in data:
        raise SystemExit(f"{path}: dataset needs 'corpus' and 'questions' keys")
    return data


def _docs_from_corpus(corpus: list[dict]):
    """Build recall Documents from the labeled corpus. The slug is stored as
    the path so we can check whether a retrieved doc is a supporting one."""
    from recall.core import Document

    docs = []
    for entry in corpus:
        slug = entry["slug"]
        title = entry.get("title", slug)
        body = entry["body"]
        text = f"{title}\n\n{body}"
        docs.append(
            Document(
                path=slug,
                source="bench",
                title=title,
                frontmatter={},
                body=body,
                text=text,
            )
        )
    return docs


def run_benchmark(data: dict, k: int = 5) -> dict:
    from recall.core import HybridRetriever

    docs = _docs_from_corpus(data["corpus"])
    questions = data["questions"]

    retriever = HybridRetriever(documents=docs, collections=["bench"])

    n = len(questions)
    hits_at = {1: 0, 3: 0, 5: 0}
    rr_sum = 0.0
    answer_cov = 0
    per_q = []

    for item in questions:
        q = item["q"]
        supports = set(item.get("supports", []))
        answer_sub = item.get("answer_substring", "")

        results = retriever.query(q, k=max(k, 5))
        ranked_slugs = [r.document.path for r in results]

        # rank of first supporting doc (1-based), 0 if not found
        first_rank = 0
        for i, slug in enumerate(ranked_slugs, start=1):
            if slug in supports:
                first_rank = i
                break
        for kk in hits_at:
            if first_rank and first_rank <= kk:
                hits_at[kk] += 1
        rr_sum += (1.0 / first_rank) if first_rank else 0.0

        top5_text = "\n".join(r.document.body for r in results[:5]).lower()
        covered = bool(answer_sub) and answer_sub.lower() in top5_text
        if covered:
            answer_cov += 1

        per_q.append({
            "q": q,
            "supports": sorted(supports),
            "first_rank": first_rank,
            "answer_covered": covered,
        })

    return {
        "n_questions": n,
        "n_docs": len(docs),
        "recall@1": round(hits_at[1] / n, 3) if n else 0.0,
        "recall@3": round(hits_at[3] / n, 3) if n else 0.0,
        "recall@5": round(hits_at[5] / n, 3) if n else 0.0,
        "mrr": round(rr_sum / n, 3) if n else 0.0,
        "answer_coverage@5": round(answer_cov / n, 3) if n else 0.0,
        "baseline_no_memory": {
            "recall@5": 0.0, "mrr": 0.0, "answer_coverage@5": 0.0,
            "note": "empty brain surfaces nothing; 0 by construction",
        },
        "per_question": per_q,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=Path(__file__).parent / "bench_dataset.json")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a human summary")
    args = ap.parse_args()

    data = _load_dataset(args.dataset)

    # Isolate the index cache so the bench never touches a real brain, while
    # reusing any cached embedding model so it does not re-download.
    with tempfile.TemporaryDirectory(prefix="recall-bench-") as tmp:
        os.environ["XDG_CACHE_HOME"] = tmp
        os.environ.setdefault(
            "FASTEMBED_CACHE_PATH",
            str(Path.home() / ".cache" / "fastembed"),
        )
        result = run_benchmark(data, k=args.k)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print("== recall A/B benchmark ==")
    print(f"  dataset: {args.dataset.name}  ({result['n_docs']} docs, {result['n_questions']} questions)")
    print()
    print("  condition B (with recall):")
    print(f"    recall@1            {result['recall@1']:.3f}")
    print(f"    recall@3            {result['recall@3']:.3f}")
    print(f"    recall@5            {result['recall@5']:.3f}")
    print(f"    MRR                 {result['mrr']:.3f}")
    print(f"    answer-coverage@5   {result['answer_coverage@5']:.3f}  (re-explanation-avoided proxy)")
    print()
    print("  condition A (no memory): 0.000 on every metric, by construction.")
    print()
    misses = [p for p in result["per_question"] if not p["first_rank"]]
    if misses:
        print(f"  {len(misses)} question(s) with no supporting doc in top-5:")
        for p in misses:
            print(f"    - {p['q']}")
    else:
        print("  every question surfaced its supporting doc within top-5.")


if __name__ == "__main__":
    main()
