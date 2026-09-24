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
                  doc is in the top-k. Computed over questions that carry
                  `supports` labels only.
  - MRR : mean reciprocal rank of the first supporting doc.
  - answer-coverage@5 : fraction of questions whose answer substring appears
                  in the concatenated top-5 bodies. This is the
                  "re-explanation avoided" proxy: if the fact is in the
                  injected context, the user does not have to re-explain it.
                  (Eywa-style retrieval-sufficiency: the top-k context is
                  sufficient to answer.)
  - stale-ahead rate / stale-answer-coverage@5 : only for datasets with
                  contradiction labels (`stale`, `stale_answer_substring`).
                  stale-ahead = an outdated doc ranks above the current one
                  (or the current one is absent); stale-answer-coverage = the
                  OUTDATED answer text is present in top-5 — the "agent would
                  repeat a stale fact" probe (HaluMem-style, retrieval-side).

Datasets: the shipped labeled set (default), LongMemEval v1 JSON, a prepared
LongMemEval-V2 data root (pass the directory), or any native-format JSON.
Conversion lives in eval/longmemeval_convert.py; per-question `docs` entries
restrict retrieval to that question's own haystack.

This is a retrieval-grounded benchmark, NOT an end-to-end task-success score
with an LLM judge. It is reproducible offline (ships its own labeled set).

Usage:
  python eval/bench_recall_ab.py                 # ships labeled set
  python eval/bench_recall_ab.py --dataset X.json [--limit 50]
  python eval/bench_recall_ab.py --dataset eval/data/longmemeval_s_cleaned.json
  python eval/bench_recall_ab.py --dataset eval/data/lme_v2   # V2 root dir
  python eval/bench_recall_ab.py --write-results --dataset X.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

RESULTS_MD = _REPO_ROOT / "eval" / "RESULTS.md"
RESULTS_START = "<!-- bench-ab:start -->"
RESULTS_END = "<!-- bench-ab:end -->"


