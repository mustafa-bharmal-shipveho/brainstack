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

def test_entry_features_prefers_summary_when_present():
    e = _entry(
        "ignored ignored ignored",
        summary="distinct content for clustering",
    )
    feats = cluster._entry_features(e)
    # Summary words should be in the feature set.
    assert "distinct" in feats
    assert "clustering" in feats
    # action/reflection/detail words ("ignored") should NOT dominate when
    # summary is present.
    assert "ignored" not in feats


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


# --- regression: clusters smaller than min_size are still filtered ----

def test_content_cluster_min_size_filter_within_bucket():
    """Filtering happens within each origin bucket, not across them."""
    # Two distinct origins, each with one isolated entry. Neither should
    # cluster (min_size=2 default).
    e1 = _entry("alpha", origin="coding.tool_call")
    e2 = _entry("beta", origin="agentry.x.action")
    clusters = cluster.content_cluster([e1, e2], threshold=0.3)
    assert clusters == []
