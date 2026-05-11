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
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Tunables for the v2 LLM-synthesized rule claim path.
#
# v1 (the original meta-prompt claim) is non-graduatable by the user's
# standard (a graduated lesson must change what future LLM sessions DO).
# v2 calls an LLM to extract a shared imperative rule from clustered
# learnings; these constants bound the prompt size and the per-run
# failure budget. Codex review 2026-05-11 required the caps and breaker.
# ---------------------------------------------------------------------------
_MAX_LEARNINGS_PER_THEME = 15
_MAX_LEARNING_CHARS = 600
_MAX_CONSECUTIVE_LLM_ERRORS = 3


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


def _theme_id_v2(theme: dict) -> str:
    """v2 id for LLM-synthesized rule candidates. Distinct from v1 so the
    same cluster can stage afresh with a real behavioral rule even when
    its v1 (meta-prompt) candidate is already pending or rejected on
    disk. Format: theme_v2_<md5[:12]> over `v2||{tag.lower()}||{sorted_sids}`.

    Codex 2026-05-11 review pinned the exact prefix + payload so any
    drift in id derivation breaks `test_v2_id_format_matches_contract`."""
    sids = sorted(theme["session_ids"])
    payload = "v2||" + theme["tag"].lower() + "||" + "|".join(sids)
    return "theme_v2_" + hashlib.md5(payload.encode()).hexdigest()[:12]


_RULE_SYSTEM = (
    "You synthesize a single behavioral rule from a cluster of past "
    "LLM-session learnings. The rule will be appended to a lessons "
    "file that future LLM sessions read at context-load time, so it "
    "must change what they DO when the condition matches.\n"
    "\n"
    "Rules MUST:\n"
    "- be an imperative starting with: Always / Never / When X / Don't / Use / Prefer / Avoid / Stop / Only / Must\n"
    "- be a single sentence, 30 to 300 characters\n"
    "- be generic: no PR numbers, no session IDs, no absolute paths, no one-off names\n"
    "- not reference the review process itself (no 'review', 'candidate', 'theme', 'lesson', 'graduate', 'session', 'brainstack')\n"
    "- not merely restate the tag (e.g., 'Always code-review.' for tag 'code-review' is rejected)\n"
    "\n"
    "If the learnings do NOT share a clear transferable rule, output the literal string NONE.\n"
)

_RULE_SCHEMA = {
    "type": "object",
    "required": ["rule"],
    "properties": {
        "rule": {"type": "string", "minLength": 4, "maxLength": 300}
    }
}

# Imperative markers. Some require a trailing space to avoid matching
# longer words (e.g., "when " vs "whenever", "must " vs "mustard",
# "stop " vs "stopwatch", "only " vs "onlyfans", "use " vs "useless").
_IMPERATIVE_MARKERS = (
    "always", "never", "when ", "don't", "do not", "use ",
    "prefer", "avoid ", "stop ", "only ", "must ",
)

_SELF_REFERENCE_WORDS = (
    # "review" is intentionally NOT in the substring blocklist: it
    # legitimately appears in tool names ("codex-review", "code-review")
    # and engineering rules ("Always run codex-review after Claude review
    # for parallel second opinion"). The other words are unambiguous
    # markers of brainstack-internal meta-noise.
    "candidate", "theme", "lesson", "graduate",
    "session", "brainstack",
)

