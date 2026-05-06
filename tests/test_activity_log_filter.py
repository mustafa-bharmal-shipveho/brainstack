"""Tests for the activity-log claim filter.

Companion to the burst-cluster filter (test_burst_filter.py). Where the
burst filter drops giant single-bucket spikes pre-extraction, this filter
drops small-to-medium clusters whose extracted claim is verbatim per-tool
narration from `_build_action()` in claude_code_post_tool.py — strings
like:

  - `Edited /path: replaced 'X' with 'Y'`
  - `Wrote /path (78 lines)`
  - `High-stakes op completed (migrate): cat ...`
  - `FAILURE in claude-code: ...`

These slip past the burst filter because cluster size is small (the user
hit cluster_size=4 on a plan-markdown editing burst), but they are never
useful as long-term lessons — they're activity log dressed up as
patterns. Filter at the same upstream gate (cluster_and_extract) so no
candidate id is ever persisted, closing the rejection-history loophole
the same way the burst filter does.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clear_dream_env(monkeypatch):
    """Scrub DREAM_* env so a stale shell can't change thresholds mid-suite."""
    for var in [
        "DREAM_ACTIVITY_LOG_DISABLED",
        "DREAM_BURST_DISABLED",
        "DREAM_BURST_MAX_EVIDENCE",
        "DREAM_BURST_MAX_WINDOW_SECONDS",
        "DREAM_BURST_REQUIRE_SINGLE_BUCKET",
        "DREAM_BURST_CHRONIC_COUNT",
        "DREAM_BURST_DOMINANT_FRACTION",
    ]:
        monkeypatch.delenv(var, raising=False)


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness"))

import cluster  # noqa: E402
import promote  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ep(text: str, *, ts: str = "2026-05-06T12:00:00+00:00",
        skill: str = "claude-code", result: str = "success") -> dict:
    """Episode whose `action` field carries the narrator output. The
    extractor's claim resolution is `reflection or action or summary`,
    so leaving reflection unset puts `action` in the canonical claim."""
    return {
        "timestamp": ts,
        "skill": skill,
        "result": result,
        "action": text,
        "detail": "",
        "pain_score": 5,
        "importance": 5,
    }


# ---------------------------------------------------------------------------
# Unit: _is_activity_log_claim — pure, no env, no disk
# ---------------------------------------------------------------------------