def load_dataset(path: Path) -> dict:
    """Load a dataset: native JSON, LongMemEval v1 JSON, or a V2 data root dir."""
    from eval.longmemeval_convert import convert_v1, convert_v2

    if path.is_dir():
        return convert_v2(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return convert_v1(data)
    if isinstance(data, dict) and "corpus" in data and "questions" in data:
        return data
    raise SystemExit(
        f"{path}: unrecognized dataset — expected native {{corpus, questions}} "
        "JSON, a LongMemEval v1 JSON list, or a LongMemEval-V2 data directory"
    )


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


def coverage(top_text: str, substring: object) -> bool:
    """True when `substring` appears in `top_text` (case-insensitive).

    `substring` is coerced with str(): real LongMemEval answers are not
    always strings (numeric answers arrive as int), and a non-string answer
    must not crash the run.
    """
    if not isinstance(top_text, str):
        top_text = str(top_text)
    if substring is None:
        return False
    sub = str(substring)
    if not sub:
        return False
    return sub.lower() in top_text.lower()


def compute_rank_row(ranked_slugs: list[str], top_text: str, item: dict) -> dict:
    """Pure per-question metric row.

    first_support_rank / first_stale_rank are 1-based, 0 when absent.
    stale_ahead is None when the question carries no stale labels.
    """
    supports = set(item.get("supports", []))
    stale = set(item.get("stale", []))

    def _first_rank(wanted: set[str]) -> int:
        for i, slug in enumerate(ranked_slugs, start=1):
            if slug in wanted:
                return i
        return 0

    first_support = _first_rank(supports) if supports else 0
    first_stale = _first_rank(stale) if stale else 0
    if not stale:
        stale_ahead = None
        stale_solo = None
    else:
        # Strict ranking comparison: both docs retrieved, stale ranks higher.
        # A stale doc retrieved while the current one is absent entirely is a
        # different failure mode (retrieval miss, not outranking) and is
        # reported separately as stale_solo.
        stale_ahead = bool(
            first_stale and first_support and first_stale < first_support
        )
        stale_solo = bool(first_stale and supports and not first_support)
    return {
        "id": item.get("id", ""),
        "q": item.get("q", ""),
        "question_type": item.get("question_type", ""),
        "has_supports": bool(supports),
        "first_support_rank": first_support,
        "first_stale_rank": first_stale,
        "stale_ahead": stale_ahead,
        "stale_solo": stale_solo,
        "answer_covered": coverage(top_text, item.get("answer_substring", "")),
        "stale_answer_covered": coverage(
            top_text, item.get("stale_answer_substring", "")
        ),
    }


def aggregate_metrics(rows: list[dict], n_docs: int) -> dict:
    """Pure aggregation. recall@k/MRR are scoped to questions with supports;
    coverage is over all questions; stale metrics over stale-labelled ones."""
    n = len(rows)
    supported = [r for r in rows if r["has_supports"]]
    n_sup = len(supported)

    def _hits(kk: int) -> int:
        return sum(1 for r in supported if 0 < r["first_support_rank"] <= kk)

    rr_sum = sum(
        (1.0 / r["first_support_rank"]) for r in supported if r["first_support_rank"]
    )
    stale_rows = [r for r in rows if r["stale_ahead"] is not None]

    def _by_type() -> dict:
        out: dict[str, dict] = {}
        for r in rows:
            t = r.get("question_type") or ""
            agg = out.setdefault(t, {"n": 0, "covered": 0, "sup_n": 0, "sup_hit5": 0})
            agg["n"] += 1
            agg["covered"] += int(r["answer_covered"])
            if r["has_supports"]:
                agg["sup_n"] += 1
                agg["sup_hit5"] += int(0 < r["first_support_rank"] <= 5)
        return {
            t: {
                "n": a["n"],
                "answer_coverage@5": round(a["covered"] / a["n"], 3),
                **(
                    {"recall@5": round(a["sup_hit5"] / a["sup_n"], 3)}
                    if a["sup_n"]
                    else {}
                ),
            }
            for t, a in sorted(out.items())
        }

    metrics = {
        "n_questions": n,
        "n_docs": n_docs,
        "n_with_supports": n_sup,
        "recall@1": round(_hits(1) / n_sup, 3) if n_sup else 0.0,
        "recall@3": round(_hits(3) / n_sup, 3) if n_sup else 0.0,
        "recall@5": round(_hits(5) / n_sup, 3) if n_sup else 0.0,
        "mrr": round(rr_sum / n_sup, 3) if n_sup else 0.0,
        "answer_coverage@5": round(sum(r["answer_covered"] for r in rows) / n, 3)
        if n
        else 0.0,
        "by_type": _by_type(),
    }
    if stale_rows:
        ns = len(stale_rows)
        both = [r for r in stale_rows if r["first_support_rank"] and r["first_stale_rank"]]
        metrics["n_stale_labelled"] = ns
        # Strict: of questions where BOTH docs were retrieved, how often the
        # stale one ranked higher.
        metrics["stale_ahead_rate"] = round(
            sum(1 for r in both if r["stale_ahead"]) / len(both), 3
        ) if both else 0.0
        # Solo: stale doc retrieved while the current one was missed entirely.
        metrics["stale_solo_rate"] = round(
            sum(1 for r in stale_rows if r["stale_solo"]) / ns, 3
        )
        # Headline contamination: injected context leads with stale info
        # (outranked OR solo).
        metrics["stale_contamination_rate"] = round(
            sum(1 for r in stale_rows if r["stale_ahead"] or r["stale_solo"]) / ns, 3
        )
        metrics["stale_answer_coverage@5"] = round(
            sum(r["stale_answer_covered"] for r in stale_rows) / ns, 3
        )
    return metrics


def run_benchmark(
    data: dict, k: int = 5, limit: int | None = None, shard: tuple[int, int] | None = None
) -> dict:
    from recall.core import HybridRetriever

    docs = _docs_from_corpus(data["corpus"])
    if not docs:
        raise SystemExit("dataset corpus is empty — nothing to retrieve from")
    docmap = {d.path: d for d in docs}
    questions = data["questions"][:limit] if limit else data["questions"]
    if shard is not None:
        i, n = shard
        # Strided slicing is deterministic and keeps shard corpora disjoint.
        questions = [q for idx, q in enumerate(questions) if idx % n == i]

    # One retriever per distinct per-question haystack; questions without a
    # `docs` list share the whole corpus (sentinel key None). Each distinct
    # haystack gets its OWN collection: HybridRetriever indexes Documents by
    # `source` (not by the collections arg) and unions source into the
    # queryable collections, so per-haystack isolation requires BOTH a unique
    # collection name AND `source` set to that name on the docs. (Observed
    # bug: source="bench" accumulated every question's docs into one shared
    # collection — later questions retrieved earlier questions' haystacks,
    # and the per-upsert metadata scan went quadratic.)
    import hashlib
    from dataclasses import replace

    retrievers: dict = {}

    def _retriever_for(item: dict):
        slugs = item.get("docs")
        key = None if slugs is None else tuple(sorted(slugs))
        if key not in retrievers:
            if slugs is None:
                subset, collection = docs, "bench"
            else:
                digest = hashlib.sha1("|".join(key).encode()).hexdigest()[:12]
                collection = f"bench-{digest}"
                subset = [replace(docmap[s], source=collection) for s in slugs]
            retrievers[key] = HybridRetriever(
                documents=subset, collections=[collection]
            )
        return retrievers[key]

    rows = []
    for item in questions:
        slugs = item.get("docs")
        if slugs is not None and not slugs:
            # A question with an explicitly empty haystack: no retrieval is
            # possible; record the all-miss row instead of building an empty
            # retriever (which would construct a useless Qdrant collection).
            row = compute_rank_row([], "", item)
            row["supports"] = sorted(set(item.get("supports", [])))
            row["ranked"] = []
            rows.append(row)
            continue
        retriever = _retriever_for(item)
        results = retriever.query(item["q"], k=max(k, 5))
        ranked_slugs = [r.document.path for r in results]
        top_text = "\n".join(r.document.body for r in results[:5])
        row = compute_rank_row(ranked_slugs, top_text, item)
        row["supports"] = sorted(set(item.get("supports", [])))
        row["ranked"] = ranked_slugs[:5]
        rows.append(row)

    metrics = aggregate_metrics(rows, n_docs=len(docs))
    metrics["baseline_no_memory"] = {
        "recall@5": 0.0,
        "mrr": 0.0,
        "answer_coverage@5": 0.0,
        "note": "empty brain surfaces nothing; 0 by construction",
    }
    metrics["per_question"] = rows
    return metrics


def _results_block(dataset_name: str, result: dict) -> str:
    lines = [
        f"## LongMemEval run ({result['n_questions']} questions, {result['n_docs']} docs)",
        "",
        f"Dataset: `{dataset_name}` · date: {date.today().isoformat()} · "
        f"harness: `bench_recall_ab.py` (retrieval-grounded, no LLM judge)",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| recall@1 | {result['recall@1']} |",
        f"| recall@3 | {result['recall@3']} |",
        f"| recall@5 | {result['recall@5']} |",
        f"| MRR | {result['mrr']} |",
        f"| answer-coverage@5 (retrieval sufficiency) | {result['answer_coverage@5']} |",
    ]
    if "stale_contamination_rate" in result:
        lines += [
            f"| stale-contamination rate | {result['stale_contamination_rate']} |",
            f"| stale-ahead rate (strict) | {result['stale_ahead_rate']} |",
            f"| stale-solo rate | {result['stale_solo_rate']} |",
            f"| stale-answer-coverage@5 | {result['stale_answer_coverage@5']} |",
        ]
    if result.get("by_type"):
        lines += ["", "| question_type | n | recall@5 | answer-coverage@5 |", "|---|---:|---:|---:|"]
        for t, a in result["by_type"].items():
            lines.append(
                f"| {t or '(untyped)'} | {a['n']} | {a.get('recall@5', '—')} "
                f"| {a['answer_coverage@5']} |"
            )
    return "\n".join(lines)


def merge_shard_results(shard_results: list[dict]) -> dict:
    """Merge per-question rows from --shard runs and recompute aggregates.

    Pure: aggregation is derived from the merged rows, so a sharded run and a
    single-process run over the same questions produce identical metrics.
    """
    rows = [row for r in shard_results for row in r["per_question"]]
    n_docs = shard_results[0]["n_docs"] if shard_results else 0
    merged = aggregate_metrics(rows, n_docs=n_docs)
    merged["baseline_no_memory"] = {
        "recall@5": 0.0,
        "mrr": 0.0,
        "answer_coverage@5": 0.0,
        "note": "empty brain surfaces nothing; 0 by construction",
    }
    merged["per_question"] = rows
    merged["n_shards"] = len(shard_results)
    return merged


def write_results_block(path: Path, block_text: str) -> None:
    """Replace the marked bench block in RESULTS.md (append when absent)."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f"{RESULTS_START}\n{block_text}\n{RESULTS_END}"
    if RESULTS_START in text and RESULTS_END in text:
        pre = text.split(RESULTS_START, 1)[0]
        post = text.split(RESULTS_END, 1)[1]
        path.write_text(pre + block + post, encoding="utf-8")
    else:
        sep = "" if text.endswith("\n") else "\n"
        prefix = text + sep if text else ""
        path.write_text(prefix + "\n" + block + "\n" if prefix else block + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=Path(__file__).parent / "bench_dataset.json")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--limit", type=int, default=None, help="run only the first N questions")
    ap.add_argument(
        "--shard",
        type=str,
        default=None,
        metavar="I/N",
        help="run only shard I of N (0-based, strided; deterministic). Shard "
        "outputs merge with --merge.",
    )
    ap.add_argument(
        "--merge",
        type=Path,
        nargs="+",
        default=None,
        metavar="SHARD_JSON",
        help="merge --json outputs of shard runs instead of running a bench",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a human summary")
    ap.add_argument(
        "--write-results",
        action="store_true",
        help=f"replace the {RESULTS_START} block in {RESULTS_MD}",
    )
    args = ap.parse_args()

    if args.merge:
        shard_results = [
            json.loads(p.read_text(encoding="utf-8")) for p in args.merge
        ]
        result = merge_shard_results(shard_results)
        if args.write_results:
            write_results_block(RESULTS_MD, _results_block("merged shards", result))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"merged {result['n_shards']} shards: {result['n_questions']} questions")
            print(f"  recall@5          {result['recall@5']:.3f}")
            print(f"  MRR               {result['mrr']:.3f}")
            print(f"  answer-coverage@5 {result['answer_coverage@5']:.3f}")
        return

    shard = None
    if args.shard:
        try:
            i_str, n_str = args.shard.split("/")
            shard = (int(i_str), int(n_str))
        except ValueError:
            raise SystemExit(f"--shard must be I/N with 0 <= I < N, got {args.shard!r}")
        if not (0 <= shard[0] < shard[1]):
            raise SystemExit(f"--shard must satisfy 0 <= I < N, got {args.shard!r}")

    data = load_dataset(args.dataset)

    # Isolate the index cache so the bench never touches a real brain, while
    # reusing any cached embedding model so it does not re-download.
    with tempfile.TemporaryDirectory(prefix="recall-bench-") as tmp:
        os.environ["XDG_CACHE_HOME"] = tmp
        os.environ.setdefault(
            "FASTEMBED_CACHE_PATH",
            str(Path.home() / ".cache" / "fastembed"),
        )
        result = run_benchmark(data, k=args.k, limit=args.limit, shard=shard)

    if args.write_results:
        write_results_block(RESULTS_MD, _results_block(args.dataset.name, result))

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
    print(f"    answer-coverage@5   {result['answer_coverage@5']:.3f}  (retrieval sufficiency)")
    if "stale_contamination_rate" in result:
        print(f"    stale-contamination {result['stale_contamination_rate']:.3f}  (context leads with outdated info)")
        print(f"      strict-ahead      {result['stale_ahead_rate']:.3f}  (outdated outranks current)")
        print(f"      solo              {result['stale_solo_rate']:.3f}  (outdated retrieved, current missed)")
        print(f"    stale-answer-cov@5  {result['stale_answer_coverage@5']:.3f}  (outdated answer in context)")
    print()
    print("  condition A (no memory): 0.000 on every metric, by construction.")
    print()
    misses = [p for p in result["per_question"] if p["has_supports"] and not p["first_support_rank"]]
    if misses:
        print(f"  {len(misses)} question(s) with no supporting doc in top-5:")
        for p in misses[:20]:
            print(f"    - {p['q']}")
        if len(misses) > 20:
            print(f"    … and {len(misses) - 20} more")
    else:
        print("  every question surfaced its supporting doc within top-5.")


if __name__ == "__main__":
    main()
