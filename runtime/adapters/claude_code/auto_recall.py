"""Auto-recall: query the brain on every substantive user prompt and
inject top-K results as additional context.

Pure logic kept here so the hook entrypoint stays a thin shell and so
this module is easy to unit-test with a fake retriever.

Architecture:
- `should_skip()` is the cheap first gate (no I/O). Filters short
  prompts, slash commands, bareword acks.
- `build_recall_block()` runs the query, formats the system-reminder
  block, and returns `(block, telemetry, injected)`. Caller (hook)
  handles timeout, printing, and the dedup-store write.
- `_load_retriever()` builds the production HybridRetriever lazily on
  first call. Fail-loud if dependencies are missing — caller catches.

The retriever-loader is module-level so tests can monkeypatch a fake
in cleanly without setting up qdrant/fastembed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
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

# Appended after an excerpt that got cut at _EXCERPT_CHAR_CAP, inside the
# fence, so the reading model can tell a truncated excerpt from a complete
# one instead of silently hitting a mid-sentence cutoff. No marker when the
# body fit whole.
_TRUNCATION_MARKER = " … [excerpt truncated]"

# Telemetry payload size constraint (events.py:122 enforces 1024 bytes per
# x_* value). We cap arrays at 3 entries and round floats so a single x_*
# field never serializes anywhere near that limit.
_TELEMETRY_SCORE_CAP = 3

# `x_paths` is the one telemetry field whose length scales with k, so it is
# the one that can breach events.py's 1024-byte per-key cap and cost us the
# WHOLE record. Target 1000, not 1024: the slack absorbs the difference
# between the compact separators events.py measures with and whatever a
# consumer re-encodes with.
_PATHS_JSON_MAX_BYTES = 1000


class _Retriever(Protocol):
    """Structural type for the retriever passed to build_recall_block.
    HybridRetriever from recall.core satisfies this. Tests can pass any
    object with a compatible `query` method."""

    def query(self, prompt: str, *, k: int = 5,
              type_filter: Any = None,
              source_filter: Any = None) -> list[Any]: ...


@dataclass
class RecallCandidate:
    """Normalized shape of one retrieval result, independent of whether it
    came from a `recall.core.QueryResult` (in-process path) or a daemon
    wire dict (`recall.daemon.result_to_wire`) via `DaemonResults`.

    Field order is part of the contract: `dedup.py` and the gate pipeline
    construct these positionally in a few places.
    """

    path: str
    source: str
    title: str
    score: float
    rerank_score: "float | None"
    body: str
    frontmatter: dict
    content_sha256: str


def normalize_results(raw: "list[Any]") -> "list[RecallCandidate]":
    """Convert raw retriever results (QueryResult objects OR daemon wire
    dicts) into `RecallCandidate`s. `content_sha256` is computed from the
    body when the source result doesn't already carry one.

    Hashing here rather than at the dedup store means both paths key on the
    same value: the daemon hashes the FULL body before capping it for the
    wire, and the in-process path hashes what it has. A doc the user edited
    therefore re-injects on either path.
    """
    out: list[RecallCandidate] = []
    for r in raw:
        body = _attr(r, "body", "") or ""
        sha = str(_attr(r, "content_sha256", "") or "")
        if not sha:
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        rerank = _attr(r, "rerank_score", None)
        out.append(RecallCandidate(
            path=str(_attr(r, "path", "<unknown>")),
            source=str(_attr(r, "source", "unknown")),
            title=str(_attr(r, "title", "") or _attr(r, "name", "") or ""),
            score=_as_float(_attr(r, "score", 0.0), 0.0),
            rerank_score=(None if rerank is None else _as_float(rerank, 0.0)),
            body=body,
            frontmatter=_attr(r, "frontmatter", None) or {},
            content_sha256=sha,
        ))
    return out


def _as_float(value: Any, default: float) -> float:
    """Coerce leniently. A malformed score from a wire dict must degrade to
    the default, not take the whole prompt down."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class DaemonResults:
    """Adapts one `recall.daemon_client.query()` wire response to the
    `_Retriever` protocol, so `build_recall_block` never learns whether
    results came from the daemon or the in-process retriever.

    Carries the daemon's OWN measurements (`query_ms`, `degraded`,
    `index_stale`) — the builder prefers these over a local stopwatch,
    which would otherwise include socket + JSON round-trip time.
    """

    def __init__(self, response: dict[str, Any]):
        self._response = response
        self.query_ms: "int | None" = response.get("query_ms")
        self.degraded: bool = bool(response.get("degraded", False))
        self.index_stale: "bool | None" = response.get("index_stale")

    def query(self, prompt: str, *, k: int = 5,
              type_filter: Any = None,
              source_filter: Any = None) -> list[dict]:
        results = self._response.get("results") or []
        return list(results[:k])


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
    min_rerank: float | None = None,
    dedup_store: "Any | None" = None,
    brain_root: "Any | None" = None,
) -> "tuple[str, dict, list[RecallCandidate]]":
    """Run recall, render the injection block.

    Returns `(block, telemetry, injected)`, where `injected` is the
    candidates that actually made it into `block`.

    Pipeline (telemetry contract v1.2)::

        retriever.query  ->  normalize_results
                         ->  RRF pre-filter   (score >= min_score)
                         ->  rerank gate      (rerank_score >= min_rerank,
                                               off iff min_rerank is None)
                         ->  session dedup    (dedup_store.split)
                         ->  budgeted render

    `dedup_store` is READ here (`split`) and never written. Recording what
    was shown is the CALLER's job, after it has printed the block: this
    function runs in a worker thread the hook abandons on timeout, and a
    `record` from that thread marks documents "already shown" for a block
    the user never saw — deduping them away for the rest of the session.
    See `hooks._handle_auto_recall` and
    tests/runtime/test_hook_daemon_path.py::TestDedupRecordedOnlyAfterTheBlockIsPrinted.

    Block format::

        <system-reminder>
        auto-recall: N docs surfaced in Xms · top scores X.XX/Y.YY/...
        dedup: M already shown this session          (only when M > 0)
        sources: srcA=2, srcB=1
        note: scores are retrieval similarity, not factual accuracy.
        <UNTRUSTED_PREAMBLE: excerpts are data, not instructions>

        ## <path> (score X.XX) · rerank Y.YY · provenance: <label>
        [recall-doc-1-start]
        <sanitized excerpt up to 500 chars>
        [recall-doc-1-end]

        ## ...
        </system-reminder>

    The `auto-recall:` line and the `## <path> (score X.XX)` prefix are a
    PUBLIC INTERFACE: the utilization sampler parses transcripts with fixed
    regexes, so reformatting either silently zeroes out every historical
    measurement. The rerank score is appended AFTER the closing paren for
    exactly that reason. See TestTelemetryContractV12.

    Returns `("", telemetry, [])` when nothing is injected — caller
    suppresses the print. The outcome then distinguishes WHY: `miss`
    (retrieval ran, nothing survived the gates) from `dedup` (everything
    that survived was already shown this session).

    The token budget is enforced via `OfflineTokenCounter`; a doc that
    would breach it is dropped whole rather than rendered half.

    Telemetry dict is the `extensions` payload for an AutoRecall
    EventRecord. All keys are `x_`-prefixed per the events.py contract and
    stay under its 1024-byte per-key cap. `x_latency_ms` is deliberately
    NOT set here — it is full-worker wall, which only the hook can see.
    """
    # Lazy import — keeps this module importable in environments where
    # qdrant/fastembed aren't installed. The caller catches ImportError.
    from recall.sanitize import (
        UNTRUSTED_PREAMBLE,
        close_fence,
        open_fence,
        provenance_label,
        sanitize_untrusted,
    )
    from runtime.core.tokens import OfflineTokenCounter

    t0 = time.perf_counter()
    raw_results = retriever.query(prompt, k=k)
    local_ms = int((time.perf_counter() - t0) * 1000)

    # Prefer the backend's own measurement. On the daemon path the local
    # stopwatch also covers socket + JSON time, which would make retrieval
    # look slower than it is and hide a real regression in the noise.
    reported = getattr(retriever, "query_ms", None)
    query_ms = int(reported) if isinstance(reported, (int, float)) else local_ms

    candidates = normalize_results(raw_results)
    k_candidates = len(candidates)

    # --- gate 1: the RRF pre-filter. Cheap and always available.
    if min_score > 0.0:
        survivors = [c for c in candidates if c.score >= min_score]
    else:
        survivors = list(candidates)

    # Score samples describe the survivors of the RRF pre-filter, BEFORE
    # the rerank gate and dedup. Sampling the injected set instead would
    # make every miss look like it had no candidates at all — exactly the
    # fires we most need to diagnose.
    top_scores = [round(c.score, 2) for c in survivors[:_TELEMETRY_SCORE_CAP]]
    rerank_scores = [
        round(c.rerank_score, 2)
        for c in survivors[:_TELEMETRY_SCORE_CAP]
        if c.rerank_score is not None
    ]

    # --- gate 2: the cross-encoder floor.
    #
    # `None` is the ONLY off switch. Cross-encoder outputs are raw logits,
    # not probabilities, so a calibrated floor is routinely NEGATIVE —
    # eval/RESULTS.md picks -1.9547. An `if min_rerank > 0.0` enable-check
    # therefore disabled the gate for precisely the values the calibration
    # exists to produce, and 0.0 is a real threshold (admit >= 0) rather
    # than a sentinel.
    #
    # A `None` rerank_score still passes: the in-process fallback never
    # loads a reranker, and gating everything out there would make
    # auto-recall go permanently silent whenever the daemon is down.
    # `x_path` / `x_rerank_scores == []` make that degradation visible
    # instead of silent.
    if min_rerank is None:
        passed = list(survivors)
    else:
        passed = [c for c in survivors
                  if c.rerank_score is None or c.rerank_score >= min_rerank]
    k_gated_out = k_candidates - len(passed)

    # --- gate 3: per-session dedup. Re-showing the same doc on every
    # prompt of a long session burns context for zero new information.
    if dedup_store is not None:
        fresh, duplicates = dedup_store.split(passed)
    else:
        fresh, duplicates = list(passed), []
    k_dedup = len(duplicates)

    counter = OfflineTokenCounter()

    # Render the doc sections first: the header has to report how many
    # docs SURVIVED the budget, and that is not known until they are laid
    # out. The header's own cost is charged up front from a provisional
    # copy sized with the full candidate count (an over-estimate by at
    # most a couple of tokens, always in the safe direction).
    provisional_header = _render_header(
        n_docs=len(fresh), n_dedup=k_dedup, query_ms=query_ms,
        top_scores=top_scores, source_counts=Counter(c.source for c in fresh),
        preamble=UNTRUSTED_PREAMBLE,
    )
    used_tokens = counter.count(provisional_header)

    # Doc bodies are UNTRUSTED: every excerpt is sanitized (wrapper-escape
    # neutralization, control-char strip, truncation AFTER neutralization)
    # and wrapped in fence lines so the consuming model can tell recalled
    # data from block structure. A forged fence inside a body is itself
    # neutralized by the sanitizer.
    sections: list[str] = []
    injected: list[RecallCandidate] = []
    for doc_n, c in enumerate(fresh, start=1):
        rerank_part = (
            "" if c.rerank_score is None else f" · rerank {c.rerank_score:.2f}"
        )
        # Truncation is decided against the fully neutralized, uncapped
        # length — the same quantity sanitize_untrusted's own max_len branch
        # compares against — so the marker appears iff the body actually got
        # cut, never for a body that just happens to end near the cap.
        was_truncated = len(sanitize_untrusted(c.body)) > _EXCERPT_CHAR_CAP
        excerpt = sanitize_untrusted(c.body, max_len=_EXCERPT_CHAR_CAP)
        if was_truncated:
            excerpt = f"{excerpt}{_TRUNCATION_MARKER}"
        section = (
            f"## {c.path} (score {c.score:.2f}){rerank_part}"
            f" · provenance: {provenance_label(c.frontmatter)}\n"
            f"{open_fence(doc_n)}\n{excerpt}\n{close_fence(doc_n)}\n"
        )
        section_tokens = counter.count(section)
        if used_tokens + section_tokens > budget_tokens:
            # Drop the remaining docs whole rather than render a
            # half-truncated section.
            break
        sections.append(section)
        injected.append(c)
        used_tokens += section_tokens

    if injected:
        outcome = "hit"
    elif duplicates and not fresh:
        # Everything that cleared the gates was already on screen. That is
        # a distinct, healthy state — not the miss it used to be logged as.
        outcome = "dedup"
    else:
        outcome = "miss"

    paths = [_relativize(c.path, brain_root) for c in injected]
    kept, paths_truncated = _cap_paths(paths)
    paths_hash = hashlib.sha256(
        "\n".join(sorted(paths)).encode("utf-8")
    ).hexdigest()[:16]

    source_counts: Counter[str] = Counter(c.source for c in injected)
    telemetry: dict[str, Any] = {
        "x_outcome": outcome,
        "x_query_ms": query_ms,
        "x_degraded": bool(getattr(retriever, "degraded", False))
        or _dense_fallback_active(),
        "x_k_requested": k,
        "x_k_candidates": k_candidates,
        "x_k_gated_out": k_gated_out,
        "x_k_dedup": k_dedup,
        "x_k_returned": len(injected),
        "x_top_scores": top_scores,
        "x_rerank_scores": rerank_scores,
        "x_sources": dict(source_counts),
        "x_paths": kept,
        "x_paths_truncated": paths_truncated,
        "x_paths_hash": paths_hash,
    }
    # Only the daemon tracks index freshness. On the in-process path
    # staleness is UNKNOWN, and reporting `false` would be a lie that makes
    # a stale-index incident invisible. Omit the key instead.
    index_stale = getattr(retriever, "index_stale", None)
    if index_stale is not None:
        telemetry["x_index_stale"] = bool(index_stale)

    if not injected:
        return "", telemetry, []

    header = _render_header(
        n_docs=len(injected), n_dedup=k_dedup, query_ms=query_ms,
        top_scores=top_scores, source_counts=source_counts,
        preamble=UNTRUSTED_PREAMBLE,
    )
    # Blank line between sections keeps the block readable at a glance.
    body = "".join(f"{s}\n" for s in sections)
    return f"{header}{body}</system-reminder>", telemetry, injected


