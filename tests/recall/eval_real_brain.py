"""Real-brain retrieval eval harness.

Loads a golden set of (query, expected_doc_abs) pairs, runs each through the
configured HybridRetriever, and reports industry-standard metrics:

  - Recall@1, Recall@5, Recall@10  (binary: expected doc in top-K)
  - MRR@10                          (mean reciprocal rank of expected doc)
  - NDCG@10                         (single-relevant; degenerates to 1/log2(rank+1))
  - p50_ms, p95_ms                  (per-query latency, wall-clock)

Run:

    BRAIN_ROOT=$HOME/.agent python tests/recall/eval_real_brain.py \\
        --golden tests/recall/golden/real_brain_v1.jsonl \\
        --out tests/recall/eval_runs/baseline.json \\
        [--rerank cross_encoder | none]

Output: JSON with aggregate scores + per-query results so failures can be
inspected. Exits 0 always (this is a measurement tool, not a CI gate).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Make repo root importable when run as a script.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from recall.config import load_config
from recall.core import HybridRetriever
from recall.index import build_index, load_index, needs_refresh


def _ndcg_single_relevant(rank: Optional[int]) -> float:
    """NDCG@K when exactly one doc is relevant. Rank is 1-indexed; None = not found."""
    if rank is None:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def _run_one(retriever: HybridRetriever, query: str, k: int, expected_path: str):
    """Returns (rank or None, latency_ms, top_paths)."""
    t0 = time.perf_counter()
    results = retriever.query(query, k=k)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    top_paths = [r.document.path for r in results]
    rank = None
    for i, p in enumerate(top_paths, start=1):
        if Path(p).resolve() == Path(expected_path).resolve():
            rank = i
            break
    return rank, latency_ms, top_paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True, help="JSONL golden set")
    ap.add_argument("--out", required=True, help="Output JSON path")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument(
        "--rerank",
        default=None,
        help="Override reranker: cross_encoder | none. Default = config value.",
    )
    ap.add_argument(
        "--label",
        default="baseline",
        help="Human-readable label stored in the output JSON.",
    )
    ap.add_argument(
        "--prefetch",
        type=int,
        default=None,
        help="Override _HYBRID_PREFETCH (per-leg over-pull before RRF fusion).",
    )
    ap.add_argument(
        "--rerank-n",
        type=int,
        default=None,
        help="Override rerank_n (candidates fed to the reranker).",
    )
    ap.add_argument(
        "--embedder",
        default=None,
        help=(
            "Override dense embedder. Forces a fresh Qdrant collection in a "
            "separate cache dir so vector dims match. Caller must also pass "
            "--dense-dim if the new model isn't 768-dim."
        ),
    )
    ap.add_argument(
        "--dense-dim",
        type=int,
        default=None,
        help="Vector dim for the dense embedder (required if --embedder isn't 768-dim).",
    )
    args = ap.parse_args()

    # Apply oversampling knobs by monkey-patching module constants.
    if args.prefetch is not None:
        from recall import qdrant_backend
        qdrant_backend._HYBRID_PREFETCH = args.prefetch
        print(f"override _HYBRID_PREFETCH = {args.prefetch}")
    if args.rerank_n is not None:
        from recall import qdrant_backend
        qdrant_backend._RERANK_OVERSAMPLE = args.rerank_n
        print(f"override _RERANK_OVERSAMPLE = {args.rerank_n}")

    # Embedder swap → fresh cache dir + patch the function defaults that
    # control indexing (Python binds defaults at function-definition time
    # so patching the module constant alone wouldn't propagate).
    _embedder_swap = None
    if args.embedder is not None:
        import os
        import tempfile
        from recall import qdrant_backend

        if args.dense_dim is None:
            raise SystemExit("--embedder requires --dense-dim")

        tmp_cache = Path(tempfile.mkdtemp(prefix="recall-eval-cache-"))
        os.environ["XDG_CACHE_HOME"] = str(tmp_cache)
        from importlib import reload
        from recall import config as _recall_config
        reload(_recall_config)
        qdrant_backend._reset_client_cache_for_tests()
        qdrant_backend._reset_model_cache_for_tests()

        # Patch module constants for any code that reads them at call time.
        qdrant_backend._DENSE_DEFAULT = args.embedder
        qdrant_backend._DENSE_DIM = args.dense_dim

        # Rebind function defaults (Python evaluated these at def-time using
        # the original module constants, so we replace them here).
        u = qdrant_backend.upsert_documents
        # signature: (client, collection, docs, dense_model=, sparse_model=, batch_size=)
        u.__defaults__ = (args.embedder, qdrant_backend._SPARSE_DEFAULT, 64)
        e = qdrant_backend.ensure_collection
        # signature: (client, collection, dense_size=)
        e.__defaults__ = (args.dense_dim,)

        _embedder_swap = args.embedder
        print(f"override _DENSE_DEFAULT = {args.embedder}  (dim={args.dense_dim})")
        print(f"using fresh cache at {tmp_cache} (will reindex)")

    golden = [json.loads(l) for l in Path(args.golden).read_text().splitlines() if l.strip()]
    print(f"loaded {len(golden)} golden pairs")

    cfg = load_config()
    cache = build_index(cfg.sources) if needs_refresh(cfg.sources) else load_index(cfg.sources)
    fresh = cache is None or not cache.documents or needs_refresh(cfg.sources)
    print(f"index: {len(cache.documents) if cache else 0} docs (fresh={fresh})")

    effective_reranker = args.rerank if args.rerank is not None else cfg.ranking.reranker
    effective_rerank_n = args.rerank_n if args.rerank_n is not None else cfg.ranking.rerank_n
    effective_embedder = _embedder_swap if _embedder_swap is not None else cfg.ranking.embedder
    retriever = HybridRetriever(
        documents=cache.documents if fresh else None,
        collections=[s.name for s in cfg.sources],
        embedder=effective_embedder,
        sparse_embedder=cfg.ranking.sparse_embedder,
        reranker=effective_reranker,
        reranker_model=cfg.ranking.reranker_model,
        rerank_n=effective_rerank_n,
    )
    print(
        f"retriever ready (reranker={effective_reranker}, model={cfg.ranking.reranker_model}, "
        f"rerank_n={effective_rerank_n})"
    )

    per_query = []
    latencies = []
    for pair in golden:
        rank, lat_ms, top_paths = _run_one(retriever, pair["query"], args.k, pair["expected_doc_abs"])
        latencies.append(lat_ms)
        per_query.append(
            {
                "query": pair["query"],
                "query_type": pair["query_type"],
                "expected_doc": pair["expected_doc"],
                "rank": rank,
                "latency_ms": round(lat_ms, 2),
                "top1_path": top_paths[0] if top_paths else None,
            }
        )

    # Aggregate
    n = len(per_query)
    hit_at = lambda k: sum(1 for r in per_query if r["rank"] is not None and r["rank"] <= k)
    metrics = {
        "n": n,
        "label": args.label,
        "reranker": effective_reranker,
        "reranker_model": cfg.ranking.reranker_model,
        "embedder": cfg.ranking.embedder,
        "k": args.k,
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
    }

    # Break down by query_type
    by_type: dict[str, list[dict]] = {}
    for r in per_query:
        by_type.setdefault(r["query_type"], []).append(r)
    metrics["by_query_type"] = {
        t: {
            "n": len(rs),
            "recall_at_5": round(sum(1 for r in rs if r["rank"] and r["rank"] <= 5) / len(rs), 4),
            "recall_at_10": round(sum(1 for r in rs if r["rank"] and r["rank"] <= 10) / len(rs), 4),
        }
        for t, rs in sorted(by_type.items())
    }

    print(json.dumps(metrics, indent=2))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"metrics": metrics, "per_query": per_query}, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
