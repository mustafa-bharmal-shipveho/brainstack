"""Tests for cluster.py — origin-aware grouping + summary feature preference.

PR1 of the brainstack/agentry integration adds:
  - `_entry_features` reads `summary` first, falls back to `(action,
    reflection, detail)` for legacy episodes that don't carry a summary.
  - `content_cluster(..., group_by_origin=True)` pre-buckets entries by
    `entry.get("origin", "coding.tool_call")` and clusters within each
    bucket. Cross-origin events with identical text never end up in the
    same cluster.
  - `extract_pattern` stamps `origin` on the returned dict so candidates
    inherit the source bucket.

Backward compatibility: default `group_by_origin=True` collapses
existing data (no `origin` field) into a single `coding.tool_call`
bucket, producing the same cluster shape as before.
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness"))

import cluster  # noqa: E402


def _entry(text: str, *, origin=None, summary=None):
    """Build a minimally-filled episode for cluster tests."""
    e = {
        "timestamp": "2026-04-29T00:00:00+00:00",
        "skill": "claude-code",
        "action": text,
        "reflection": text,
        "detail": text,
        "pain_score": 5,
        "importance": 5,
    }
    if origin is not None:
        e["origin"] = origin
    if summary is not None:
        e["summary"] = summary
    return e


# --- _entry_features --------------------------------------------------

def test_entry_features_includes_summary_words():
    e = _entry(
        "command line text",
        summary="distinct content for clustering",
    )
    feats = cluster._entry_features(e)
    # Summary words should be in the feature set.
    assert "distinct" in feats
    assert "clustering" in feats
    # The action/reflection/detail words are *also* in the set —
    # _entry_features unions both so cluster pids stay stable across
    # the PR1 migration boundary (reliability-persona finding).
    assert "command" in feats
    assert "text" in feats


def test_entry_features_pid_stable_across_pre_post_pr1():
    """A pre-PR1 entry (no summary) and a post-PR1 entry (with summary
    derived as the first 120 chars of reflection) must produce the same
    feature set. Otherwise `pattern_id` shifts and lifecycle state in
    candidates/ detaches at the migration boundary."""
    pre_pr1 = _entry("alpha beta gamma delta epsilon")
    pre_pr1.pop("summary", None)  # legacy data has no summary
    # Post-PR1 derives summary from reflection[:120].
    post_pr1 = _entry("alpha beta gamma delta epsilon",
                      summary="alpha beta gamma delta epsilon")
    assert cluster._entry_features(pre_pr1) == cluster._entry_features(post_pr1)


def test_entry_features_falls_back_to_action_reflection_detail():
    """Legacy episode (no summary) still clusters via existing fields."""
    e = _entry("legacy text bash command failed")
    e.pop("summary", None)
    feats = cluster._entry_features(e)
    assert "legacy" in feats
    assert "bash" in feats
    assert "failed" in feats


def test_entry_features_empty_summary_falls_back():
    """Empty-string summary should not blank out the feature set."""
    e = _entry("real content here", summary="")
    feats = cluster._entry_features(e)
    assert "real" in feats
    assert "content" in feats


# --- content_cluster origin bucketing ---------------------------------

def test_content_cluster_separates_different_origins_with_identical_text():
    """Same words, different origins → different clusters."""
    text = "shared phrase about pnpm package manager"
    coding = [_entry(text, origin="coding.tool_call") for _ in range(3)]
    inbox = [_entry(text, origin="agentry.inbox.action") for _ in range(3)]
    clusters = cluster.content_cluster(
        coding + inbox, threshold=0.3, group_by_origin=True
    )
    # Two clusters, one per origin. Neither cluster mixes origins.
    assert len(clusters) == 2
    for c in clusters:
        origins = {e.get("origin") for e in c}
        assert len(origins) == 1, f"cluster mixes origins: {origins}"


def test_content_cluster_default_group_by_origin_is_true():
    """Default behaviour groups by origin (cross-origin isolation)."""
    text = "shared text"
    mixed = [
        _entry(text, origin="coding.tool_call"),
        _entry(text, origin="coding.tool_call"),
        _entry(text, origin="agentry.x.action"),
        _entry(text, origin="agentry.x.action"),
    ]
    clusters = cluster.content_cluster(mixed, threshold=0.3)
    assert len(clusters) == 2


def test_content_cluster_legacy_entries_default_to_coding_origin():
    """Entries with no `origin` field bucket together as 'coding.tool_call'."""
    legacy = [_entry("legacy bash") for _ in range(3)]
    coding = [_entry("legacy bash", origin="coding.tool_call") for _ in range(3)]
    clusters = cluster.content_cluster(
        legacy + coding, threshold=0.3, group_by_origin=True
    )
    # One bucket: missing origin treated identical to "coding.tool_call".
    assert len(clusters) == 1
    assert len(clusters[0]) == 6


def test_content_cluster_group_by_origin_false_preserves_legacy_behavior():
    """Opt-out preserves the prior cross-origin clustering."""
    text = "shared phrase here"
    mixed = [
        _entry(text, origin="coding.tool_call"),
        _entry(text, origin="coding.tool_call"),
        _entry(text, origin="agentry.x.action"),
    ]
    clusters = cluster.content_cluster(mixed, threshold=0.3, group_by_origin=False)
    # Without bucketing the three identical-text entries form one cluster.
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


# --- extract_pattern stamps origin ------------------------------------

def test_extract_pattern_stamps_origin_from_cluster():
    cluster_in = [
        _entry("identical text", origin="agentry.inbox.action") for _ in range(3)
    ]
    p = cluster.extract_pattern(cluster_in)
    assert p["origin"] == "agentry.inbox.action"


def test_extract_pattern_stamps_default_origin_for_legacy_cluster():
    """Cluster of legacy entries (no origin) gets stamped 'coding.tool_call'."""
    cluster_in = [_entry("legacy text") for _ in range(3)]
    p = cluster.extract_pattern(cluster_in)
    assert p["origin"] == "coding.tool_call"


# --- pattern_id origin discrimination (codex finding) ----------------

def test_pattern_id_default_origin_matches_legacy_hash():
    """coding.tool_call (default) must hash like origin=None for backward
    compat — pre-PR1 candidates keep their slugs."""
    pid_legacy = cluster.pattern_id("a claim", ["x", "y"])
    pid_default = cluster.pattern_id("a claim", ["x", "y"], "coding.tool_call")
    assert pid_legacy == pid_default


def test_pattern_id_non_default_origin_changes_hash():
    """Non-default origins produce distinct pids so cross-origin clusters
    with identical text don't collide on the same slug (codex finding)."""
    pid_default = cluster.pattern_id("a claim", ["x", "y"], "coding.tool_call")
    pid_inbox = cluster.pattern_id("a claim", ["x", "y"], "agentry.inbox.action")
    assert pid_default != pid_inbox


