"""Auto-recall: query the brain on every substantive user prompt and
inject top-K results as additional context.

Pure logic kept here so the hook entrypoint stays a thin shell and so
this module is easy to unit-test with a fake retriever.

Architecture:
- `should_skip()` is the cheap first gate (no I/O). Filters short
  prompts, slash commands, bareword acks.
- `build_recall_block()` runs the query, formats the system-reminder
  block, returns telemetry. Caller (hook) handles timeout + I/O.
- `_load_retriever()` builds the production HybridRetriever lazily on
  first call. Fail-loud if dependencies are missing — caller catches.

The retriever-loader is module-level so tests can monkeypatch a fake
in cleanly without setting up qdrant/fastembed.
"""
from __future__ import annotations

import hashlib
import time
from collections import Counter
from typing import Any, Protocol


# Bareword acks that never deserve recall. Lowercased + punctuation-stripped
# before lookup. These represent "user is acknowledging, not asking" — no
# benefit to surfacing memories for them.
_ACKS = frozenset({
    "yes", "y", "yep", "yeah", "yup",
    "no", "n", "nope", "nah",
    "ok", "okay", "k", "kk",
    "go", "do it", "done",
    "stop", "wait", "pause",
    "thanks", "ty", "thx", "thank you",
})

# Excerpt cap per-doc in chars (rough proxy for ~125 tokens). The token-
# budget enforcement below is the authoritative bound; this is just to
# keep individual docs from dominating the block.
_EXCERPT_CHAR_CAP = 500

# Telemetry payload size constraint (events.py:122 enforces 1024 bytes per
# x_* value). We cap arrays at 3 entries and round floats so a single x_*
# field never serializes anywhere near that limit.
_TELEMETRY_SCORE_CAP = 3


class _Retriever(Protocol):
    """Structural type for the retriever passed to build_recall_block.
    HybridRetriever from recall.core satisfies this. Tests can pass any
    object with a compatible `query` method."""

    def query(self, prompt: str, *, k: int = 5,
              type_filter: Any = None,
              source_filter: Any = None) -> list[Any]: ...


def should_skip(prompt: str, *, min_chars: int) -> tuple[bool, str | None]:
    """Decide whether to skip auto-recall for this prompt.

    Returns (skip?, reason). Reason is a short tag for telemetry so
    `recall stats` can report skip-cause distribution. Skipping happens
    BEFORE retriever load — saves cold-start cost on the common cases.
    """
    stripped = prompt.strip()
    if len(stripped) < min_chars:
        return True, "too_short"
    if stripped.startswith("/"):
        return True, "slash"
    if stripped.lower().rstrip(" !.?,").rstrip() in _ACKS:
        return True, "ack"
    return False, None


