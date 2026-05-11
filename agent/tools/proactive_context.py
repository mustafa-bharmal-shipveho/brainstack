"""Proactive context surface (Phase 3a) — the killer feature.

A SessionStart hook reads the user's first prompt, searches the digest
corpus (and themes/lessons if available), and injects a `<brain-context>`
block so the LLM has past-work context BEFORE answering. The user doesn't
have to type `recall <topic>` to remind the brain it has memories — the
brain volunteers them.

Search strategy (no qdrant dep — works in any python environment):
  - For each digest markdown under memory/semantic/digests/, extract
    title + tags + what_was_learned + what_user_did
  - Score = token overlap (Jaccard) of prompt tokens vs digest tokens
    PLUS a tag-match boost (each prompt token that matches a tag adds
    a fixed bonus)
  - Surface hits above `score_threshold`, sorted descending, capped at k

Why not recall/qdrant? Two reasons:
  1. The proactive hook runs in the SessionStart latency budget. A
     qdrant query opens a connection, loads the embedder, etc. — too
     slow for hot-path injection.
  2. The recall pipeline targets full prose; for tag-driven recall
     ("did I work on X?") the simpler lexical score is competitive
     and trivially fast.

A future enhancement can plug a qdrant-backed search behind the same
`search()` API.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent))

# Reuse profile_builder's tolerant front-matter parser
from profile_builder import _parse_simple_yaml_front  # type: ignore


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_]+")
_STOPWORDS = {
    "the", "and", "for", "you", "with", "this", "that", "have", "are",
    "was", "but", "not", "from", "your", "what", "how", "why", "when",
    "should", "would", "could", "did", "had", "has", "any", "all",
    "into", "out", "about", "into", "an", "a", "i", "is", "of", "on",
    "to", "it", "in", "be", "do", "im", "ive", "id",
}


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) > 1 and t not in _STOPWORDS
    }


# ---------------------------------------------------------------------------
# Hit dataclass
# ---------------------------------------------------------------------------

@dataclass
class ProactiveHit:
    title: str
    source: str  # "digest" | "theme" | "lesson"
    summary: str
    path: str
    session_id: str
    date: str
    score: float


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

def _load_digest_for_search(path: Path) -> dict | None:
    """Return a search-ready dict for one digest markdown: tokens,
    title, summary (what_was_learned), session_id, tags, date. None
    when the file can't be parsed."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    front, body = _parse_simple_yaml_front(raw)
    if front.get("archived", "").lower() in ("true", "yes", "1"):
        return None
    title_m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else path.stem
    learned_m = re.search(
        r"## What was learned\s*\n\s*(.+?)(?=\n##|\Z)",
        body, re.DOTALL,
    )
    learned = learned_m.group(1).strip() if learned_m else ""
    did_m = re.search(
        r"## What you did\s*\n\s*(.+?)(?=\n##|\Z)",
        body, re.DOTALL,
    )
    did = did_m.group(1).strip() if did_m else ""
    tags = front.get("domain_tags") or []
    if not isinstance(tags, list):
        tags = []
    date = str(front.get("started_at", ""))[:10]
    return {
        "path": str(path),
        "session_id": front.get("session_id", ""),
        "title": title,
        "summary": learned[:240],
        "did": did[:240],
        "tags": [t.lower() for t in tags if isinstance(t, str)],
        "date": date,
        "tokens": _tokenize(title + " " + learned + " " + did
                            + " " + " ".join(tags)),
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(prompt_tokens: set[str], doc: dict) -> float:
    """Jaccard token overlap + tag bonus.

    Jaccard: |A∩B| / |A∪B|.
    Tag bonus: each prompt token that appears in the doc's tag list
    adds 0.2 to the score (capped at +0.6 so tag-stuffed digests can't
    completely dominate)."""
    if not prompt_tokens:
        return 0.0
    doc_tokens = doc.get("tokens") or set()
    if not doc_tokens:
        return 0.0
    inter = prompt_tokens & doc_tokens
    union = prompt_tokens | doc_tokens
    base = len(inter) / len(union)
    tags = set(doc.get("tags") or [])
    tag_hits = prompt_tokens & tags
    boost = min(0.6, 0.2 * len(tag_hits))
    return base + boost


# ---------------------------------------------------------------------------
# Public search API
# ---------------------------------------------------------------------------

def search(prompt: str, *, brain_root: Path, k: int = 5,
           score_threshold: float = 0.05) -> list[ProactiveHit]:
    """Return the top-k digest hits for `prompt`. Empty list when the
    digest corpus is empty or all matches fall below `score_threshold`."""
    md_dir = Path(brain_root) / "memory" / "semantic" / "digests"
    if not md_dir.is_dir():
        return []
    prompt_tokens = _tokenize(prompt)
    if not prompt_tokens:
        return []
    hits: list[ProactiveHit] = []
    for path in md_dir.glob("*.md"):
        doc = _load_digest_for_search(path)
        if doc is None:
            continue
        s = _score(prompt_tokens, doc)
        if s < score_threshold:
            continue
        hits.append(ProactiveHit(
            title=doc["title"],
            source="digest",
            summary=doc["summary"],
            path=doc["path"],
            session_id=doc["session_id"],
            date=doc["date"],
            score=s,
        ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


# ---------------------------------------------------------------------------
# Context block formatting
# ---------------------------------------------------------------------------

def format_context_block(hits: list[ProactiveHit], *,
                          max_tokens: int = 1500) -> str:
    """Build the `<brain-context>` block to inject. Empty `hits` →
    empty string (do NOT emit an empty wrapper; LLM would treat it as
    a signal that "no relevant context found" and may infer poorly)."""
    if not hits:
        return ""
    lines = ["<brain-context>",
             "You've worked on related topics before:"]
    for i, h in enumerate(hits, 1):
        line = (f"{i}. [{h.source}] {h.title} ({h.date}, "
                f"score={h.score:.2f}) — {h.summary}")
        lines.append(line)
    lines.append("</brain-context>")
    block = "\n".join(lines)
    # Trim trailing hits until we fit max_tokens (4-char rule).
    while len(block) // 4 > max_tokens and len(hits) > 1:
        hits = hits[:-1]
        lines = ["<brain-context>",
                 "You've worked on related topics before:"]
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. [{h.source}] {h.title} ({h.date}, "
                         f"score={h.score:.2f}) — {h.summary}")
        lines.append("</brain-context>")
        block = "\n".join(lines)
    # If a single hit is still too long, truncate title and summary
    # together so the whole block fits the budget.
    if len(block) // 4 > max_tokens and len(hits) == 1:
        h = hits[0]
        # Reserve ~50 chars for wrapper + score + date metadata.
        budget_chars = max(40, max_tokens * 4 - 100)
        # Split the remainder evenly between title and summary.
        per_field = max(20, budget_chars // 2)
        trimmed_title = h.title[:per_field]
        trimmed_summary = h.summary[:per_field]
        block = (
            "<brain-context>\n"
            f"1. [{h.source}] {trimmed_title} ({h.date}) — "
            f"{trimmed_summary}\n"
            "</brain-context>"
        )
    return block


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def dispatch(prompt: str, *, brain_root: Path | None = None) -> str:
    """Single entry point for the SessionStart hook. Returns the block
    to inject (or empty string when disabled / no hits / no brain)."""
    if os.environ.get("BRAIN_PROACTIVE_DISABLED", "").strip() not in ("", "0",
                                                                     "false",
                                                                     "no",
                                                                     "off"):
        return ""
    if brain_root is None:
        brain_root = Path(os.environ.get("BRAIN_ROOT",
                                          str(Path.home() / ".agent")))
    hits = search(prompt, brain_root=Path(brain_root))
    return format_context_block(hits)


__all__ = ["search", "format_context_block", "dispatch", "ProactiveHit"]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(prog="proactive_context")
    p.add_argument("prompt", help="Prompt text to search the brain for")
    p.add_argument("--brain", default=None)
    p.add_argument("-k", type=int, default=5)
    args = p.parse_args()
    root = Path(args.brain).expanduser() if args.brain else None
    out = dispatch(args.prompt, brain_root=root)
    if out:
        print(out)
    else:
        print("(no relevant context found)")
