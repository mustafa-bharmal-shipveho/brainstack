"""Tests for digest theme clustering (Phase 2b).

When ≥3 digests share a theme (overlap of domain_tags + similar
titles), the brain stages a "theme candidate" through the existing
candidates/ pipeline. The user reviews per-candidate via the same
`recall pending --review` UI and graduates the durable ones — closing
the raw-events → digests → themes → lessons loop.

Contract pinned:
  - cluster_themes(digests, min_size=3) returns themes only when ≥3
    digests share at least one tag (substring/overlap counts)
  - Themes carry session_ids of all member digests for provenance
  - stage_theme_candidates(themes, candidates_dir) writes one JSON per
    theme in the same shape as existing candidates (lifecycle metadata,
    decisions list)
  - Idempotent: re-running on the same input doesn't duplicate
    candidates (matched by content-derived id)
  - One-or-two-digest groups never become themes (no singleton churn)
  - Framework purity: no hardcoded taxonomy
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))


@pytest.fixture
def theme_mod():
    import theme_cluster
    return theme_cluster


def _d(sid, tags, title="t", learned="l", started="2026-05-10T12:00:00Z",
       outcome="completed"):
    """Build a minimal digest dict matching what theme_cluster expects.
    Real digests come from the markdown parser; tests use the dict
    shape directly for clarity."""
    return {
        "session_id": sid,
        "domain_tags": tags,
        "title": title,
        "what_was_learned": learned,
        "started_at": started,
        "outcome": outcome,
    }


class TestClustering:
    def test_three_digests_sharing_tag_form_a_theme(self, theme_mod):
        digests = [
            _d("s1", ["topic-a"], title="Investigation 1",
               learned="X is true"),
            _d("s2", ["topic-a", "topic-b"], title="Investigation 2",
               learned="Y is true"),
            _d("s3", ["topic-a"], title="Investigation 3",
               learned="Z is true"),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        assert len(themes) == 1
        theme = themes[0]
        assert theme["tag"] == "topic-a"
        assert sorted(theme["session_ids"]) == ["s1", "s2", "s3"]

    def test_two_digests_do_not_form_a_theme(self, theme_mod):
        """min_size=3 is the floor. Two digests sharing a tag are too
        weak a signal to surface — would create churn for one-off
        coincidences."""
        digests = [
            _d("s1", ["topic-a"]),
            _d("s2", ["topic-a"]),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        assert themes == []

    def test_digests_with_no_shared_tag_dont_cluster(self, theme_mod):
        digests = [
            _d("s1", ["topic-a"]),
            _d("s2", ["topic-b"]),
            _d("s3", ["topic-c"]),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        assert themes == []

    def test_multiple_themes_can_emerge_from_same_digests(self, theme_mod):
        """A digest with multiple tags can contribute to multiple
        themes simultaneously."""
        digests = [
            _d("s1", ["topic-a", "topic-b"]),
            _d("s2", ["topic-a", "topic-b"]),
            _d("s3", ["topic-a"]),
            _d("s4", ["topic-b"]),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        tags = sorted([t["tag"] for t in themes])
        assert tags == ["topic-a", "topic-b"]

    def test_same_tag_variants_in_one_digest_count_once(self, theme_mod):
        """A digest listing the same tag in multiple casings/whitespace
        variants ("auth-rewrite", " Auth-Rewrite ", "AUTH-rewrite") must
        count ONCE for that tag. Without per-digest dedup a single
        noisy digest can fabricate a 3-member theme on its own.

        Codex review caught this — bug was that case_for_tag and
        seen_keys were not per-digest scoped."""
        digests = [
            _d("s1", ["auth-rewrite", "Auth-Rewrite", " auth-rewrite "]),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        # Single digest cannot form a theme by self-multiplying its tags
        assert themes == []

    def test_empty_tags_dont_cluster(self, theme_mod):
        """A digest with an empty domain_tags list contributes nothing
        — the clustering keys on tags, not titles."""
        digests = [
            _d("s1", []),
            _d("s2", []),
            _d("s3", []),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        assert themes == []

    def test_theme_includes_outcome_distribution(self, theme_mod):
        """Each theme carries an outcome counter so the resulting
        candidate's claim can describe "3 sessions, 2 completed, 1
        blocked" without re-reading the digests."""
        digests = [
            _d("s1", ["topic-a"], outcome="completed"),
            _d("s2", ["topic-a"], outcome="completed"),
            _d("s3", ["topic-a"], outcome="blocked"),
        ]
        themes = theme_mod.cluster_themes(digests, min_size=3)
        assert len(themes) == 1
        outs = themes[0]["outcomes"]
        assert outs["completed"] == 2
        assert outs["blocked"] == 1


