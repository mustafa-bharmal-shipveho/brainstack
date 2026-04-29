"""Retriever core: BM25, embeddings (optional), Reciprocal Rank Fusion."""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from typing import Optional, Sequence

from rank_bm25 import BM25Plus


@dataclass(frozen=True)
class Document:
    path: str
    source: str
    title: str
    frontmatter: dict
    body: str
    text: str


@dataclass(frozen=True)
class QueryResult:
    document: Document
    score: float


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], k: int = 60
) -> list[str]:
    """Fuse multiple rankings using RRF.

    Each input is an ordered list of doc ids (or keys). Output is a single
    fused ordering containing every unique key, sorted by RRF score descending.
    """
    if not rankings:
        return []
    if all(len(r) == 0 for r in rankings):
        return []

    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)

    # Sort: higher score first; stable order ensures determinism
    return sorted(scores.keys(), key=lambda x: (-scores[x], x))


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------


class Bm25Retriever:
    def __init__(self, documents: Sequence[Document]):
        self.documents = list(documents)
        if self.documents:
            tokenized_corpus = [tokenize(d.text) for d in self.documents]
            # rank_bm25 doesn't like empty docs; substitute an unlikely token
            tokenized_corpus = [tokens or ["__empty__"] for tokens in tokenized_corpus]
            # BM25Plus is preferred over BM25Okapi: it avoids the negative-IDF
            # quirk that occurs when a term appears in all (or nearly all) docs,
            # which would otherwise make additional matches *decrease* the score.
            self._bm25 = BM25Plus(tokenized_corpus)
        else:
            self._bm25 = None

    def query(self, query: str, k: int) -> list[QueryResult]:
        if k <= 0 or not self.documents or self._bm25 is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            range(len(self.documents)),
            key=lambda i: (-float(scores[i]), i),
        )
        return [
            QueryResult(document=self.documents[i], score=float(scores[i]))
            for i in ranked[:k]
        ]


# ---------------------------------------------------------------------------
# Embeddings (optional)
# ---------------------------------------------------------------------------


class EmbeddingRetriever:
    """Cosine-similarity retriever over sentence-transformers embeddings.

    Skipped silently if sentence-transformers is not installed (the hybrid
    retriever falls back to BM25 only).
    """

    _model_lock = threading.Lock()
    _model_cache: dict[str, object] = {}

    def __init__(
        self, documents: Sequence[Document], model_name: str = "all-MiniLM-L6-v2"
    ):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            import numpy as np
        except ImportError as e:
            raise RuntimeError("sentence-transformers not installed") from e

        self.documents = list(documents)
        self.model_name = model_name
        with self._model_lock:
            if model_name not in self._model_cache:
                self._model_cache[model_name] = SentenceTransformer(model_name)
            self._model = self._model_cache[model_name]

        if self.documents:
            self._embeddings = self._model.encode(
                [d.text for d in self.documents],
                convert_to_numpy=True,
                show_progress_bar=False,
                normalize_embeddings=True,
            )
        else:
            self._embeddings = None

    def query(self, query: str, k: int) -> list[QueryResult]:
        if not self.documents or self._embeddings is None:
            return []
        import numpy as np

        q_vec = self._model.encode(
            [query],
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=True,
        )[0]
        # Embeddings are normalized → dot product = cosine similarity
        scores = self._embeddings @ q_vec
        ranked = sorted(
            range(len(self.documents)),
            key=lambda i: (-float(scores[i]), i),
        )
        return [
            QueryResult(document=self.documents[i], score=float(scores[i]))
            for i in ranked[:k]
        ]


# ---------------------------------------------------------------------------
# Hybrid retriever
# ---------------------------------------------------------------------------


def _embeddings_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("sentence_transformers") is not None
    except Exception:
        return False


class HybridRetriever:
    """Combines BM25 and (optionally) embeddings via Reciprocal Rank Fusion."""

    def __init__(
        self,
        documents: Sequence[Document],
        bm25_weight: float = 1.0,
        embedding_weight: float = 1.0,
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        self.documents = list(documents)
        self.bm25_weight = bm25_weight
        self.embedding_weight = embedding_weight

        self._bm25 = Bm25Retriever(self.documents) if bm25_weight > 0 else None

        self._emb: Optional[EmbeddingRetriever] = None
        if embedding_weight > 0 and _embeddings_available():
            try:
                self._emb = EmbeddingRetriever(self.documents, model_name=embedding_model)
            except Exception:
                # If model loading fails (network, disk), fall back gracefully
                self._emb = None

    def query(
        self,
        query: str,
        k: int,
        type_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> list[QueryResult]:
        if k <= 0 or not self.documents:
            return []

        # Apply pre-filters first so we rank only the relevant subset.
        eligible = [
            d
            for d in self.documents
            if (type_filter is None or d.frontmatter.get("type") == type_filter)
            and (source_filter is None or d.source == source_filter)
        ]
        if not eligible:
            return []

        # Build sub-retrievers if filtering changed the corpus
        if len(eligible) == len(self.documents):
            bm25 = self._bm25
            emb = self._emb
        else:
            bm25 = Bm25Retriever(eligible) if self.bm25_weight > 0 else None
            emb = None
            if self.embedding_weight > 0 and self._emb is not None:
                # Reuse the model but build a fresh embedding matrix
                try:
                    emb = EmbeddingRetriever(eligible, model_name=self._emb.model_name)
                except Exception:
                    emb = None

        # Pull top-N from each (oversample to give RRF good material)
        oversample = max(k * 4, 20)
        rankings: list[list[str]] = []
        score_lookup: dict[str, float] = {}

        if bm25 is not None:
            bm_results = bm25.query(query, k=oversample)
            rankings.append([r.document.path for r in bm_results])
            for r in bm_results:
                score_lookup.setdefault(r.document.path, r.score)

        if emb is not None:
            em_results = emb.query(query, k=oversample)
            rankings.append([r.document.path for r in em_results])
            for r in em_results:
                # Embedding scores are 0..1; keep the larger of the two if both present
                score_lookup[r.document.path] = max(
                    score_lookup.get(r.document.path, -math.inf), r.score
                )

        if not rankings:
            return []

        fused_ids = reciprocal_rank_fusion(rankings)
        path_to_doc = {d.path: d for d in eligible}
        results: list[QueryResult] = []
        for doc_path in fused_ids:
            doc = path_to_doc.get(doc_path)
            if doc is None:
                continue
            results.append(QueryResult(document=doc, score=score_lookup.get(doc_path, 0.0)))
            if len(results) >= k:
                break
        return results