class TestIsActivityLogClaim:
    """Each shape produced by claude_code_post_tool._build_action and
    on_failure must be detected as activity log."""

    @pytest.mark.parametrize("claim", [
        "Edited /Users/me/.claude/plans/foo.md: replaced 'A' with 'B'",
        "Edited /tmp/file.txt",
        "Edited ./relative/path.py: replaced 'old' with 'new'",
    ])
    def test_edited_shapes_detected(self, claim):
        is_log, reason = cluster._is_activity_log_claim(claim)
        assert is_log is True
        assert reason.startswith("activity_log:edited")

    @pytest.mark.parametrize("claim", [
        "Wrote /Users/me/.claude/plans/foo.md (78 lines)",
        "Wrote /tmp/x.py (1 line)",
        "Wrote ./out.json (200 lines)",
        # Codex 2026-05-06 P2: paths can contain spaces. The original
        # `\S+` anchor missed `Wrote /Users/me/My File.md (10 lines)` and
        # let it leak through. Match must allow spaces up to the
        # `(N lines)` suffix.
        "Wrote /Users/me/My File.md (10 lines)",
        "Wrote /Users/me/Documents/has multiple spaces here.md (42 lines)",
    ])
    def test_wrote_shapes_detected(self, claim):
        is_log, reason = cluster._is_activity_log_claim(claim)
        assert is_log is True
        assert reason.startswith("activity_log:wrote")

    @pytest.mark.parametrize("claim", [
        "High-stakes op completed (migrate): cat $HOME/.agent/imports/extra.txt",
        "High-stakes op FAILED (secret): gh pr create --repo foo/bar",
        "High-stakes op completed (deploy): kubectl apply -f manifest.yaml",
    ])
    def test_high_stakes_shapes_detected(self, claim):
        is_log, reason = cluster._is_activity_log_claim(claim)
        assert is_log is True
        assert reason.startswith("activity_log:high")

    def test_command_failed_detected(self):
        is_log, reason = cluster._is_activity_log_claim(
            "Command failed: npm test"
        )
        assert is_log is True
        assert reason == "activity_log:command"

    def test_ran_detected(self):
        is_log, reason = cluster._is_activity_log_claim("Ran: ls -la")
        assert is_log is True
        assert reason == "activity_log:ran"

    @pytest.mark.parametrize("claim", [
        "Completed todo: refactor the parser",
        "Now working on: investigate the lock contention",
        "Updated todo list (5 items)",
    ])
    def test_todo_shapes_detected(self, claim):
        is_log, reason = cluster._is_activity_log_claim(claim)
        assert is_log is True

    def test_fallback_tool_completed_detected(self):
        is_log, reason = cluster._is_activity_log_claim(
            "Tool Glob completed successfully"
        )
        assert is_log is True
        assert reason == "activity_log:tool"

    def test_failure_in_skill_detected(self):
        """on_failure._refl shape: `FAILURE in <skill>: <error>`. The
        canonical pre-burst-filter rejection (1de077bc2298) had this
        prefix; small clusters of the same shape must also be filtered."""
        is_log, reason = cluster._is_activity_log_claim(
            "FAILURE in claude-code: TimeoutError: connection timed out"
        )
        assert is_log is True
        assert reason == "activity_log:failure"

    def test_standalone_edit_failed_detected(self):
        is_log, reason = cluster._is_activity_log_claim("Edit failed")
        assert is_log is True

    @pytest.mark.parametrize("claim", [
        "always validate user input before sending to the database",
        "test against real infrastructure before merging — mocks lie",
        "field names are contracts: rename HourUtc to HourLocal if it's local",
        "the dream pipeline must filter activity-log claims upstream",
        "writing tests against real Postgres caught the migration bug",
    ])
    def test_real_lessons_not_flagged(self, claim):
        """Sanity guard: prose-shaped lessons must pass through. If the
        regex over-matches, every legitimate insight gets dropped — far
        worse than letting some narration through."""
        is_log, reason = cluster._is_activity_log_claim(claim)
        assert is_log is False, (
            f"legitimate lesson misclassified as activity log: {claim!r} "
            f"(reason={reason!r})"
        )

    def test_empty_claim_not_flagged(self):
        assert cluster._is_activity_log_claim("") == (False, "")
        assert cluster._is_activity_log_claim(None) == (False, "")

    def test_leading_whitespace_tolerated(self):
        """Real episodes sometimes have stray whitespace; the matcher
        strips before matching so noise still gets caught."""
        is_log, _ = cluster._is_activity_log_claim(
            "   Wrote /tmp/foo.txt (10 lines)"
        )
        assert is_log is True


# ---------------------------------------------------------------------------
# Integration: cluster_and_extract drops activity-log clusters
# ---------------------------------------------------------------------------

