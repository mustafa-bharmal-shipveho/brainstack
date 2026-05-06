"""Content-based clustering + deterministic pattern extraction.

Phase 3's replacement for action-prefix clustering. Works without an LLM:
similarity is Jaccard on word_set, and extraction picks a canonical episode
rather than synthesizing a new claim. Structured candidates flow through the
Phase 1 validation gate — if no LLM is available, they defer as before.
"""
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "harness"))
from salience import salience_score
from text import jaccard, word_set


def _normalize_claim(text):
    """Lowercase, strip punctuation, collapse whitespace.

    Used to derive a stable pattern id — same claim text must always produce
    the same id so lifecycle state (decisions, rejection_count, graduation
    status) carries across dream cycles even when the cluster membership
    shifts by one episode. Kept in sync with validate._normalize.
    """
    t = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def _canonicalize_condition(c):
    """Normalize a single condition token for stable hashing.

    - strip zero-width characters (ZWSP, ZWNJ, ZWJ, WJ, BOM)
    - casefold (more aggressive than lower; handles e.g. ß → ss)
    - collapse ALL unicode whitespace (spaces, tabs, NBSP, etc.) to single ' '
    - outer strip
    Returns "" if nothing substantive remains.
    """
    if not c:
        return ""
    c = _ZERO_WIDTH_RE.sub("", str(c))
    c = _WHITESPACE_RE.sub(" ", c).strip()
    return c.casefold()


def pattern_id(claim, conditions, origin=None):
    """Stable content-derived pattern id. Single source of truth.

    Same logical (claim, conditions, origin) → same id. Conditions are
    canonicalized before hashing: case-folded, unicode-whitespace
    collapsed, zero-width characters stripped, outer-trimmed, empties
    dropped, deduplicated, sorted. So `['Alpha']`, `[' alpha ']`,
    `['alpha\\u200b']`, and `['alpha']` all hash the same.

    What's NOT normalized: punctuation, hyphens, underscores. `'cross-region'`
    and `'cross_region'` are still distinct — they're likely distinct
    concepts. Callers who want them equivalent must pre-normalize.

    Origin discriminator: when `origin` is None or `coding.tool_call`
    (the legacy default), the hash input is the original `(claim,
    conditions)` shape — preserving lifecycle continuity for already-
    staged candidates from before PR1. When `origin` is a non-default
    value (agentry-side writers), it is mixed into the hash so cross-
    origin clusters with identical text don't collide on the same pid
    (codex review of PR1 caught this — `cluster_and_extract` keys its
    return dict by `name` which embeds pid; collision = lost cluster).
    """
    canonical = sorted({
        _canonicalize_condition(c)
        for c in (conditions or [])
        if _canonicalize_condition(c)
    })
    conditions_key = "|".join(canonical)
    origin_key = ""
    if origin and origin != DEFAULT_ORIGIN:
        origin_key = "||origin=" + str(origin)
    return hashlib.md5(
        (_normalize_claim(claim) + "||" + conditions_key + origin_key).encode()
    ).hexdigest()[:12]


DEFAULT_ORIGIN = "coding.tool_call"


def _origin_of(entry):
    """Origin discriminator for an episode.

    Missing or empty `origin` collapses to the legacy default
    (`coding.tool_call`) so existing data clusters identically to before
    the field was introduced.
    """
    o = entry.get("origin")
    if isinstance(o, str) and o:
        return o
    return DEFAULT_ORIGIN


def _entry_features(entry):
    """Content feature set for clustering.

    PR1 schema unification: new writers emit a one-line `summary` field.
    To keep `pattern_id` (claim+conditions hash) stable across the
    migration boundary, the feature set is the **union** of
    `word_set(summary)` and `word_set(action + reflection + detail)`.
    A pre-PR1 episode (no summary) and a post-PR1 episode (with summary
    derived as `reflection[:120]`) collapse to the same word set,
    because summary's tokens are already a subset of reflection's.
    Lifecycle state in `candidates/` (rejection_count, decisions) thus
    carries across the migration even when membership shifts by one
    episode. Empty/non-string summary contributes nothing.
    """
    summary = entry.get("summary")
    summary_text = summary if (isinstance(summary, str) and summary) else ""
    text = " ".join([
        summary_text,
        entry.get("action", "") or "",
        entry.get("reflection", "") or "",
        entry.get("detail", "") or "",
    ])
    return word_set(text)


def _cluster_one_bucket(featured, threshold, min_size):
    """The original single-linkage agglomeration, applied to a pre-bucketed list.

    Extracted so `content_cluster` can call it once per origin bucket
    when `group_by_origin=True`.
    """
    clusters = []  # each: list of (entry, feature_set)
    for item in featured:
        _, fs_i = item
        matching_indices = [
            i for i, c in enumerate(clusters)
            if any(jaccard(fs_i, fs_j) >= threshold for _, fs_j in c)
        ]
        if not matching_indices:
            clusters.append([item])
            continue
        # Merge the new item + every cluster it connects to into one.
        target = clusters[matching_indices[0]]
        target.append(item)
        # Absorb the rest, tail-first so indexing stays valid.
        for idx in reversed(matching_indices[1:]):
            target.extend(clusters[idx])
            del clusters[idx]
    return [[e for e, _ in c] for c in clusters if len(c) >= min_size]