_PR_NUMBER_RE = re.compile(r"#\d+")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _synthesize_rule_claim(theme: dict, *, provider) -> Optional[str]:
    """Ask the LLM for a transferable imperative rule synthesizing the
    theme's clustered learnings, then run a deterministic validation
    pipeline. Returns the rule on success, None on any failure mode
    (LLM says NONE, parsed_json missing, validation rejects).

    Provider errors propagate to the caller; `stage_theme_candidates`
    catches LLMError to drive the consecutive-error circuit breaker."""
    raw_learnings = theme.get("learnings") or []
    capped = raw_learnings[:_MAX_LEARNINGS_PER_THEME]
    # Truncate each learning to the per-learning char cap so a single
    # 50KB digest can't blow the token budget. No ellipsis: a hard chop
    # keeps prompt-length math predictable.
    trimmed: list[str] = []
    for item in capped:
        s = str(item) if item is not None else ""
        if len(s) > _MAX_LEARNING_CHARS:
            s = s[:_MAX_LEARNING_CHARS]
        trimmed.append(s)

    if trimmed:
        bullets = "\n".join(f"{i+1}. {t}" for i, t in enumerate(trimmed))
    else:
        bullets = "1. (no learnings recorded)"

    tag = theme.get("tag", "")
    n = len(theme.get("session_ids") or [])
    outcomes = theme.get("outcomes") or {}
    if outcomes:
        outcomes_str = ", ".join(
            f"{c} {name}"
            for name, c in sorted(outcomes.items(), key=lambda x: -x[1])
        )
    else:
        outcomes_str = "unknown"

    prompt = (
        f"Tag: {tag}\n"
        f"Sessions clustered: {n}\n"
        f"Outcomes: {outcomes_str}\n"
        f"\n"
        f"Learnings from each session:\n"
        f"{bullets}\n"
        f"\n"
        f'Return JSON: {{"rule": "<imperative rule or NONE>"}}'
    )

    result = provider.invoke(_RULE_SYSTEM, prompt, json_schema=_RULE_SCHEMA)
    parsed = getattr(result, "parsed_json", None)
    if not parsed or "rule" not in parsed:
        return None
    rule = parsed["rule"]
    if not isinstance(rule, str):
        return None

    # Explicit no-rule signal from the LLM. Tolerate whitespace and
    # case (e.g., "  none  " or "None").
    if rule.strip().upper() == "NONE":
        return None

    # ---- Validation pipeline (deterministic, no LLM) ----
    # Length: inclusive on both bounds.
    if not (30 <= len(rule) <= 300):
        return None

    # Imperative marker must appear in the first 80 chars.
    head = rule.lower()[:80]
    if not any(marker in head for marker in _IMPERATIVE_MARKERS):
        return None

    # Self-reference blocklist (substring match on lowercased rule).
    rule_lower = rule.lower()
    if any(word in rule_lower for word in _SELF_REFERENCE_WORDS):
        return None

    # PR / issue numbers and UUIDs are point-in-time specifics.
    if _PR_NUMBER_RE.search(rule):
        return None
    if _UUID_RE.search(rule):
        return None

    # Absolute / machine-specific paths.
    if "/Users/" in rule or "~/" in rule or "C:\\" in rule:
        return None

    # Tag tautology: "Always {tag}." or "Never {tag}." (case-insensitive).
    tag_lower = str(tag).lower()
    stripped = rule.strip().rstrip(".!?;: ").lower()
    if stripped in (f"always {tag_lower}", f"never {tag_lower}"):
        return None

    return rule


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
                            candidates_dir: Path,
                            *,
                            provider=None) -> int:
    """Stage v2 theme candidates from clustered learnings.

    New contract (Mustafa 2026-05-11): a candidate is staged only when
    the LLM can synthesize a real behavioral rule from the cluster.
    No provider → no staging. Old v1 meta-prompt staging is gone in
    production; the helper that built those claims (`_theme_claim`) is
    kept private for internal callers (the v1 id path) but is never
    written through this function under the new contract.

    Provider=None: returns 0 immediately (zero-staging on no LLM is
    the codex-required degradation; never write meta-prompt fallbacks).
    Provider given: per-theme LLM synthesis through validation, with a
    consecutive-error circuit breaker that trips after
    `_MAX_CONSECUTIVE_LLM_ERRORS` and emits one stderr warning."""
    candidates_dir = Path(candidates_dir)
    candidates_dir.mkdir(parents=True, exist_ok=True)
    if provider is None:
        return 0

    # Lazy import: production callers have llm_providers on the path,
    # but importing it at module load would create a hard coupling for
    # callers that never touch the provider path (e.g., cluster-only
    # use). Tests load the real LLMError via the same lazy hook.
    try:
        from llm_providers.base import LLMError  # type: ignore
    except ImportError:
        class LLMError(Exception):  # pragma: no cover - graceful fallback
            pass

    staged = 0
    consecutive_errors = 0
    breaker_tripped = False
    skipped_due_to_breaker = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for theme in themes:
        if breaker_tripped:
            skipped_due_to_breaker += 1
            continue

        tid = _theme_id_v2(theme)
        path = candidates_dir / f"{tid}.json"
        if path.exists():
            continue
        if _is_already_decided(tid, candidates_dir):
            continue

        try:
            rule = _synthesize_rule_claim(theme, provider=provider)
        except LLMError:
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_LLM_ERRORS:
                breaker_tripped = True
            continue
        # Any successful LLM call (even one that returns NONE / fails
        # validation) resets the consecutive-error counter; the breaker
        # protects against provider outages, not against valid no-rule
        # answers.
        consecutive_errors = 0

        if rule is None:
            continue

        tag = theme.get("tag", "")
        sids = list(theme.get("session_ids") or [])
        outcomes = theme.get("outcomes") or {}
        # Salience: floor 3.9 at n=3, caps at 10 around n=24. Smoother
        # growth than v1's stair-step (min(10, max(5, n))); both UI paths
        # treat the field as a 0-10 ranking key so the curve shape isn't
        # load-bearing, but document the actual math here so a future
        # reader doesn't think the comment is stale.
        canonical_salience = min(10.0, 3.0 + len(sids) * 0.3)

        # Payload shape MUST match v1 for `triage_candidates.py`'s REPL
        # to render v2 candidates without crashing. Specifically:
        #   - `source` is a dict (the REPL does `data["source"].get("outcomes")`)
        #   - `decisions[*].ts` is the timestamp key (REPL reads `[:19]`)
        # Code review 2026-05-11 caught both as BLOCKING when the first
        # draft used a bare string for `source` and `at` for the
        # timestamp key.
        payload = {
            "id": tid,
            "key": tid,
            "name": tid,
            "claim": rule,
            "conditions": [tag],
            "origin": "theme.digest.v2",
            "evidence_ids": sids,
            "cluster_size": len(sids),
            "canonical_salience": canonical_salience,
            "staged_at": now,
            "status": "staged",
            "decisions": [{
                "ts": now,
                "reviewer": "theme_cluster.v2",
                "action": "staged",
                "note": (
                    f"Synthesized rule from {len(sids)} sessions tagged "
                    f"{tag!r}."
                ),
            }],
            "rejection_count": 0,
            "source": {
                "adapter": "theme_cluster.v2",
                "tag": tag,
                "outcomes": outcomes,
            },
        }
        path.write_text(json.dumps(payload, indent=2))
        staged += 1

    if breaker_tripped:
        print(
            f"theme_cluster: circuit breaker tripped after "
            f"{_MAX_CONSECUTIVE_LLM_ERRORS} consecutive LLM errors; "
            f"{skipped_due_to_breaker} theme(s) skipped this run.",
            file=sys.stderr,
        )

    return staged


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
    try:
        from llm_providers import resolve_provider, ProviderNotAvailable  # type: ignore
        provider = resolve_provider()
    except ProviderNotAvailable as e:
        print(
            f"theme_cluster: no LLM provider available; staging 0 "
            f"candidates this run. Reason: {e}",
            file=sys.stderr,
        )
        provider = None

    n = stage_theme_candidates(themes, candidates_dir, provider=provider)
    print(f"clustered {len(digests)} digests into {len(themes)} theme(s); "
          f"staged {n} new theme candidate(s)")
