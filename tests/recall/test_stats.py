"""Tests for `recall stats` and the underlying aggregator.

The aggregator reads `events.log.jsonl` (written by the auto-recall hook),
filters AutoRecall events, and computes a StatsReport. The CLI command
`recall stats` renders the report; `--since 7d`, `--session-current`
narrow the window.

Telemetry shape comes from auto_recall.build_recall_block:
    x_outcome: hit | skip | timeout | unavailable | error
    x_skip_reason: too_short | slash | ack       (only when outcome=skip)
    x_latency_ms: int                            (only when fired)
    x_k_requested: int
    x_k_returned: int
    x_top_scores: list[float] (rounded to 2dp, max 3)
    x_sources: dict[name -> count]
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_event(log_path: Path, *, event: str = "AutoRecall",
                 ts_ms: int | None = None, session_id: str = "s",
                 **extensions) -> None:
    """Helper: append one event to the log with given extension fields."""
    record = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=ts_ms if ts_ms is not None else _now_ms(),
        event=event,
        session_id=session_id,
        turn=0,
        extensions={k: v for k, v in extensions.items() if k.startswith("x_")},
    )
    append_event(log_path, record)


class TestAggregateEvents:
    def test_empty_log_returns_zero_report(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        log.touch()
        report = aggregate_events(log)
        assert report.fired_count == 0
        assert report.skipped_count == 0
        assert report.surfaced_count == 0
        assert report.skip_reasons == {}
        assert report.top_sources == []

    def test_basic_aggregation_counts_hits_and_skips(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        # 3 hits, 2 skips
        for i in range(3):
            _write_event(
                log, x_outcome="hit", x_k_requested=5, x_k_returned=4,
                x_latency_ms=20 + i, x_top_scores=[0.8, 0.7, 0.6],
                x_sources={"brain": 2, "imports": 2},
            )
        _write_event(log, x_outcome="skip", x_skip_reason="too_short")
        _write_event(log, x_outcome="skip", x_skip_reason="ack")
        # Plus an unrelated UserPromptSubmit event — must not be counted
        _write_event(log, event="UserPromptSubmit")

        report = aggregate_events(log)
        assert report.fired_count == 3
        assert report.skipped_count == 2
        assert report.surfaced_count == 12  # 3 hits * 4 returned
        assert report.skip_reasons == {"too_short": 1, "ack": 1}
        assert dict(report.top_sources) == {"brain": 6, "imports": 6}

    def test_latency_percentiles(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        # Latencies 10, 20, 30, ... 100 → p50=55ms-ish, p95~95ms
        for i in range(10):
            _write_event(
                log, x_outcome="hit", x_k_requested=5, x_k_returned=1,
                x_latency_ms=(i + 1) * 10, x_top_scores=[0.8],
                x_sources={"brain": 1},
            )
        report = aggregate_events(log)
        # Don't pin exact percentile algorithm — just bounds
        assert 30 <= report.latency_p50_ms <= 70
        assert 80 <= report.latency_p95_ms <= 100

    def test_since_filter_excludes_old_events(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        old = _now_ms() - 10 * 24 * 60 * 60 * 1000  # 10 days ago
        recent = _now_ms() - 60 * 60 * 1000          # 1 hour ago
        _write_event(log, ts_ms=old, x_outcome="hit", x_k_requested=5,
                     x_k_returned=3, x_latency_ms=20, x_top_scores=[0.7],
                     x_sources={"brain": 3})
        _write_event(log, ts_ms=recent, x_outcome="hit", x_k_requested=5,
                     x_k_returned=2, x_latency_ms=15, x_top_scores=[0.8],
                     x_sources={"imports": 2})
        # 7d window should include only the recent event
        seven_days_ago = _now_ms() - 7 * 24 * 60 * 60 * 1000
        report = aggregate_events(log, since_ts_ms=seven_days_ago)
        assert report.fired_count == 1
        assert report.surfaced_count == 2

    def test_top_sources_sorted_by_count_desc(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        # imports gets 7 hits, brain gets 3, personal gets 1
        for sources, n in [({"imports": 1}, 7), ({"brain": 1}, 3), ({"personal": 1}, 1)]:
            for _ in range(n):
                _write_event(log, x_outcome="hit", x_k_requested=5,
                             x_k_returned=1, x_latency_ms=10,
                             x_top_scores=[0.9], x_sources=sources)
        report = aggregate_events(log)
        # First entry must be the most frequent source
        assert report.top_sources[0] == ("imports", 7)
        # Order: imports > brain > personal
        assert [s for s, _ in report.top_sources] == ["imports", "brain", "personal"]


class TestParseSince:
    """`--since 7d` / `--since 24h` / `--since 1h` and ISO date forms."""

    def test_parse_days(self):
        from recall.stats import parse_since
        # Pin the clock so the test is deterministic — no flakiness on slow CI
        anchor = 1_700_000_000_000
        ts = parse_since("7d", now_ms=anchor)
        assert ts == anchor - 7 * 24 * 60 * 60 * 1000

    def test_parse_hours(self):
        from recall.stats import parse_since
        anchor = 1_700_000_000_000
        ts = parse_since("24h", now_ms=anchor)
        assert ts == anchor - 24 * 60 * 60 * 1000

    def test_parse_iso_date(self):
        from recall.stats import parse_since
        ts = parse_since("2026-01-01")
        # Should produce midnight UTC ts for that date
        import datetime
        expected = int(datetime.datetime(2026, 1, 1,
                                          tzinfo=datetime.timezone.utc).timestamp() * 1000)
        assert ts == expected

    def test_parse_empty_returns_none(self):
        from recall.stats import parse_since
        assert parse_since("") is None
        assert parse_since(None) is None

    def test_parse_garbage_raises_clear_error(self):
        from recall.stats import parse_since
        with pytest.raises(ValueError):
            parse_since("nonsense")


class TestRenderHuman:
    """Pretty-printer is what the user sees. Pin the major sections so a
    refactor doesn't accidentally drop the ROI framing — that line is
    the whole point of the feature."""

    def test_render_includes_roi_framing(self, tmp_path: Path):
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=47,
            skipped_count=6,
            skip_reasons={"too_short": 4, "slash": 2},
            latency_p50_ms=38,
            latency_p95_ms=89,
            surfaced_count=234,
            top_sources=[("imports", 89), ("brain", 74)],
            top_paths=[],
            score_distribution={},
            window_start_ts_ms=_now_ms() - 7 * 24 * 60 * 60 * 1000,
            window_end_ts_ms=_now_ms(),
        )
        out = render_human(report)
        # ROI framing — the line that justifies auto-recall to the user
        assert "Without auto-recall" in out or "without auto-recall" in out.lower()
        # Anchor counts with surrounding context to avoid false positives
        # from numbers colliding with timestamps or other report fields
        assert "47 turns" in out
        assert "234 docs" in out
        # Latency labelled with p50/p95 markers
        assert "p50 38ms" in out
        assert "p95 89ms" in out
        # Top sources line lists imports with its count
        assert "imports (89)" in out

    def test_render_counts_diagnostic_outcomes(self, tmp_path: Path):
        """When the window contains only timeouts/errors/unavailable
        (no hits, no skips), `render_human` must NOT say "no events" —
        it should report the diagnostic count and roll those into the
        prompt-total denominator. Codex 2026-05-05 P2."""
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=0,
            skipped_count=0,
            other_outcomes={"timeout": 5, "unavailable": 2},
        )
        out = render_human(report)
        # Must NOT be the "no events" message
        assert "no auto-recall events" not in out.lower()
        # Diagnostic count surfaces
        assert "5 timeout" in out or "timeout" in out
        # Coverage denominator includes the diagnostic outcomes
        assert "0 turns / 7 prompts" in out

    def test_render_zero_count_message(self, tmp_path: Path):
        """No events yet — the report should still produce a sensible
        message rather than divide-by-zero or empty output."""
        from recall.stats import StatsReport, render_human
        empty = StatsReport(
            fired_count=0, skipped_count=0, skip_reasons={},
            latency_p50_ms=0, latency_p95_ms=0, surfaced_count=0,
            top_sources=[], top_paths=[], score_distribution={},
            window_start_ts_ms=None, window_end_ts_ms=None,
        )
        out = render_human(empty)
        # Some indication that there's nothing to report — anti-empty-output guard
        assert "no" in out.lower() or "0 turns" in out.lower() or "0 fires" in out.lower()


# ---------------------------------------------------------------------------
# Cross-source observability — Phase 1 of "should we federate?"
# ---------------------------------------------------------------------------


def _write_transcript_entry(path: Path, *, ts_iso: str, kind: str = "tool_use",
                             tool_name: str = "Bash", text: str = "") -> None:
    """Append one transcript entry. Mimics the shape of Claude Code's
    `~/.claude/projects/<slug>/<sid>.jsonl` files. Each entry is a JSON
    line with a `message.content` array of either tool_use blocks (with
    `name`) or text blocks. We only need to mock what the aggregator reads.
    """
    if kind == "tool_use":
        content = [{"type": "tool_use", "name": tool_name, "input": {}}]
    elif kind == "user_text":
        content = [{"type": "text", "text": text}]
    else:
        raise ValueError(f"unknown kind: {kind}")
    entry = {
        "type": "user" if kind == "user_text" else "assistant",
        "timestamp": ts_iso,
        "message": {"content": content},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


class TestToolCallAggregation:
    """`aggregate_tool_calls(transcripts_dir, since_ts_ms=...)` walks
    Claude Code session transcripts and counts `tool_use` blocks by name,
    grouped by MCP namespace. The data source is different from the
    AutoRecall events log — we read raw transcripts because the runtime
    PostToolUse hook only captures Bash/Edit/Read/Write (most MCP and
    builtin tool calls don't surface in events.log.jsonl)."""

    def test_aggregate_groups_mcp_calls_by_namespace(self, tmp_path: Path):
        """`mcp__minerva__search_code` and `mcp__minerva__get_file` both
        roll up into the `mcp__minerva__*` bucket. Different MCP servers
        get separate buckets so the user can see which one is being used."""
        from recall.stats import aggregate_tool_calls
        proj = tmp_path / "projects" / "-Users-foo-codebase"
        ts = "2026-05-05T12:00:00.000Z"
        _write_transcript_entry(
            proj / "session-a.jsonl", ts_iso=ts, tool_name="mcp__minerva__search_code"
        )
        _write_transcript_entry(
            proj / "session-a.jsonl", ts_iso=ts, tool_name="mcp__minerva__get_file"
        )
        _write_transcript_entry(
            proj / "session-a.jsonl", ts_iso=ts, tool_name="mcp__notebooklm__ask_question"
        )
        _write_transcript_entry(
            proj / "session-a.jsonl", ts_iso=ts, tool_name="Bash"
        )
        result = aggregate_tool_calls(tmp_path / "projects")
        assert result["mcp__minerva__*"] == 2
        assert result["mcp__notebooklm__*"] == 1
        assert result["Bash"] == 1

    def test_since_window_filters_old_calls(self, tmp_path: Path):
        """tool_use entries before `since_ts_ms` are excluded."""
        from recall.stats import aggregate_tool_calls
        proj = tmp_path / "projects" / "-Users-foo-codebase"
        old_ts = "2026-04-01T00:00:00.000Z"
        recent_ts = "2026-05-05T12:00:00.000Z"
        _write_transcript_entry(
            proj / "s.jsonl", ts_iso=old_ts, tool_name="mcp__minerva__search_code"
        )
        _write_transcript_entry(
            proj / "s.jsonl", ts_iso=recent_ts, tool_name="mcp__minerva__search_code"
        )
        # Window starting May 1 → only the recent call counts
        import datetime
        cutoff = int(datetime.datetime(2026, 5, 1,
                                        tzinfo=datetime.timezone.utc).timestamp() * 1000)
        result = aggregate_tool_calls(tmp_path / "projects", since_ts_ms=cutoff)
        assert result["mcp__minerva__*"] == 1

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        """No projects/ directory yet (fresh user / wrong path) → empty
        result, no exception."""
        from recall.stats import aggregate_tool_calls
        result = aggregate_tool_calls(tmp_path / "nonexistent")
        assert result == {}

    def test_malformed_lines_are_skipped(self, tmp_path: Path):
        """Real transcripts sometimes have non-JSON lines (debug output,
        truncated writes). Aggregator must not crash."""
        from recall.stats import aggregate_tool_calls
        proj = tmp_path / "projects" / "-Users-foo"
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(
            "{\"this\": \"is\", \"valid\": true}\n"  # but no message.content
            "this is not json\n"
            "{\"truncated\":\n"  # incomplete
        )
        # Plus one valid entry
        _write_transcript_entry(
            proj / "s.jsonl", ts_iso="2026-05-05T12:00:00.000Z",
            tool_name="mcp__minerva__search_code"
        )
        result = aggregate_tool_calls(tmp_path / "projects")
        assert result["mcp__minerva__*"] == 1


class TestRoutingCoverage:
    """The model is supposed to call `mcp__notebooklm__ask_question` for
    system-level questions and `mcp__minerva__*` for code-level ones (per
    CLAUDE.md routing rules). `routing_coverage()` computes how often that
    actually happened.

    Heuristic: simple keyword/regex match on the user's prompt text.
    Imperfect — produces false positives — but a useful signal."""

    def test_detects_system_level_question(self):
        from recall.stats import classify_prompt
        for prompt in [
            "How does the WMS architecture work end-to-end?",
            "Who owns the package routing service?",
            "What's the difference between PrOps and DMS?",
            "walk me through how facility orchestration handles inbound",
        ]:
            assert classify_prompt(prompt) == "system-level", (
                f"misclassified: {prompt!r}"
            )

    def test_detects_code_level_question(self):
        from recall.stats import classify_prompt
        for prompt in [
            "Where is FacilityProfile used in the monorepo?",
            "What repos depend on the inventory-management package?",
            "What events does the sort service emit?",
            "blast radius of changing this column in package_handling",
        ]:
            assert classify_prompt(prompt) == "code-level", (
                f"misclassified: {prompt!r}"
            )

    def test_neither_classification_for_random_prompts(self):
        """Most prompts in a coding session aren't system-level or
        code-level — they're task-specific. Don't force a classification."""
        from recall.stats import classify_prompt
        for prompt in [
            "fix the failing test",
            "thanks",
            "let's commit and push",
            "/dream",
        ]:
            assert classify_prompt(prompt) is None, (
                f"shouldn't classify: {prompt!r}"
            )

    def test_string_user_message_classified(self, tmp_path: Path):
        """Real Claude Code transcripts encode user turns with `content`
        as a plain string, not a content-block list. Without handling
        this shape, routing coverage misses the bulk of real prompts.
        Codex 2026-05-05 P2."""
        from recall.stats import compute_routing_coverage
        proj = tmp_path / "projects" / "-Users-foo"
        proj.mkdir(parents=True)
        ts = "2026-05-05T12:00:00.000Z"
        # User turn with STRING content (the common shape)
        entry_user = {
            "type": "user", "timestamp": ts,
            "message": {"role": "user", "content": "How does the WMS architecture work end-to-end?"},
        }
        # Assistant turn with notebooklm tool_use
        entry_assistant = {
            "type": "assistant", "timestamp": ts,
            "message": {"content": [
                {"type": "tool_use", "name": "mcp__notebooklm__ask_question"}
            ]},
        }
        with (proj / "s.jsonl").open("w") as f:
            f.write(json.dumps(entry_user) + "\n")
            f.write(json.dumps(entry_assistant) + "\n")
        coverage = compute_routing_coverage(tmp_path / "projects")
        assert coverage["system_level_total"] == 1
        assert coverage["system_level_notebooklm"] == 1
        assert coverage["system_level_coverage"] == 1.0

    def test_routing_coverage_computes_per_category_rates(self, tmp_path: Path):
        """Given a window where the user asked 4 system-level questions
        and 2 of them triggered notebooklm, coverage should be 0.5."""
        from recall.stats import compute_routing_coverage

        proj = tmp_path / "projects" / "-Users-foo"
        ts = "2026-05-05T12:00:00.000Z"
        # 4 system-level user prompts
        for q in ["how does X work", "who owns Y", "what's the difference between A and B", "walk me through Z"]:
            _write_transcript_entry(proj / "s.jsonl", ts_iso=ts, kind="user_text", text=q)
        # 2 of those windows had notebooklm calls; emulate by interleaving
        _write_transcript_entry(
            proj / "s.jsonl", ts_iso=ts,
            tool_name="mcp__notebooklm__ask_question"
        )
        _write_transcript_entry(
            proj / "s.jsonl", ts_iso=ts,
            tool_name="mcp__notebooklm__ask_question"
        )
        coverage = compute_routing_coverage(tmp_path / "projects")
        # 4 system-level questions, 2 notebooklm calls.
        # Implementation rounds to 1 decimal: 2/4 = 0.5
        assert coverage["system_level_total"] == 4
        assert coverage["system_level_notebooklm"] == 2
        assert coverage["system_level_coverage"] == 0.5


class TestStatsCliNoToolsFlag:
    """`recall stats --no-tools` is the perf escape hatch: when the user
    has hundreds of transcripts and only wants the auto-recall stats fast,
    skip the transcript scan entirely."""

    def test_aggregate_events_alone_does_not_touch_transcripts(self, tmp_path: Path,
                                                                monkeypatch):
        """The base `aggregate_events(log_path)` (existing API) must keep
        working with no transcripts dir at all — guards against regressions
        from the new file readers."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        log.touch()
        # No transcripts at all — should not raise
        report = aggregate_events(log)
        assert report.fired_count == 0


class TestRenderCrossSourceSection:
    """`render_human()` must include the new "Model-driven tool calls" and
    "Coverage check" sections when the report has the relevant fields
    populated. Empty fields → omit the section (don't print empty headers)."""

    def test_renders_mcp_calls_section(self):
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=10,
            mcp_calls={"mcp__minerva__*": 23, "mcp__notebooklm__*": 5},
            tool_calls_other={"Bash": 287, "Edit": 45},
        )
        out = render_human(report)
        assert "Model-driven tool calls" in out
        assert "mcp__minerva__*" in out and "23" in out
        assert "mcp__notebooklm__*" in out and "5" in out

    def test_omits_section_when_no_tool_calls(self):
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=10, mcp_calls={}, tool_calls_other={},
        )
        out = render_human(report)
        assert "Model-driven tool calls" not in out

    def test_renders_when_only_cross_source_data(self):
        """User has auto-recall disabled (zero AutoRecall events) but the
        transcript scan found tool calls — render must NOT bail to the
        'no events' message. Codex 2026-05-05 P2."""
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=0,
            skipped_count=0,
            other_outcomes={},  # zero AutoRecall events
            mcp_calls={"mcp__minerva__*": 12},
            tool_calls_other={"Bash": 100},
        )
        out = render_human(report)
        # Must NOT be the "no events" bail
        assert "no auto-recall events" not in out.lower()
        # Cross-source data still surfaces
        assert "mcp__minerva__*" in out
        assert "12" in out
        # And we don't emit meaningless "p50 0ms" lines
        assert "p50 0" not in out and "p95 0" not in out

    def test_renders_coverage_section_with_percentages(self):
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=10,
            routing_coverage={
                "system_level_total": 12,
                "system_level_notebooklm": 5,
                "system_level_coverage": 0.42,
                "code_level_total": 8,
                "code_level_minerva": 6,
                "code_level_coverage": 0.75,
            },
        )
        out = render_human(report)
        assert "Coverage check" in out
        # Both rates surface
        assert "42%" in out or "0.42" in out
        assert "75%" in out or "0.75" in out