def build_recall_block(
    prompt: str,
    retriever: _Retriever,
    *,
    k: int,
    budget_tokens: int,
    min_score: float = 0.0,
) -> tuple[str, dict]:
    """Run recall, render the injection block, return (block, telemetry).

    Block format::

        <system-reminder>
        auto-recall: N docs surfaced in Xms · top scores X.XX/Y.YY/...
        sources: srcA=2, srcB=1
        note: scores are retrieval similarity, not factual accuracy.

        ## <path> (score X.XX)
        <excerpt up to 500 chars>

        ## ...
        </system-reminder>

    Returns `("", telemetry)` when no results — caller suppresses the print.

    The token budget is enforced via `OfflineTokenCounter`. Excerpts are
    truncated mid-doc when adding the next doc would exceed `budget_tokens`.

    Telemetry dict is the `extensions` payload for an AutoRecall EventRecord.
    All keys prefixed `x_` per events.py contract; values stay well under
    the 1024-byte per-key cap.
    """
    # Lazy import — keeps this module importable in environments where
    # qdrant/fastembed aren't installed. The caller catches ImportError.
    from runtime.core.tokens import OfflineTokenCounter

    t0 = time.perf_counter()
    raw_results = retriever.query(prompt, k=k)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    # Score floor: drop low-relevance hits before they pollute context.
    # Default 0.0 = no filtering (preserves backward compat). When the
    # user raises this, the metadata header still reports the post-filter
    # count, so `recall stats` reflects what was actually injected.
    if min_score > 0.0:
        results = [r for r in raw_results
                   if float(_attr(r, "score", 0.0)) >= min_score]
    else:
        results = list(raw_results)

    # Per-source counts go in both the block header (human-readable) and
    # telemetry (machine-readable for `recall stats`)
    source_counts: Counter[str] = Counter()
    for r in results:
        source_counts[_attr(r, "source", "unknown")] += 1

    # Top scores go in the header + telemetry. Capped at 3 to keep payload
    # tiny; rounded to 2dp so the model can't latch onto spurious precision.
    top_scores = [
        round(float(_attr(r, "score", 0.0)), 2)
        for r in results[:_TELEMETRY_SCORE_CAP]
    ]

    # Stable hash of the surfaced paths — lets us correlate fires across
    # the events log without leaking absolute paths into telemetry.
    paths_for_hash = "\n".join(sorted(_attr(r, "path", "") for r in results))
    paths_hash = hashlib.sha256(paths_for_hash.encode("utf-8")).hexdigest()[:16]

    telemetry: dict[str, Any] = {
        "x_outcome": "hit",
        "x_latency_ms": latency_ms,
        "x_k_requested": k,
        "x_k_returned": len(results),
        "x_top_scores": top_scores,
        "x_sources": dict(source_counts),
        "x_paths_hash": paths_hash,
    }

    if not results:
        return "", telemetry

    counter = OfflineTokenCounter()
    parts: list[str] = []

    # Header
    score_str = "/".join(f"{s:.2f}" for s in top_scores) if top_scores else "n/a"
    sources_str = ", ".join(f"{s}={n}" for s, n in source_counts.most_common())
    header_lines = [
        "<system-reminder>",
        f"auto-recall: {len(results)} docs surfaced in {latency_ms}ms · top scores {score_str}",
        f"sources: {sources_str}",
        "note: scores are retrieval similarity, not factual accuracy.",
        "",
    ]
    parts.extend(header_lines)
    used_tokens = counter.count("\n".join(header_lines))

    # Per-doc sections, budget-bounded
    for r in results:
        path = _attr(r, "path", "<unknown>")
        score = float(_attr(r, "score", 0.0))
        body = _attr(r, "body", "") or ""
        excerpt = body[:_EXCERPT_CHAR_CAP]
        section = f"## {path} (score {score:.2f})\n{excerpt}\n"
        section_tokens = counter.count(section)
        if used_tokens + section_tokens > budget_tokens:
            # Skip remaining docs entirely rather than rendering a
            # half-truncated section. Telemetry already records full
            # k_returned so the stats reflect what was retrieved, not
            # what made it into the budget.
            break
        parts.append(section)
        used_tokens += section_tokens

    parts.append("</system-reminder>")
    return "\n".join(parts), telemetry


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Read an attribute from a result object.

    Production: `recall.core.QueryResult` wraps a `Document` — so `.path`,
    `.source`, `.name`, `.body` actually live on `obj.document.X` while
    `.score` lives on `obj.X` directly. We try the wrapped document first
    (most common path), fall through to direct attr (works for the test
    fake which is flat), then dict-key access (legacy/raw)."""
    doc = getattr(obj, "document", None)
    if doc is not None and hasattr(doc, name):
        return getattr(doc, name, default)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _load_retriever() -> _Retriever:
    """Build the production HybridRetriever from the user's recall config.

    Module-level entrypoint so tests can monkeypatch this in to inject a
    fake retriever without going through the full embedder/qdrant load.
    Raises ImportError or other exceptions if dependencies are missing —
    the caller is responsible for catching and falling open.

    Uses the cold-start construction pattern: pass collection names rather
    than re-walking documents. The brain is already indexed; we just query.
    """
    from recall.config import load_config
    from recall.core import HybridRetriever

    cfg = load_config()
    return HybridRetriever(
        documents=None,
        collections=[s.name for s in cfg.sources],
        embedder=cfg.ranking.embedder,
        sparse_embedder=cfg.ranking.sparse_embedder,
        reranker=cfg.ranking.reranker,
        reranker_model=cfg.ranking.reranker_model,
        rerank_n=cfg.ranking.rerank_n,
    )