def content_cluster(entries, threshold=0.3, min_size=2, group_by_origin=True):
    """Single-linkage agglomerative clustering on Jaccard similarity.

    An entry joins every existing cluster it's similar to, and all such
    clusters merge into one — proper single-linkage. Without the merge
    step, ordering matters: entries [A, C, B] where A~B~C but A⊄C would
    produce two clusters [A,B] + [C] instead of one, so recurrence
    counts and promotion thresholds become input-order dependent.

    Entries with empty feature sets are dropped (jaccard of two empty
    sets would otherwise be 1.0). Clusters smaller than min_size are
    filtered so singletons don't create candidate churn.

    `group_by_origin` (default True): pre-bucket entries by their
    `origin` field and run agglomeration WITHIN each bucket
    independently. Two entries from different origins (e.g. coding tool
    calls vs an agentry inbox action) never end up in the same cluster
    even when their feature sets are identical. Missing-origin entries
    collapse to `coding.tool_call` so legacy data clusters as before.
    Pass `group_by_origin=False` to fall back to the prior cross-origin
    clustering.
    """
    featured = [(e, _entry_features(e)) for e in entries]
    featured = [(e, fs) for e, fs in featured if fs]

    if not group_by_origin:
        return _cluster_one_bucket(featured, threshold, min_size)

    buckets = {}
    for e, fs in featured:
        buckets.setdefault(_origin_of(e), []).append((e, fs))
    out = []
    # Iterate buckets in insertion order so cluster output is deterministic.
    for bucket in buckets.values():
        out.extend(_cluster_one_bucket(bucket, threshold, min_size))
    return out


def _parse_iso_to_aware(iso):
    """Parse an ISO-8601 timestamp string to a tz-aware datetime.

    Returns None on missing/malformed input rather than raising — the
    caller decides whether bad timestamps disqualify a cluster. Naive
    timestamps are treated as UTC, matching `_age_factor` and
    `_count_recent_failures` elsewhere in brainstack.
    """
    if not iso or not isinstance(iso, str):
        return None
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_burst_cluster(
    cluster,
    *,
    max_evidence_count: int = 500,
    max_window_seconds: int = 1800,
    require_single_bucket: bool = True,
    chronic_evidence_count: int = 2000,
    min_dominant_bucket_fraction: float = 0.95,
):
    """Detect a noise cluster. Returns (is_burst, reason).

    Two detection paths, OR'd. Either trips → cluster is dropped.

    **Path A — time burst** (the original signal): a tight, dense, single-
    bucket spike. All three of:
      - count > max_evidence_count (default 500)
      - window < max_window_seconds (default 1800, strict)
      - single bucket (when require_single_bucket=True; see dominance below)

    **Path B — chronic single-bucket dominance**: a single (skill, result)
    pair has accumulated extreme volume regardless of time window.
    Field-graded against a real candidate that escaped earlier designs
    by mixing 7,021 success events with 76 failure events from the same
    skill — the dominant bucket holds 99% of events. Trips when:
      - count > chronic_evidence_count (default 2000, well above any
        legitimate single-skill+result cluster observed in practice)
      - single bucket (when require_single_bucket=True; see dominance below)
      - no window constraint

    **Dominant-bucket relaxation.** When `require_single_bucket=True`
    AND the largest bucket holds ≥ `min_dominant_bucket_fraction` of
    events (default 0.95), the cluster is TREATED as effectively
    single-bucket for both paths above. This catches the real-world
    case where a tiny minority of off-pattern events (e.g. 76 failures
    in a 7,000-event success spam) shouldn't shield the cluster from
    the detector. The reason string includes the dominance percentage
    when this relaxation triggers, so logs distinguish strict-single
    from dominant-single.

    `reason` is "" on a non-burst, and a compact stable string on
    a burst:
      - Path A strict-single: "burst: n=N window_s=S bucket=skill/result"
      - Path A dominant-single: "burst_dominant: n=N window_s=S bucket=skill/result frac=0.99"
      - Path A multi-bucket (relaxed): "burst_multi_bucket: ..."
      - Path B strict-single: "chronic_noise: n=N bucket=skill/result"
      - Path B dominant-single: "chronic_dominant: n=N bucket=skill/result frac=0.99"
      - Path B multi-bucket (relaxed): "chronic_multi_bucket: ..."

    Defensive on bad data: events with missing or malformed `timestamp`
    are excluded from the window calc but still counted toward evidence.
    If fewer than 2 valid timestamps remain the window is undefined —
    Path A bails, Path B may still trip on count alone.

    Pure function — no env reads, no I/O. Env-var integration lives in
    the caller (cluster_and_extract).
    """
    n = len(cluster)
    if n == 0:
        return False, ""

    # Bucket distribution: (skill, result) tuple → count
    bucket_counts = {}
    for e in cluster:
        key = (e.get("skill"), e.get("result"))
        bucket_counts[key] = bucket_counts.get(key, 0) + 1
    bucket_count = len(bucket_counts)
    dominant_bucket, dominant_n = max(bucket_counts.items(), key=lambda kv: kv[1])
    dominant_fraction = dominant_n / n if n > 0 else 0.0
    is_effectively_single = (
        bucket_count == 1
        or dominant_fraction >= min_dominant_bucket_fraction
    )

    if require_single_bucket and not is_effectively_single:
        return False, ""

    def _bucket_label():
        skill, result = dominant_bucket
        return f"{skill or '?'}/{result or '?'}"

    def _dominant_suffix():
        # Only annotate dominance when it triggered the relaxation
        # (i.e. bucket_count > 1 but fraction was high enough). Pure
        # single-bucket clusters get the simpler reason form.
        if bucket_count > 1:
            return "_dominant"
        return ""

    def _dominant_frac_suffix():
        if bucket_count > 1:
            return f" frac={dominant_fraction:.2f}"
        return ""

    # Path B — chronic dominance (no window constraint).
    # Checked first because it's strictly stronger on count: any cluster
    # tripping B will also trip A's count threshold (chronic > max).
    if n > chronic_evidence_count:
        if is_effectively_single:
            return True, (
                f"chronic{_dominant_suffix() or '_noise'}: n={n} "
                f"bucket={_bucket_label()}{_dominant_frac_suffix()}"
            )
        return True, (
            f"chronic_multi_bucket: n={n} bucket_count={bucket_count}"
        )

    # Path A — time burst.
    if n <= max_evidence_count:
        return False, ""

    parsed = []
    for e in cluster:
        dt = _parse_iso_to_aware(e.get("timestamp"))
        if dt is not None:
            parsed.append(dt)
    if len(parsed) < 2:
        return False, ""
    span_seconds = (max(parsed) - min(parsed)).total_seconds()
    if span_seconds >= max_window_seconds:
        return False, ""

    if is_effectively_single:
        prefix = "burst" + _dominant_suffix()
        return True, (
            f"{prefix}: n={n} window_s={int(span_seconds)} "
            f"bucket={_bucket_label()}{_dominant_frac_suffix()}"
        )
    return True, (
        f"burst_multi_bucket: n={n} bucket_count={bucket_count} "
        f"window_s={int(span_seconds)}"
    )


