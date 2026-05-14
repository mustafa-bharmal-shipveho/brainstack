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
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))


@pytest.fixture
def theme_mod():
    import theme_cluster
    return theme_cluster


# ---------------------------------------------------------------------------
# Fake LLM provider stub (used by TestStagingWithLLM + updated TestStaging)
# ---------------------------------------------------------------------------

def _import_llm_error():
    """Sentinel helper that lets a stub `invoke()` raise the real
    LLMError without importing it at module-import time (production
    module imports the providers lazily, so tests stay loose-coupled)."""
    from llm_providers.base import LLMError
    return LLMError


class _StubProvider:
    """Minimal LLMProvider stub for tests.

    Records the (system, prompt, json_schema) tuple of each invoke()
    call so tests can assert on prompt-shaping behavior (e.g., the
    15-learning cap). Returns a pre-baked LLMResult-shaped object;
    we duck-type because the production caller only reads `.text`,
    `.parsed_json`, and `.provider` off the result.

    Schedule shapes:
      - `responses=[dict, dict, ...]`: each dict becomes parsed_json on
        successive calls. Mixed-in Exception values are raised instead
        of returned (so a single stub can fail-then-succeed without
        the test having to swap stubs). This lets the
        circuit-breaker-reset test pin that breaker state lives on
        the call to stage_theme_candidates, not on the provider object.
      - `raise_each_call=Exception(...)`: raise that exception on every
        invocation regardless of the response queue. Used by the
        all-errors breaker-trip test.
    """
    name = "stub"
    default_model = "stub-1"

    def __init__(self, responses, *, raise_each_call=None):
        self._responses = list(responses)
        self._raise_each = raise_each_call
        self.calls = []  # list[tuple[str, str, dict|None]]

    def is_available(self):
        return (True, "")

    def invoke(self, system, prompt, *, model=None, json_schema=None,
               max_budget_usd=0.10, timeout_s=60):
        self.calls.append((system, prompt, json_schema))
        # Global raise-on-every-call overrides the queue entirely.
        if self._raise_each is not None:
            raise self._raise_each
        if not self._responses:
            raise AssertionError(
                "stub provider called more times than responses supplied"
            )
        payload = self._responses.pop(0)
        # Mixed schedule: an Exception entry is raised, a dict is returned.
        if isinstance(payload, Exception):
            raise payload

        @dataclass
        class _Res:
            text: str
            parsed_json: dict | None
            tokens_in: int | None
            tokens_out: int | None
            provider: str
            model: str
            cost_usd: float | None

        return _Res(
            text=str(payload),
            parsed_json=payload,
            tokens_in=10,
            tokens_out=10,
            provider="stub",
            model="stub-1",
            cost_usd=0.0,
        )