def _render_header(*, n_docs: int, n_dedup: int, query_ms: int,
                   top_scores: list[float], source_counts: "Counter[str]",
                   preamble: str) -> str:
    """The block header, ending in a blank line so sections follow cleanly.

    BYTE-STABLE INTERFACE — see `build_recall_block`'s docstring.
    """
    score_str = "/".join(f"{s:.2f}" for s in top_scores) if top_scores else "n/a"
    sources_str = ", ".join(f"{s}={n}" for s, n in source_counts.most_common())
    doc_noun = "doc" if n_docs == 1 else "docs"
    lines = [
        "<system-reminder>",
        f"auto-recall: {n_docs} {doc_noun} surfaced in {query_ms}ms · top scores {score_str}",
    ]
    if n_dedup > 0:
        lines.append(f"dedup: {n_dedup} already shown this session")
    lines += [
        f"sources: {sources_str}",
        "note: scores are retrieval similarity, not factual accuracy.",
        preamble,
        "",
    ]
    return "\n".join(lines) + "\n"


def _relativize(path: str, brain_root: "Any | None") -> str:
    """Strip the brain-root prefix so `x_paths` is machine-portable.

    A path OUTSIDE the brain root (an `--add-source` directory, say) is
    returned unchanged rather than `../..`-walked: the stats planner
    re-absolutizes with `recall.config.brain_root()`, and a relative path
    that escapes the root would rejoin to the wrong file.
    """
    if not brain_root:
        return path
    try:
        p = Path(path)
        root = Path(brain_root)
        if p.is_relative_to(root):
            return os.path.relpath(str(p), str(root))
    except (ValueError, OSError):
        pass
    return path