def test_extract_pattern_distinct_pid_across_origins():
    """Two clusters from different origins with identical claim text
    must produce distinct ids and names. Otherwise `cluster_and_extract`'s
    name-keyed dict overwrites one with the other."""
    text = "shared phrase here"
    c1 = [_entry(text, origin="coding.tool_call") for _ in range(3)]
    c2 = [_entry(text, origin="agentry.inbox.action") for _ in range(3)]
    p1 = cluster.extract_pattern(c1)
    p2 = cluster.extract_pattern(c2)
    assert p1["id"] != p2["id"]
    assert p1["name"] != p2["name"]


# --- claim falls back to summary (codex finding) ---------------------

def test_extract_pattern_falls_back_to_summary_for_claim():
    """When a cluster's canonical episode has no reflection or action
    (e.g., agentry-style writers using only summary), claim falls back
    to summary so write_candidates doesn't silently drop the cluster."""
    base = {
        "timestamp": "2026-04-29T00:00:00+00:00",
        "summary": "summary-only cluster claim",
        "pain_score": 9,
        "importance": 9,
        "origin": "agentry.x.action",
    }
    cluster_in = [dict(base) for _ in range(3)]
    p = cluster.extract_pattern(cluster_in)
    assert p["claim"] == "summary-only cluster claim"


# --- regression: clusters smaller than min_size are still filtered ----

def test_content_cluster_min_size_filter_within_bucket():
    """Filtering happens within each origin bucket, not across them."""
    # Two distinct origins, each with one isolated entry. Neither should
    # cluster (min_size=2 default).
    e1 = _entry("alpha", origin="coding.tool_call")
    e2 = _entry("beta", origin="agentry.x.action")
    clusters = cluster.content_cluster([e1, e2], threshold=0.3)
    assert clusters == []
