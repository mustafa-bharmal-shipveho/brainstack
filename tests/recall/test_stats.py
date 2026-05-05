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