def _cap_paths(paths: list[str]) -> tuple[list[str], bool]:
    """Drop paths from the END until the list fits `_PATHS_JSON_MAX_BYTES`.

    An over-cap value makes events.py reject the whole record at dump time,
    so a wide-k fire would cost us its ENTIRE telemetry rather than a few
    path strings. Truncating from the end keeps the highest-ranked docs.
    """
    kept = list(paths)
    truncated = False
    while kept and len(json.dumps(kept).encode("utf-8")) > _PATHS_JSON_MAX_BYTES:
        kept.pop()
        truncated = True
    return kept, truncated


def _dense_fallback_active() -> bool:
    """Whether retrieval is running BM25-only because the dense embedder
    was unavailable.

    Deliberately consults `recall.qdrant_backend` only when it is ALREADY
    imported. On the daemon path the hook never touches qdrant, and
    importing it just to read a flag would reintroduce the ~1.5 s
    cold-start this whole design exists to avoid.
    """
    mod = sys.modules.get("recall.qdrant_backend")
    if mod is None:
        return False
    try:
        return bool(mod.dense_fallback_active())
    except Exception:
        return False


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


def _auto_recall_collections(cfg: Any) -> list[str]:
    """Collection names that auto-recall is allowed to query.

    Every configured source feeds auto-recall EXCEPT those named in
    `cfg.auto_recall.exclude_sources`. Excluded sources stay available to
    explicit `recall query` and MCP; they just never get injected into a
    session on every prompt. This is the source-level scoping lever from the
    adoption audit: a sensitive mirrored `--add-source` folder can be kept
    searchable on demand without leaking into unrelated repositories.

    Pure and hermetic (no Qdrant), so it is unit-tested directly. An unknown
    name in the exclude list is a harmless no-op; excluding every source
    yields an empty list (auto-recall then surfaces nothing).
    """
    exclude = set(getattr(getattr(cfg, "auto_recall", None), "exclude_sources", []) or [])
    return [s.name for s in cfg.sources if s.name not in exclude]