class TestClusterAndExtractFiltersActivityLog:
    """End-to-end: clusters whose canonical claim matches an activity-log
    shape must produce no pattern, with telemetry reporting the skip."""

    def test_edit_narration_cluster_dropped(self):
        """The actual pending candidate (365e74886152) shape: 4 Edit
        calls on the same plan markdown. Pre-fix, this stages a
        candidate; post-fix, it produces no patterns."""
        entries = [
            _ep(
                f"Edited /Users/me/.claude/plans/foo.md: "
                f"replaced 'X{i}' with 'Y{i}'",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(5)
        ]
        result = promote.cluster_and_extract(entries)
        assert result == {}, (
            f"Edit-narration cluster must produce no candidates, "
            f"got {list(result)!r}"
        )

    def test_write_narration_cluster_dropped(self):
        """Cluster of `Wrote <path> (N lines)` narrations from a session
        of plan-markdown writes — same shape as rejected candidate
        ebcca1c125ac."""
        entries = [
            _ep(
                f"Wrote /Users/me/.claude/plans/note-{i}.md (78 lines)",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(4)
        ]
        result = promote.cluster_and_extract(entries)
        assert result == {}

    def test_high_stakes_completed_cluster_dropped(self):
        """`High-stakes op completed (...)` cluster — same shape as
        rejected candidate 04dfd61b2e68 (the false-positive `cat`
        tagged as `migrate`)."""
        entries = [
            _ep(
                "High-stakes op completed (migrate): "
                f"cat $HOME/.agent/imports/extra_sources_{i}.txt",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(3)
        ]
        result = promote.cluster_and_extract(entries)
        assert result == {}

    def test_telemetry_records_each_skip(self):
        """When `activity_log_telemetry=[]` is passed, every skipped
        cluster appends a structured entry. Lets `run_dream_cycle`
        emit `activity_log_skipped=N` without parsing stdout."""
        entries = [
            _ep(
                f"Wrote /tmp/file-{i}.txt (10 lines)",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(4)
        ]
        telemetry: list = []
        promote.cluster_and_extract(
            entries, activity_log_telemetry=telemetry
        )
        assert len(telemetry) == 1
        entry = telemetry[0]
        assert entry["reason"].startswith("activity_log:wrote")
        assert entry["cluster_size"] == 4
        assert "Wrote" in entry["claim_prefix"]

    def test_real_lesson_cluster_passes_through(self):
        """A cluster whose claim is prose, not narrator output, must
        NOT be dropped. Without this, every legitimate dream cycle
        breaks. Also asserts the surviving pattern's claim is the
        lesson text, catching the inverse failure mode."""
        lesson = "always run tests against real Postgres before shipping"
        entries = [
            _ep(lesson, ts=f"2026-05-06T12:0{i}:00+00:00")
            for i in range(3)
        ]
        result = promote.cluster_and_extract(entries)
        assert len(result) == 1, (
            f"prose lesson cluster must survive, got {list(result)!r}"
        )
        pattern = next(iter(result.values()))
        assert "postgres" in pattern["claim"].lower()

    def test_mixed_narration_and_lesson_keeps_only_lesson(self):
        """Most realistic case: activity-log noise + a real lesson in
        the same dream cycle. Filter is per-cluster, not global."""
        # 3 narration episodes (all share enough text to cluster together)
        entries = [
            _ep(
                f"Edited /tmp/plan.md: replaced 'foo' with 'bar{i}'",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(3)
        ]
        # 3 lesson episodes
        lesson = "field names are contracts; rename mismatched fields early"
        entries.extend([
            _ep(lesson, ts=f"2026-05-06T13:0{i}:00+00:00")
            for i in range(3)
        ])
        telemetry: list = []
        result = promote.cluster_and_extract(
            entries, activity_log_telemetry=telemetry
        )
        assert len(telemetry) == 1, (
            f"expected exactly 1 activity-log skip, got {telemetry!r}"
        )
        assert len(result) == 1, (
            f"lesson cluster should survive, got {list(result)!r}"
        )
        surviving = next(iter(result.values()))
        assert "field names" in surviving["claim"].lower()

    def test_kill_switch_disables_filter(self, monkeypatch):
        """`DREAM_ACTIVITY_LOG_DISABLED=1` is the operator escape hatch:
        all narration clusters pass through to candidates as before.
        Useful for forensic dream runs where you want to see the raw
        cluster output without the filter."""
        monkeypatch.setenv("DREAM_ACTIVITY_LOG_DISABLED", "1")
        entries = [
            _ep(
                f"Wrote /tmp/file-{i}.txt (10 lines)",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(4)
        ]
        telemetry: list = []
        result = promote.cluster_and_extract(
            entries, activity_log_telemetry=telemetry
        )
        assert len(result) == 1, (
            f"kill switch should pass narration through, got {list(result)!r}"
        )
        assert telemetry == [], (
            f"kill switch should not record telemetry, got {telemetry!r}"
        )

    def test_kill_switch_falsy_value_keeps_filter_active(self, monkeypatch):
        """`_env_bool` falsy values ('0', 'false', etc.) must NOT
        disable the filter. Mirrors the burst-filter env-bool contract."""
        monkeypatch.setenv("DREAM_ACTIVITY_LOG_DISABLED", "false")
        entries = [
            _ep(
                f"Wrote /tmp/file-{i}.txt (10 lines)",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(4)
        ]
        result = promote.cluster_and_extract(entries)
        assert result == {}

    def test_no_telemetry_kwarg_still_filters(self):
        """Backward-compat: callers that don't pass
        `activity_log_telemetry=` still get the filtering behavior;
        the kwarg only opts into per-skip records, not into filtering
        itself. Critical because run_dream_cycle's prior signature
        only had `telemetry=`."""
        entries = [
            _ep(
                f"Wrote /tmp/file-{i}.txt (10 lines)",
                ts=f"2026-05-06T12:0{i}:00+00:00",
            )
            for i in range(4)
        ]
        result = promote.cluster_and_extract(entries)  # no telemetry kwarg
        assert result == {}