def _theme(tag="topic-a", sids=None, learnings=None,
           outcomes=None, titles=None):
    """Build a theme dict matching what stage_theme_candidates expects.
    Defaults give a minimal 3-session theme."""
    return {
        "tag": tag,
        "session_ids": sids if sids is not None else ["s1", "s2", "s3"],
        "outcomes": outcomes if outcomes is not None else {"completed": 3},
        "titles": titles if titles is not None else [],
        "learnings": learnings if learnings is not None else [],
    }


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
    """The original staging contract, updated for the new provider-gated
    semantics. Each test now passes an LLM stub provider, since the new
    `stage_theme_candidates(themes, dir)` (no provider) writes zero
    candidates. The intent of each test is preserved."""

    def test_stage_writes_one_candidate_per_theme(self, theme_mod, tmp_path):
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        themes = [
            {"tag": "topic-a", "session_ids": ["s1", "s2", "s3"],
             "outcomes": {"completed": 2, "blocked": 1},
             "titles": ["t1", "t2", "t3"],
             "learnings": ["l1", "l2", "l3"]},
        ]
        stub = _StubProvider([
            {"rule": "Always X cleanly without hitting filters."},
        ])
        n = theme_mod.stage_theme_candidates(
            themes, candidates_dir, provider=stub,
        )
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
        # v2 id format
        assert data["id"].startswith("theme_v2_")
        # claim is the LLM rule, not the tag-frequency meta-prompt
        assert "Always X" in data["claim"]

    def test_stage_idempotent_on_rerun(self, theme_mod, tmp_path):
        """Re-staging the same theme set must not produce duplicate
        candidate files. Theme v2 id is content-derived from tag +
        sorted session_ids, so the second run hits the existing file."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        themes = [
            {"tag": "topic-a", "session_ids": ["s1", "s2", "s3"],
             "outcomes": {"completed": 3}, "titles": [],
             "learnings": []},
        ]
        # Two identical responses (in case the v2-id pre-check is
        # post-LLM-invoke for some impls). Either order is acceptable
        # so long as we end up with exactly one file.
        stub = _StubProvider([
            {"rule": "Always X cleanly without hitting filters."},
            {"rule": "Always X cleanly without hitting filters."},
        ])
        theme_mod.stage_theme_candidates(
            themes, candidates_dir, provider=stub,
        )
        theme_mod.stage_theme_candidates(
            themes, candidates_dir, provider=stub,
        )
        cands = list(candidates_dir.glob("*.json"))
        assert len(cands) == 1, (
            f"expected 1 candidate after 2 stagings, got {len(cands)}"
        )

    def test_stage_skips_already_graduated(self, theme_mod, tmp_path):
        """If a theme's v2 candidate id matches an already-graduated
        entry, don't re-stage AND don't pay LLM tokens to discover that.
        The provider must never be invoked for already-decided themes."""
        candidates_dir = tmp_path / "candidates"
        graduated = candidates_dir / "graduated"
        graduated.mkdir(parents=True)
        theme = {"tag": "topic-a", "session_ids": ["s1", "s2", "s3"],
                 "outcomes": {"completed": 3}, "titles": [],
                 "learnings": []}
        # Pre-seed graduated/ with the theme's v2 id.
        cid = theme_mod._theme_id_v2(theme)
        (graduated / f"{cid}.json").write_text(
            json.dumps({"id": cid, "status": "graduated"})
        )
        stub = _StubProvider([
            {"rule": "Should never be reached."},
        ])
        n = theme_mod.stage_theme_candidates(
            [theme], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []
        # Critical: no LLM tokens spent on a pre-decided theme.
        assert stub.calls == [], (
            f"provider was invoked for already-graduated theme: {stub.calls}"
        )


# ---------------------------------------------------------------------------
# Staging with LLM rule synthesis (Phase 2c: theme → imperative claim)
# ---------------------------------------------------------------------------

class TestStagingWithLLM:
    """The new provider-gated staging path. With no provider, zero
    candidates are written. With a provider, each theme runs through the
    LLM to synthesize an imperative rule, which is validated before the
    candidate is written. Invalid / NONE / restated-tag rules are
    silently skipped."""

    def test_v2_id_is_distinct_from_v1(self, theme_mod):
        """The v2 id namespace prevents collisions with any existing
        candidate file written by the original `_theme_id` formula."""
        theme = _theme(tag="topic-a", sids=["s1", "s2", "s3"])
        v1 = theme_mod._theme_id(theme)
        v2 = theme_mod._theme_id_v2(theme)
        assert v1 != v2
        assert v2.startswith("theme_v2_")

    def test_v2_id_is_stable_for_same_input(self, theme_mod):
        """Idempotency contract: same theme → same v2 id, every call."""
        theme = _theme(tag="topic-a", sids=["s1", "s2", "s3"])
        assert theme_mod._theme_id_v2(theme) == theme_mod._theme_id_v2(theme)

    def test_v2_id_format_matches_contract(self, theme_mod):
        """Pin the exact id derivation: lowercased tag, sorted
        session_ids, joined by '|', prefixed with 'v2||', md5[:12].
        Catches drift from a constant or a different hash scheme.

        Note we pass the tag in mixed case ("Topic-A") and the
        session_ids out of order ("s2", "s1", "s3") to prove the
        normalization happens inside _theme_id_v2."""
        import hashlib
        theme = {"tag": "Topic-A", "session_ids": ["s2", "s1", "s3"]}
        expected_payload = "v2||" + "topic-a" + "||" + "s1|s2|s3"
        expected = ("theme_v2_"
                    + hashlib.md5(expected_payload.encode()).hexdigest()[:12])
        assert theme_mod._theme_id_v2(theme) == expected

    def test_stage_with_no_provider_writes_no_candidates(self, theme_mod, tmp_path):
        """No provider → zero writes. The old tag-frequency claim is
        not a useful candidate on its own; the LLM-synthesized rule is
        the value-add. Without an LLM, stage is a no-op."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        n = theme_mod.stage_theme_candidates([_theme()], candidates_dir)
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_with_llm_returns_imperative_rule_creates_v2_candidate(
        self, theme_mod, tmp_path,
    ):
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        rule = (
            "Always run codex-review after Claude review for "
            "parallel second opinion."
        )
        stub = _StubProvider([{"rule": rule}])
        theme = _theme(
            tag="topic-a", sids=["s1", "s2", "s3"],
            learnings=["l1 content", "l2 content", "l3 content"],
        )
        n = theme_mod.stage_theme_candidates(
            [theme], candidates_dir, provider=stub,
        )
        assert n == 1
        cands = list(candidates_dir.glob("*.json"))
        assert len(cands) == 1
        data = json.loads(cands[0].read_text())
        assert data["id"].startswith("theme_v2_")
        assert data["claim"] == rule
        assert data["origin"] == "theme.digest.v2"
        assert data["conditions"] == ["topic-a"]
        assert data["cluster_size"] == 3

    def test_stage_with_llm_returns_NONE_skips_candidate(
        self, theme_mod, tmp_path,
    ):
        """The LLM explicitly returning {"rule":"NONE"} is the no-rule
        signal; we called the LLM but the answer was "nothing durable
        here". Skip without writing anything."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([{"rule": "NONE"}])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []
        assert len(stub.calls) == 1

    def test_stage_rejects_rule_with_self_reference_words(
        self, theme_mod, tmp_path,
    ):
        """A rule that talks about reviewing/graduating/etc. is
        self-referential meta-noise about the brain itself, not a
        durable workflow lesson. Skip."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always review every theme candidate before graduating."},
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_rule_with_pr_number_specificity(
        self, theme_mod, tmp_path,
    ):
        """PR numbers are point-in-time specifics, not durable rules.
        Trigger: `#\\d+` regex anywhere in the claim."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always run codex-review after the merge of PR #55 to main."},
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_rule_with_uuid_specificity(
        self, theme_mod, tmp_path,
    ):
        """A UUID-shaped session id is a one-off identifier, not a
        durable rule. Trigger: UUID regex."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always inspect session "
                     "b3a1c2d4-1234-5678-9abc-def012345678 after a "
                     "Codex error."},
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_rule_with_absolute_path_specificity(
        self, theme_mod, tmp_path,
    ):
        """Absolute filesystem paths bind a rule to one machine. Skip.
        Trigger: literal `/Users/`, `~/`, or `C:\\`."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always set CLAUDE_CONFIG=/Users/foo/bar before invoking."},
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_non_imperative_rule(self, theme_mod, tmp_path):
        """Descriptive statements are not actionable rules. Require an
        imperative marker (Always/Never/Prefer/Avoid/etc.) in the first
        80 chars."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Code review is important."},
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_rule_too_short(self, theme_mod, tmp_path):
        """Below 30 chars is too terse to carry actionable context."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always X."},  # 9 chars
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_rule_too_long(self, theme_mod, tmp_path):
        """Above 300 chars is a paragraph, not a rule. Skip."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always " + ("x" * 400)},
        ])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_rule_merely_restating_tag(self, theme_mod, tmp_path):
        """`Always code-review.` for tag `code-review` is a tautology,
        not a rule. Skip when the claim is just `always {tag}.` or
        `never {tag}.` (case-insensitive)."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Always code-review."},
        ])
        theme = _theme(tag="code-review")
        n = theme_mod.stage_theme_candidates(
            [theme], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_rejects_tag_restate_in_never_form_and_case_insensitive(
        self, theme_mod, tmp_path,
    ):
        """The tautology filter must catch BOTH `always {tag}.` and
        `never {tag}.`, and must be case-insensitive on both sides:
        tag='Code-Review', rule='Never CODE-REVIEW.' is still a
        tautology and must be rejected."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([
            {"rule": "Never CODE-REVIEW."},
        ])
        theme = _theme(tag="Code-Review")
        n = theme_mod.stage_theme_candidates(
            [theme], candidates_dir, provider=stub,
        )
        assert n == 0
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_accepts_rule_at_exact_min_length_30(
        self, theme_mod, tmp_path,
    ):
        """Boundary: a rule of EXACTLY 30 chars (the min) passes
        length validation. This pins inclusive-min semantics: 29
        rejects, 30 accepts."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        # Construct a rule that:
        #   - starts with an imperative marker ("Always")
        #   - has no filter triggers (no PR#, UUID, path, self-ref words)
        #   - is exactly 30 chars long
        rule = "Always do thing carefully sirs"
        assert len(rule) == 30
        stub = _StubProvider([{"rule": rule}])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 1
        cands = list(candidates_dir.glob("*.json"))
        assert len(cands) == 1
        assert json.loads(cands[0].read_text())["claim"] == rule

    def test_stage_accepts_rule_at_exact_max_length_300(
        self, theme_mod, tmp_path,
    ):
        """Boundary: a rule of EXACTLY 300 chars (the max) passes
        length validation. Pins inclusive-max semantics: 300 accepts,
        301 rejects."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        # "Always " (7 chars) + filler to make exactly 300.
        rule = "Always " + ("x" * (300 - len("Always ")))
        assert len(rule) == 300
        stub = _StubProvider([{"rule": rule}])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 1
        cands = list(candidates_dir.glob("*.json"))
        assert len(cands) == 1
        assert json.loads(cands[0].read_text())["claim"] == rule

    @pytest.mark.parametrize("bad_rule", [
        "Always edit ~/.config/foo before invoking the tool.",
        "Always set PATH on C:\\Users\\foo for Windows installs to work.",
        "Always cd into /Users/foo/bar before running the migrate script.",
    ])
    def test_stage_rejects_tilde_and_windows_paths(
        self, theme_mod, tmp_path, bad_rule,
    ):
        """Machine-specific paths bind a rule to one filesystem. Skip.
        Triggers: `~/`, `C:\\`, `/Users/`."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        stub = _StubProvider([{"rule": bad_rule}])
        n = theme_mod.stage_theme_candidates(
            [_theme()], candidates_dir, provider=stub,
        )
        assert n == 0, f"rule with path leaked through: {bad_rule!r}"
        assert list(candidates_dir.glob("*.json")) == []

    def test_stage_caps_learnings_to_15_per_theme(self, theme_mod, tmp_path):
        """At most _MAX_LEARNINGS_PER_THEME (15) distinct learnings are
        sent to the LLM, so a noisy 50-session theme can't blow the
        token budget. Contract: EXACTLY 15 must appear (cap not floor):
        production must keep some — dropping all learnings produces a
        useless prompt — and must trim down to exactly the cap."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        learnings = [f"learning-{i:03d} content body" for i in range(1, 51)]
        theme = _theme(
            tag="topic-a",
            sids=[f"s{i}" for i in range(1, 51)],
            learnings=learnings,
        )
        stub = _StubProvider([
            {"rule": "Always X cleanly without hitting the 15-learning cap."},
        ])
        theme_mod.stage_theme_candidates(
            [theme], candidates_dir, provider=stub,
        )
        assert len(stub.calls) == 1
        _, prompt, _ = stub.calls[0]
        # Sanity: prompt is not empty.
        assert len(prompt) > 0
        # Exactly 15 distinct learning-NNN tokens, not just <= 15.
        matches = re.findall(r"learning-\d{3}", prompt)
        distinct = set(matches)
        assert theme_mod._MAX_LEARNINGS_PER_THEME == 15
        assert len(distinct) == 15, (
            f"expected exactly 15 distinct learnings in prompt, "
            f"got {len(distinct)}"
        )

    def test_stage_truncates_each_learning_to_600_chars(
        self, theme_mod, tmp_path,
    ):
        """Each individual learning is truncated to
        _MAX_LEARNING_CHARS (600) before being inlined into the prompt.
        Contract: content is PRESERVED (truncated, not dropped) AND no
        single learning run exceeds 600 chars."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        # Three distinctive filler chars so we can confirm each survived.
        big_a = "A" * 2000
        big_b = "B" * 2000
        big_c = "C" * 2000
        theme = _theme(
            tag="topic-a",
            sids=["s1", "s2", "s3"],
            learnings=[big_a, big_b, big_c],
        )
        stub = _StubProvider([
            {"rule": "Always X cleanly without exceeding the per-learning cap."},
        ])
        theme_mod.stage_theme_candidates(
            [theme], candidates_dir, provider=stub,
        )
        _, prompt, _ = stub.calls[0]
        # Sanity: prompt is not empty.
        assert len(prompt) > 0
        # Content preserved (truncated, not dropped) for all three.
        assert "AAA" in prompt
        assert "BBB" in prompt
        assert "CCC" in prompt
        # No single contiguous run exceeds the cap.
        assert theme_mod._MAX_LEARNING_CHARS == 600
        longest_a = max((len(m) for m in re.findall(r"A+", prompt)), default=0)
        longest_b = max((len(m) for m in re.findall(r"B+", prompt)), default=0)
        longest_c = max((len(m) for m in re.findall(r"C+", prompt)), default=0)
        assert longest_a <= 600, f"A-run was {longest_a} chars; cap is 600"
        assert longest_b <= 600, f"B-run was {longest_b} chars; cap is 600"
        assert longest_c <= 600, f"C-run was {longest_c} chars; cap is 600"

    def test_stage_skips_theme_after_3_consecutive_llm_errors(
        self, theme_mod, tmp_path, capsys,
    ):
        """Circuit breaker contract: EXACTLY 3 consecutive LLM errors
        trips the breaker for the remainder of the invocation. We pass
        5 themes; the stub raises LLMError on every call. Pin:
          - exactly 3 invocations attempted (NOT 1, 2, or 4 — calls 4
            and 5 are skipped because the breaker tripped after call 3)
          - the warning emitted to stderr says 'circuit breaker tripped'
            and mentions 'skipped' (so the user sees what was suppressed)
        """
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        LLMError = _import_llm_error()
        themes = [_theme(tag=f"t{i}", sids=[f"a{i}", f"b{i}", f"c{i}"])
                  for i in range(5)]
        stub = _StubProvider([], raise_each_call=LLMError("boom"))
        n = theme_mod.stage_theme_candidates(
            themes, candidates_dir, provider=stub,
        )
        assert n == 0
        assert theme_mod._MAX_CONSECUTIVE_LLM_ERRORS == 3
        # EXACTLY 3, not <=3 (would pass if production stopped after 1 or 2).
        assert len(stub.calls) == 3, (
            f"expected exactly 3 LLM calls before breaker trips, "
            f"got {len(stub.calls)}"
        )
        captured = capsys.readouterr()
        combined = (captured.err + captured.out).lower()
        # Production must emit a specific user-facing warning.
        assert "circuit breaker tripped" in combined, (
            f"breaker warning missing: {combined!r}"
        )
        assert "skipped" in combined, (
            f"warning must mention skipped themes: {combined!r}"
        )

    def test_stage_circuit_breaker_resets_per_call_to_stage_theme_candidates(
        self, theme_mod, tmp_path,
    ):
        """Breaker state is scoped to a single stage_theme_candidates
        call, not persisted across invocations.

        This test uses ONE stub instance across both calls. If
        production accidentally stored breaker state on the provider
        object (e.g., `provider._error_count`), the second call would
        still see a tripped breaker and write zero candidates. By
        sharing the stub, we catch that bug — the only valid place for
        breaker state is the stack frame of stage_theme_candidates.

        Schedule: the stub raises LLMError on calls 1, 2, 3 (tripping
        the breaker on call #1 to stage_theme_candidates), then returns
        valid rules on calls 4 through 8 (the 5 themes in the second
        stage call).
        """
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        LLMError = _import_llm_error()
        themes_a = [_theme(tag=f"a{i}", sids=[f"a{i}-1", f"a{i}-2", f"a{i}-3"])
                    for i in range(5)]
        themes_b = [_theme(tag=f"b{i}", sids=[f"b{i}-1", f"b{i}-2", f"b{i}-3"])
                    for i in range(5)]
        good = "Always X cleanly without hitting filters."
        # 3 errors (trip breaker on first stage call) then 5 successes
        # (one per theme in the second stage call). Single shared stub.
        stub = _StubProvider([
            LLMError("e1"), LLMError("e2"), LLMError("e3"),
            {"rule": good}, {"rule": good}, {"rule": good},
            {"rule": good}, {"rule": good},
        ])
        # Call 1: breaker should trip after 3 errors; 0 staged.
        n1 = theme_mod.stage_theme_candidates(
            themes_a, candidates_dir, provider=stub,
        )
        assert n1 == 0
        # Call 2: breaker MUST be reset (it's per-call, not per-provider).
        n2 = theme_mod.stage_theme_candidates(
            themes_b, candidates_dir, provider=stub,
        )
        assert n2 == 5, (
            "breaker state persisted across stage_theme_candidates calls — "
            "must be per-invocation scope"
        )
        v2_cands = [c for c in candidates_dir.glob("*.json")
                    if json.loads(c.read_text())["id"].startswith("theme_v2_")]
        assert len(v2_cands) == 5

    def test_stage_isolated_llm_errors_do_not_trip_breaker(
        self, theme_mod, tmp_path,
    ):
        """Alternating fail/succeed pattern: 5 themes, errors on 1/3/5.
        Themes 2 and 4 stage successfully; breaker stays closed because
        no run of CONSECUTIVE errors hits the threshold."""
        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        LLMError = _import_llm_error()
        themes = [_theme(tag=f"t{i}", sids=[f"a{i}", f"b{i}", f"c{i}"])
                  for i in range(5)]
        good = "Always X cleanly without hitting filters."
        # Mixed schedule (one stub, interleaved exceptions + dicts).
        stub = _StubProvider([
            LLMError("e1"),
            {"rule": good},
            LLMError("e2"),
            {"rule": good},
            LLMError("e3"),
        ])
        n = theme_mod.stage_theme_candidates(
            themes, candidates_dir, provider=stub,
        )
        # Themes 2 and 4 staged successfully.
        assert n == 2
        # All 5 themes attempted — breaker never tripped.
        assert len(stub.calls) == 5

    def test_synthesize_returns_validated_rule_when_present(self, theme_mod):
        """Direct unit on _synthesize_rule_claim: a clean imperative
        rule passes validation and is returned verbatim."""
        rule = "Always prefer composition over inheritance for shared behavior."
        stub = _StubProvider([{"rule": rule}])
        out = theme_mod._synthesize_rule_claim(_theme(), provider=stub)
        assert out == rule

    def test_synthesize_returns_none_when_llm_says_NONE(self, theme_mod):
        stub = _StubProvider([{"rule": "NONE"}])
        out = theme_mod._synthesize_rule_claim(_theme(), provider=stub)
        assert out is None

    def test_synthesize_returns_none_when_validation_rejects(self, theme_mod):
        """Self-referential rule fails validation → None (not the raw
        rule). The validator is the last line of defense before write."""
        stub = _StubProvider([{"rule": "Review every theme."}])
        out = theme_mod._synthesize_rule_claim(_theme(), provider=stub)
        assert out is None


# ---------------------------------------------------------------------------
# Seam: staged candidate -> triage REPL display
#
# Tests both sides of the contract together. Code-review 2026-05-11 caught
# two payload-shape regressions (source as bare string, decisions[*].at vs
# .ts) because earlier tests exercised the staging side and the REPL side
# in isolation. The seam they share is the JSON on disk.
# ---------------------------------------------------------------------------

class TestStagedV2CandidateRoundTrip:
    def test_v2_candidate_renders_through_triage_repl_without_error(
        self, theme_mod, tmp_path, capsys
    ):
        """Stage a v2 candidate via stage_theme_candidates, then load the
        JSON and run triage_candidates._print_candidate against it. This
        is the integration test the code reviewer asked for: the staging
        side and the REPL side share a contract (the candidate JSON
        schema), and we test that contract end-to-end here.

        Specifically pins:
          - `source` is a dict the REPL can `.get("outcomes")` on
          - `decisions[*].ts` is the timestamp key the REPL reads
          - `origin` starts with "theme.digest" so the REPL's outcome
            branch fires
          - `_behavioral_value` classifies a v2 rule claim as "rule"
            (not "marker" or "unknown")
          - `_recommend` returns the [g] graduate action for a v2 rule
        """
        import sys
        sys.path.insert(0, "agent/tools")
        import triage_candidates

        rule_text = (
            "Always validate environment variable deployment before "
            "removing hardcoded fallbacks in production code."
        )
        stub = _StubProvider([{"rule": rule_text}])
        themes = [{
            "tag": "pagerduty",
            "session_ids": ["s1", "s2", "s3"],
            "outcomes": {"completed": 2, "abandoned": 1},
            "titles": ["t1", "t2", "t3"],
            "learnings": ["l1", "l2", "l3"],
        }]

        candidates_dir = tmp_path / "candidates"
        candidates_dir.mkdir()
        n = theme_mod.stage_theme_candidates(
            themes, candidates_dir, provider=stub
        )
        assert n == 1

        candidate_file = next(candidates_dir.glob("theme_v2_*.json"))
        data = json.loads(candidate_file.read_text())

        # Contract assertions before the REPL touches it.
        assert isinstance(data["source"], dict), (
            "source must be a dict; REPL calls .get('outcomes') on it"
        )
        assert "outcomes" in data["source"]
        assert data["source"]["outcomes"] == {"completed": 2, "abandoned": 1}
        assert all("ts" in d for d in data["decisions"]), (
            "decisions[*].ts is the timestamp key the REPL reads with [:19]"
        )
        assert data["origin"].startswith("theme.digest"), (
            "REPL outcome branch and value-classifier key on this prefix"
        )

        # _print_candidate must not raise. Idx + total are arbitrary.
        triage_candidates._print_candidate(data, 0, 1)

        out = capsys.readouterr().out
        # The rule claim shows up in the rendered preview.
        assert rule_text[:40] in out
        # The recommendation is [g] graduate for a real imperative rule.
        assert "[g] graduate" in out
        # The outcomes line renders for theme.digest.v2 (regression guard
        # for the `origin == "theme.digest"` hardcode the reviewer caught).
        assert "2 completed" in out and "1 abandoned" in out


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
        forbidden = ["veho", "shipveho", "cart-location", "facility-ops"]
        for f in forbidden:
            assert f not in src, f"theme_cluster contains {f!r}"