def _load_retriever() -> _Retriever:
    """Build the production HybridRetriever from the user's recall config.

    Module-level entrypoint so tests can monkeypatch this in to inject a
    fake retriever without going through the full embedder/qdrant load.
    Raises ImportError or other exceptions if dependencies are missing —
    the caller is responsible for catching and falling open.

    Two hard rules make this path survivable inside a per-prompt
    subprocess with a ~1500 ms budget:

    1. NEVER refresh the index. `needs_refresh` stats every file in every
       source and `build_index` embeds; either one blows the budget, so the
       prompt gets nothing injected AND pays the full stall — on every
       prompt. Freshness is the daemon's job. This path is allowed to serve
       whatever the daemon or the last CLI query left behind, which is why
       `x_index_stale` is absent here rather than `false`.
    2. NEVER load a cross-encoder, whatever `cfg.ranking.reranker` says.
       The model load alone exceeds the budget. The daemon reranks; this
       fallback degrades to RRF-only, and `x_path` plus an empty
       `x_rerank_scores` say so in telemetry.
    """
    import recall.config as rcfg
    import recall.core as rcore

    cfg = rcfg.load_config()
    return rcore.HybridRetriever(
        documents=None,
        collections=_auto_recall_collections(cfg),
        embedder=cfg.ranking.embedder,
        sparse_embedder=cfg.ranking.sparse_embedder,
        reranker="none",
        reranker_model=cfg.ranking.reranker_model,
        rerank_n=cfg.ranking.rerank_n,
        needs_review_policy=cfg.ranking.needs_review_policy,
        needs_review_penalty=cfg.ranking.needs_review_penalty,
        # Same precedence as the CLI (minus the flag, which hooks lack):
        # RECALL_MODE env > config ranking.mode. Lets a brain whose dense
        # model never downloaded force sparse-only auto-recall.
        mode=rcfg.effective_mode(cfg),
    )