def extract_pattern(cluster):
    """Extractive summarization from a cluster of episodes.

    Without an LLM we cannot synthesize a generalization, so:
      - claim: canonical (highest-salience) member's reflection or action
        or summary (PR1: agentry-style writers may emit summary as the
        only narrative — codex review caught that summary-only clusters
        were producing empty claims and being silently skipped by
        `write_candidates`)
      - conditions: tokens shared by every cluster member
      - name: longest shared terms + content hash (deterministic, collision-free)
      - evidence_ids: all member timestamps
      - cluster_size: recurrence count
      - canonical_salience: salience of the canonical episode *boosted by*
        cluster_size. Repetition is a learning signal; a recurring-but-moderate
        pattern must be able to clear the promotion threshold even when no
        single episode was extreme. salience_score already caps recurrence at 3.
    """
    canonical = max(cluster, key=salience_score)
    claim = (
        canonical.get("reflection")
        or canonical.get("action")
        or canonical.get("summary")
        or ""
    ).strip()

    feature_sets = [_entry_features(e) for e in cluster]
    common = set.intersection(*feature_sets) if feature_sets else set()

    top_terms = sorted(common, key=lambda t: (-len(t), t))[:3]
    name_base = "_".join(top_terms) if top_terms else "untitled"
    # Id derived from normalized claim + conditions (shared tokens). Claim
    # alone would collide for generic canonical text (e.g., "the test
    # failed") occurring in unrelated contexts. Conditions usually stay
    # stable as cluster members shift (intersection of the cluster's common
    # vocabulary), so lifecycle history carries across membership changes
    # in the common case while genuinely-different clusters with the same
    # canonical get distinct ids.
    pid = pattern_id(claim, sorted(common))
    name = f"pattern_{name_base}_{pid[:6]}"

    # Recurrence-aware salience: give the scoring function cluster context
    # without mutating the source episode dict.
    canonical_with_recurrence = dict(canonical)
    canonical_with_recurrence["recurrence_count"] = len(cluster)
    canonical_salience = salience_score(canonical_with_recurrence)

    # All members of a cluster share an origin (when `group_by_origin=True`,
    # which is the default). For mixed-origin clusters from
    # `group_by_origin=False`, fall back to the canonical's origin so the
    # downstream candidate JSON still has a deterministic `origin` value.
    origin = _origin_of(canonical)

    # Recompute pid + name with origin so cross-origin clusters with
    # identical text don't collide (codex/api/schema persona finding).
    pid = pattern_id(claim, sorted(common), origin)
    name = f"pattern_{name_base}_{pid[:6]}"

    return {
        "id": pid,
        "name": name,
        "claim": claim,
        "conditions": sorted(common),
        "evidence_ids": [e.get("timestamp", "") for e in cluster if e.get("timestamp")],
        "cluster_size": len(cluster),
        "canonical_salience": canonical_salience,
        "origin": origin,
    }