# ---------------------------------------------------------------------------
# Staging into the candidates pipeline
# ---------------------------------------------------------------------------

class TestStaging:
    def test_stage_writes_one_candidate_per_theme(self, theme_mod, tmp_path):
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        themes = [
            {"tag": "topic-a", "session_ids": ["s1", "s2", "s3"],
             "outcomes": {"completed": 2, "blocked": 1},
             "titles": ["t1", "t2", "t3"],
             "learnings": ["l1", "l2", "l3"]},
        ]
        n = theme_mod.stage_theme_candidates(themes, candidates_dir)
        assert n == 1
        # Look for the candidate JSON
        cands = list(candidates_dir.glob("*.json"))
        assert len(cands) == 1
        data = json.loads(cands[0].read_text())
        # Has all the required candidate fields
        for k in ("id", "claim", "evidence_ids", "cluster_size",
                  "canonical_salience", "status", "decisions"):
            assert k in data, f"missing field {k!r}"
        assert data["status"] == "staged"
        assert data["cluster_size"] == 3
        # claim mentions the tag and the count
        assert "topic-a" in data["claim"].lower()
        assert "3" in data["claim"]

    def test_stage_idempotent_on_rerun(self, theme_mod, tmp_path):
        """Re-staging the same theme set must not produce duplicate
        candidate files. Theme id is content-derived from tag + sorted
        session_ids."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        themes = [
            {"tag": "topic-a", "session_ids": ["s1", "s2", "s3"],
             "outcomes": {"completed": 3}, "titles": [],
             "learnings": []},
        ]
        theme_mod.stage_theme_candidates(themes, candidates_dir)
        theme_mod.stage_theme_candidates(themes, candidates_dir)
        cands = list(candidates_dir.glob("*.json"))
        assert len(cands) == 1, (
            f"expected 1 candidate after 2 stagings, got {len(cands)}"
        )

    def test_stage_skips_already_graduated(self, theme_mod, tmp_path):
        """If a theme's candidate id matches an already-graduated entry
        (candidates/graduated/<id>.json), don't re-stage — that theme
        already became a durable lesson and we'd be churning the user."""
        candidates_dir = tmp_path / "candidates"
        graduated = candidates_dir / "graduated"
        graduated.mkdir(parents=True)
        themes = [
            {"tag": "topic-a", "session_ids": ["s1", "s2", "s3"],
             "outcomes": {"completed": 3}, "titles": [],
             "learnings": []},
        ]
        # Compute the would-be id by running stage once into a scratch
        # dir, copying that id into graduated/, then trying again.
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        theme_mod.stage_theme_candidates(themes, scratch)
        scratch_cands = list(scratch.glob("*.json"))
        assert len(scratch_cands) == 1
        cid = json.loads(scratch_cands[0].read_text())["id"]
        # Pretend that one graduated
        (graduated / f"{cid}.json").write_text(
            json.dumps({"id": cid, "status": "graduated"})
        )
        n = theme_mod.stage_theme_candidates(themes, candidates_dir)
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# Framework purity
# ---------------------------------------------------------------------------

class TestFrameworkPurity:
    def test_no_hardcoded_tags_in_module(self, theme_mod):
        """The module must not reference any specific company-, tool-,
        or domain-specific tag literal. Themes come from the user's
        own digest tags, not a fixed taxonomy."""
        import inspect
        src = inspect.getsource(theme_mod).lower()
        forbidden = ["example-corp", "internal-service", "ticket-prefix", "customer-segment"]
        for f in forbidden:
            assert f not in src, f"theme_cluster contains {f!r}"
