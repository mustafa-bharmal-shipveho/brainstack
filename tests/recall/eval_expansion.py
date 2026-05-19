"""Query-expansion eval: retrieve UNION of top-K across N query variants.

For each (query_variants, expected_doc) pair:
  1. Run each variant through HybridRetriever, get top-K=10 results
  2. Merge: a doc's union-rank = best rank it achieved across variants
     (so if doc D was rank 8 for variant 1 and rank 2 for variant 3, D's
      best rank = 2)
  3. Score Recall@1, Recall@5, Recall@10, MRR@10, NDCG@10 against the union ranking.

This tests: does giving the retriever multiple shots at semantic matching
fix the retrieval-breadth bottleneck without changing the embedder?

Run:

    BRAIN_ROOT=$HOME/.agent python tests/recall/eval_expansion.py \\
        --golden tests/recall/golden/real_brain_v2_hard_expanded.jsonl \\
        --out tests/recall/eval_runs/expansion_no_rerank.json \\
        [--rerank cross_encoder]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from recall.config import load_config
from recall.core import HybridRetriever
from recall.index import build_index, load_index, needs_refresh


def _ndcg_single_relevant(rank):
    if rank is None:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument(
        "--per-variant-k",
        type=int,
        default=10,
        help="Top-K to pull per variant before merging. Bigger = wider net.",
    )
    ap.add_argument("--rerank", default=None)
    ap.add_argument("--label", default="expansion")
    ap.add_argument("--max-variants", type=int, default=4,
                    help="Cap number of query variants used (1 = no expansion).")
    ap.add_argument(
        "--merge",
        choices=["best-rank", "rrf", "rrf-pinned"],
        default="best-rank",
        help=(
            "How to merge per-variant results. rrf=Reciprocal Rank Fusion "
            "(k=60). rrf-pinned=RRF but force the FIRST variant's top-1 to "
            "position 0 (caps Recall@1 floor at the original query's value)."
        ),
    )
    ap.add_argument(
        "--post-rerank",
        action="store_true",
        help="After building the union, rerank the WHOLE union against the "
             "ORIGINAL query (variant 0) using a cross-encoder. Mutually "
             "exclusive with --rerank (which reranks each variant separately).",
    )
    ap.add_argument(
        "--rerank-model",
        default=None,
        help="Override the cross-encoder model. Examples: "
             "BAAI/bge-reranker-base, jinaai/jina-reranker-v2-base-multilingual",
    )
    args = ap.parse_args()

    golden = [json.loads(l) for l in Path(args.golden).read_text().splitlines() if l.strip()]
    print(f"loaded {len(golden)} golden pairs (each has up to 4 variants)")

    cfg = load_config()
    cache = build_index(cfg.sources) if needs_refresh(cfg.sources) else load_index(cfg.sources)
    fresh = cache is None or not cache.documents or needs_refresh(cfg.sources)
    print(f"index: {len(cache.documents) if cache else 0} docs (fresh={fresh})")

    effective_reranker = args.rerank if args.rerank is not None else cfg.ranking.reranker
    retriever = HybridRetriever(
        documents=cache.documents if fresh else None,
        collections=[s.name for s in cfg.sources],
        embedder=cfg.ranking.embedder,
        sparse_embedder=cfg.ranking.sparse_embedder,
        reranker=effective_reranker,
        reranker_model=cfg.ranking.reranker_model,
        rerank_n=cfg.ranking.rerank_n,
    )
    print(f"retriever ready (reranker={effective_reranker}, "
          f"max_variants={args.max_variants}, per_variant_k={args.per_variant_k})")

    # If post-rerank requested, lazy-load the cross-encoder via the
    # existing singleton API. Override the model if --rerank-model was passed.
    post_rerank_fn = None
    if args.post_rerank:
        from recall import qdrant_backend
        model_name = args.rerank_model or cfg.ranking.reranker_model
        print(f"post-rerank model: {model_name}")
        ce = qdrant_backend._get_cross_encoder(model_name)

        def post_rerank_fn(query: str, doc_texts: list[str]) -> list[float]:
            return list(ce.rerank(query, doc_texts))

    # Need a path → Document map so we can rerank by full text.
    path_to_doc = {d.path: d for d in (cache.documents or [])}

    per_query = []
    latencies = []
    for pair in golden:
        variants = pair["all_query_variants"][: args.max_variants]
        original_query = variants[0]
        t0 = time.perf_counter()

        # Collect per-variant ranked results.
        per_variant: list[list] = []
        for v in variants:
            results = retriever.query(v, k=args.per_variant_k)
            per_variant.append(results)

        # Merge.
        if args.merge == "best-rank":
            path_to_best: dict[str, int] = {}
            for results in per_variant:
                for rank, r in enumerate(results, start=1):
                    p = r.document.path
                    if p not in path_to_best or rank < path_to_best[p]:
                        path_to_best[p] = rank
            union_sorted = sorted(path_to_best.items(), key=lambda kv: kv[1])
            union_paths = [p for p, _ in union_sorted]
        elif args.merge == "rrf":
            # Reciprocal Rank Fusion. k=60 is the standard value (TREC).
            K_RRF = 60
            path_to_score: dict[str, float] = {}
            for results in per_variant:
                for rank, r in enumerate(results, start=1):
                    p = r.document.path
                    path_to_score[p] = path_to_score.get(p, 0.0) + 1.0 / (K_RRF + rank)
            union_sorted = sorted(path_to_score.items(), key=lambda kv: kv[1], reverse=True)
            union_paths = [p for p, _ in union_sorted]
        elif args.merge == "rrf-pinned":
            # RRF, but force the FIRST variant's top-1 to position 0.
            # Caps the Recall@1 floor at the original query's baseline.
            K_RRF = 60
            path_to_score: dict[str, float] = {}
            for results in per_variant:
                for rank, r in enumerate(results, start=1):
                    p = r.document.path
                    path_to_score[p] = path_to_score.get(p, 0.0) + 1.0 / (K_RRF + rank)
            union_sorted = sorted(path_to_score.items(), key=lambda kv: kv[1], reverse=True)
            union_paths = [p for p, _ in union_sorted]
            if per_variant and per_variant[0]:
                anchor = per_variant[0][0].document.path
                if union_paths and union_paths[0] != anchor and anchor in union_paths:
                    union_paths.remove(anchor)
                    union_paths.insert(0, anchor)
        else:
            raise SystemExit(f"unknown merge: {args.merge}")

        # Post-hoc rerank the WHOLE union against the original query.
        if post_rerank_fn is not None and union_paths:
            docs_for_rerank = [path_to_doc[p].text for p in union_paths if p in path_to_doc]
            paths_for_rerank = [p for p in union_paths if p in path_to_doc]
            if paths_for_rerank:
                scores = post_rerank_fn(original_query, docs_for_rerank)
                reranked = sorted(
                    zip(paths_for_rerank, scores), key=lambda kv: kv[1], reverse=True
                )
                union_paths = [p for p, _ in reranked]

        latency_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(latency_ms)

        expected = Path(pair["expected_doc_abs"]).resolve()
        rank = None
        for i, p in enumerate(union_paths, start=1):
            if Path(p).resolve() == expected:
                rank = i
                break

        per_query.append({
            "query": pair["query"],
            "n_variants": len(variants),
            "expected_doc": pair["expected_doc"],
            "rank": rank,
            "latency_ms": round(latency_ms, 1),
            "union_size": len(union_paths),
        })

    n = len(per_query)
    hit_at = lambda k: sum(1 for r in per_query if r["rank"] is not None and r["rank"] <= k)

    metrics = {
        "n": n,
        "label": args.label,
        "max_variants": args.max_variants,
        "per_variant_k": args.per_variant_k,
        "reranker": effective_reranker,
        "recall_at_1": round(hit_at(1) / n, 4),
        "recall_at_5": round(hit_at(5) / n, 4),
        "recall_at_10": round(hit_at(10) / n, 4),
        "mrr_at_10": round(
            sum(1.0 / r["rank"] for r in per_query if r["rank"] is not None and r["rank"] <= 10) / n,
            4,
        ),
        "ndcg_at_10": round(
            sum(_ndcg_single_relevant(r["rank"]) for r in per_query if r["rank"] is not None and r["rank"] <= 10) / n,
            4,
        ),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(sorted(latencies)[int(0.95 * n)] if n else 0.0, 1),
        "avg_union_size": round(sum(r["union_size"] for r in per_query) / n, 1),
    }

    print(json.dumps(metrics, indent=2))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"metrics": metrics, "per_query": per_query}, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
