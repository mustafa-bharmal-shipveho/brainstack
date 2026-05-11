"""Theme detection across session digests (Phase 2b).

Clusters digests sharing a `domain_tags` token. When ≥3 digests cluster
under the same tag, emit a "theme candidate" via the existing
`candidates/` review pipeline. The user reviews per-candidate via the
same `recall pending --review` UI; graduating it produces a normal
lesson — closing the loop:

  raw episodes → digests → themes → candidates → graduated lessons

The cluster machinery here is deliberately small: tag overlap is a
robust signal because the digest LLM is already extracting tags from
session content. We don't need token-similarity on titles for v1.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_themes(digests: list[dict], *, min_size: int = 3) -> list[dict]:
    """Group digests by shared `domain_tags`. Each unique tag that
    appears across ≥ `min_size` digests becomes a theme.

    A digest with multiple tags can contribute to multiple themes.
    Empty tag lists contribute nothing — clustering keys on tags only.
    Tags are case-folded for grouping; the original case from the
    most-recent digest is preserved in the output `tag` field for
    display."""
    by_tag: dict[str, list[dict]] = defaultdict(list)
    case_for_tag: dict[str, str] = {}
    for d in digests:
        tags = d.get("domain_tags") or []
        if not isinstance(tags, list):
            continue
        # Per-digest dedup: a session listing the same tag in multiple
        # casings ("auth-rewrite", "Auth-Rewrite", " auth-rewrite ")
        # must count ONCE for that tag, not three times. Without this,
        # a single noisy digest can fabricate a 3-member theme on its
        # own and survive the min_size=3 filter.
        seen_keys: set[str] = set()
        for raw in tags:
            if not isinstance(raw, str) or not raw.strip():
                continue
            key = raw.strip().lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            by_tag[key].append(d)
            case_for_tag.setdefault(key, raw.strip())

    themes: list[dict] = []
    for key, members in by_tag.items():
        if len(members) < min_size:
            continue
        outcomes = Counter(
            (m.get("outcome") or "unknown") for m in members
        )
        themes.append({
            "tag": case_for_tag.get(key, key),
            "session_ids": [m["session_id"] for m in members],
            "outcomes": dict(outcomes),
            "titles": [m.get("title", "") for m in members],
            "learnings": [m.get("what_was_learned", "") for m in members],
        })
    # Stable order: highest-member-count theme first, then alpha by tag
    themes.sort(key=lambda t: (-len(t["session_ids"]), t["tag"].lower()))
    return themes


# ---------------------------------------------------------------------------
# Theme → candidate
# ---------------------------------------------------------------------------

def _theme_id(theme: dict) -> str:
    """Stable content-derived id for the theme candidate. Same theme
    (tag + sorted session_ids) → same id, so re-staging is a no-op."""
    sids = sorted(theme["session_ids"])
    payload = theme["tag"].lower() + "||" + "|".join(sids)
    return "theme_" + hashlib.md5(payload.encode()).hexdigest()[:12]


def _theme_claim(theme: dict) -> str:
    tag = theme["tag"]
    n = len(theme["session_ids"])
    outcomes = theme.get("outcomes") or {}
    if outcomes:
        outs = ", ".join(f"{c} {name}"
                         for name, c in sorted(outcomes.items(),
                                                key=lambda x: -x[1]))
        return (
            f"Recurring topic across {n} sessions: {tag} ({outs}). "
            "Review and graduate the durable insight if you keep "
            "running into this."
        )
    return f"Recurring topic across {n} sessions: {tag}."


def _is_already_decided(theme_id: str, candidates_dir: Path) -> bool:
    """True when this theme's candidate is already in graduated/ or
    rejected/ — don't re-stage decisions the user has made."""
    for sub in ("graduated", "rejected"):
        if (candidates_dir / sub / f"{theme_id}.json").is_file():
            return True
    return False


def stage_theme_candidates(themes: list[dict],
                            candidates_dir: Path) -> int:
    """Write one candidate JSON per theme into `candidates_dir`.
    Returns count newly staged (excludes idempotent skips + already-
    decided themes)."""
    candidates_dir = Path(candidates_dir)
    candidates_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for theme in themes:
        tid = _theme_id(theme)
        path = candidates_dir / f"{tid}.json"
        if path.exists():
            continue
        if _is_already_decided(tid, candidates_dir):
            continue
        candidate = {
            "id": tid,
            "key": tid,
            "name": tid,
            "claim": _theme_claim(theme),
            "conditions": [theme["tag"].lower()],
            "evidence_ids": list(theme["session_ids"]),
            "cluster_size": len(theme["session_ids"]),
            "canonical_salience": float(min(10, max(5,
                                                     len(theme["session_ids"])))),
            "origin": "theme.digest",
            "staged_at": now,
            "status": "staged",
            "decisions": [
                {"ts": now, "action": "staged", "reviewer": "theme_cluster"},
            ],
            "rejection_count": 0,
            "source": {
                "adapter": "theme_cluster",
                "tag": theme["tag"],
                "outcomes": theme.get("outcomes") or {},
            },
        }
        path.write_text(json.dumps(candidate, indent=2))
        n += 1
    return n


__all__ = ["cluster_themes", "stage_theme_candidates"]


# CLI for the install.sh / sync.sh pipeline
if __name__ == "__main__":
    import argparse
    import sys
    # Reuse profile_builder's digest reader since it already has a
    # tolerant front-matter parser.
    _THIS = Path(__file__).resolve()
    sys.path.insert(0, str(_THIS.parent))
    from profile_builder import _iter_digests  # type: ignore

    p = argparse.ArgumentParser(prog="theme_cluster")
    p.add_argument("--brain", default=None)
    p.add_argument("--min-size", type=int, default=3)
    args = p.parse_args()
    root = Path(args.brain).expanduser() if args.brain \
           else Path(os.environ.get("BRAIN_ROOT",
                                     str(Path.home() / ".agent")))
    md_dir = root / "memory" / "semantic" / "digests"
    digests = []
    for path, front, body in _iter_digests(md_dir):
        import re as _re
        title_m = _re.search(r"^#\s+(.+)$", body, _re.MULTILINE)
        learned_m = _re.search(
            r"## What was learned\s*\n\s*(.+?)(?=\n##|\Z)",
            body, _re.DOTALL,
        )
        digests.append({
            "session_id": front.get("session_id", path.stem),
            "domain_tags": front.get("domain_tags") or [],
            "title": title_m.group(1).strip() if title_m else "",
            "what_was_learned": learned_m.group(1).strip()
                                if learned_m else "",
            "started_at": front.get("started_at", ""),
            "outcome": front.get("outcome", "unknown"),
        })
    themes = cluster_themes(digests, min_size=args.min_size)
    candidates_dir = root / "memory" / "candidates"
    n = stage_theme_candidates(themes, candidates_dir)
    print(f"clustered {len(digests)} digests into {len(themes)} theme(s); "
          f"staged {n} new theme candidate(s)")
