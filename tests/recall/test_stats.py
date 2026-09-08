"""Tests for `recall stats` and the underlying aggregator.

The aggregator reads RAW JSON lines from `events.log.jsonl` (written by
the auto-recall hook) and from its rotated siblings `events.log*.jsonl`
in the same directory, filters AutoRecall records, and computes a
`StatsReport`. It deliberately does NOT go through
`runtime.core.events.load_events`: that loader rejects any record whose
`schema_version` differs from the runtime's current constant, so a log
spanning the 1.1 -> 1.2 hook upgrade raises on the first line written by
the other version. The live log is 70 MB of mixed versions.

Telemetry contract v1.2 (see the hook plan, "Telemetry contract v1.2"):

    x_outcome      hit | miss | dedup | skip | timeout | unavailable | error
    x_skip_reason  too_short | slash | ack        (skip only)
    x_latency_ms   full worker wall, on EVERY non-skip outcome (incl. timeout)
    x_query_ms     retrieval-only wall
    x_path         daemon | inproc
    x_daemon_error str | null            x_degraded  bool
    x_index_stale  bool — daemon hit/miss/dedup ONLY; absent = unknown
    x_k_requested / x_k_candidates / x_k_gated_out / x_k_dedup / x_k_returned
    x_paths        injected doc paths, brain-relative (+ x_paths_truncated)
    x_top_scores   RRF scores           x_rerank_scores  cross-encoder scores
    x_sources      per-source counts of injected docs

Records written before v1.2 — or that claim 1.2 but log a `hit` with no
`x_paths` — carry the OLD semantics, where "hit" could mean zero docs
were actually injected (a phantom hit). Those are summarized in a
separate `legacy` block and are NEVER mixed into the 1.2 numbers.
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

import pytest


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Omit:
    """Sentinel: pass `x_paths=OMIT` to leave the key out of the record."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "OMIT"


OMIT = _Omit()


def _write_raw(log_path: Path, record: dict) -> dict:
    """Append one RAW JSON line. No schema validation, no EventRecord.

    The aggregator has to survive whatever is already on disk, so the
    tests write bytes the same way the hook does rather than going
    through the strict dump/load pair.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def _write_event(log_path: Path, *, event: str = "AutoRecall",
                 ts_ms: int | None = None, session_id: str = "s",
                 turn: int = 0, schema_version: str = "1.2",
                 **extensions) -> dict:
    """Append one AutoRecall event. Defaults to the v1.2 contract.

    Extension keys are flattened to the top level of the record, exactly
    as `runtime.core.events._event_to_dict` writes them. For 1.2 hits we
    auto-fill `x_paths` (a 1.2 hit without paths is legacy by
    definition) and `x_path="daemon"` for every non-skip outcome.
    """
    ext = {k: v for k, v in extensions.items() if k.startswith("x_")}
    omitted = {k for k, v in ext.items() if isinstance(v, _Omit)}
    for key in omitted:
        ext.pop(key)
    outcome = ext.get("x_outcome")
    if schema_version == "1.2" and event == "AutoRecall":
        if outcome == "hit" and "x_paths" not in ext and "x_paths" not in omitted:
            k_returned = int(ext.get("x_k_returned", 1) or 1)
            ext["x_paths"] = [f"memory/d{i}.md" for i in range(k_returned)]
            ext.setdefault("x_paths_truncated", False)
        if (outcome not in (None, "skip") and "x_path" not in ext
                and "x_path" not in omitted):
            ext["x_path"] = "daemon"
    record = {
        "schema_version": schema_version,
        "ts_ms": ts_ms if ts_ms is not None else _now_ms(),
        "event": event,
        "session_id": session_id,
        "turn": turn,
    }
    record.update(ext)
    return _write_raw(log_path, record)


def _v12(log_path: Path, **fields) -> dict:
    return _write_event(log_path, schema_version="1.2", **fields)


def _v11(log_path: Path, **fields) -> dict:
    return _write_event(log_path, schema_version="1.1", **fields)


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
        assert report.total_fires == 0
        assert report.total_prompts == 0
        assert report.coverage_pct == 0.0
        assert int(report.legacy.get("events", 0)) == 0

    def test_basic_aggregation_counts_hits_and_skips(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        # 3 hits, 2 skips
        for i in range(3):
            _v12(
                log, x_outcome="hit", x_k_requested=5, x_k_returned=4,
                x_latency_ms=20 + i, x_top_scores=[0.8, 0.7, 0.6],
                x_sources={"brain": 2, "imports": 2},
            )
        _v12(log, x_outcome="skip", x_skip_reason="too_short")
        _v12(log, x_outcome="skip", x_skip_reason="ack")
        # Plus an unrelated UserPromptSubmit event — must not be counted
        _v12(log, event="UserPromptSubmit")

        report = aggregate_events(log)
        assert report.fired_count == 3
        assert report.skipped_count == 2
        assert report.surfaced_count == 12  # 3 hits * 4 returned
        assert report.skip_reasons == {"too_short": 1, "ack": 1}
        assert dict(report.top_sources) == {"brain": 6, "imports": 6}
        # Retrieval was attempted 3 times; 5 prompts reached the hook.
        assert report.total_fires == 3
        assert report.total_prompts == 5
        assert report.coverage_pct == pytest.approx(100.0)

    def test_latency_percentiles(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        # Latencies 10, 20, 30, ... 100 → p50=55ms-ish, p95~95ms
        for i in range(10):
            _v12(
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
        _v12(log, ts_ms=old, x_outcome="hit", x_k_requested=5,
             x_k_returned=3, x_latency_ms=20, x_top_scores=[0.7],
             x_sources={"brain": 3})
        _v12(log, ts_ms=recent, x_outcome="hit", x_k_requested=5,
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
                _v12(log, x_outcome="hit", x_k_requested=5,
                     x_k_returned=1, x_latency_ms=10,
                     x_top_scores=[0.9], x_sources=sources)
        report = aggregate_events(log)
        # First entry must be the most frequent source
        assert report.top_sources[0] == ("imports", 7)
        # Order: imports > brain > personal
        assert [s for s, _ in report.top_sources] == ["imports", "brain", "personal"]

    # ---- v1.2 outcome classification -----------------------------------

    def test_v12_outcomes_split_hit_miss_dedup_timeout(self, tmp_path: Path):
        """Every 1.2 outcome lands in its own counter, and the two
        denominators are distinct: `total_fires` counts prompts where
        retrieval actually ran, `total_prompts` adds the skips."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        for _ in range(2):
            _v12(log, x_outcome="hit", x_k_returned=2)
        for _ in range(3):
            _v12(log, x_outcome="miss", x_k_candidates=4, x_k_gated_out=4)
        _v12(log, x_outcome="dedup", x_k_dedup=3)
        _v12(log, x_outcome="timeout")
        _v12(log, x_outcome="unavailable")
        _v12(log, x_outcome="error")
        _v12(log, x_outcome="skip", x_skip_reason="too_short")
        _v12(log, x_outcome="skip", x_skip_reason="slash")

        report = aggregate_events(log)
        assert report.fired_count == 2
        assert report.miss_count == 3
        assert report.dedup_count == 1
        assert report.other_outcomes == {"timeout": 1, "unavailable": 1, "error": 1}
        assert report.skipped_count == 2
        assert report.skip_reasons == {"too_short": 1, "slash": 1}
        # hit + miss + dedup + timeout + unavailable + error
        assert report.total_fires == 9
        assert report.total_prompts == 11
        assert report.coverage_pct == pytest.approx(100 * 2 / 9)
        assert report.miss_pct == pytest.approx(100 * 3 / 9)
        assert report.dedup_pct == pytest.approx(100 * 1 / 9)
        assert report.timeout_pct == pytest.approx(100 * 1 / 9)

    def test_worker_latency_includes_timeout_and_miss(self, tmp_path: Path):
        """`x_latency_ms` is the full worker wall on every NON-skip
        outcome. Skips (which never start a worker) must stay out of the
        population or they drag p50 to ~0 and hide real slowness."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1, x_latency_ms=100)
        _v12(log, x_outcome="miss", x_latency_ms=100)
        _v12(log, x_outcome="dedup", x_latency_ms=100)
        _v12(log, x_outcome="timeout", x_latency_ms=900)
        for _ in range(5):
            _v12(log, x_outcome="skip", x_skip_reason="too_short", x_latency_ms=1)

        report = aggregate_events(log)
        # 5 skip latencies of 1ms would pull p50 down to 1
        assert report.latency_p50_ms == 100
        # the 900ms timeout must be inside the population
        assert report.latency_p95_ms >= 500

    def test_query_latency_from_x_query_ms(self, tmp_path: Path):
        """Retrieval-only latency is a separate population from the full
        worker wall — the whole point of splitting them in v1.2."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        for outcome in ("hit", "miss", "dedup"):
            _v12(log, x_outcome=outcome, x_k_returned=1,
                 x_latency_ms=900, x_query_ms=50)
        # timeout has a worker wall but never produced a query time
        _v12(log, x_outcome="timeout", x_latency_ms=900)

        report = aggregate_events(log)
        assert report.query_p50_ms == 50
        assert report.query_p95_ms == 50
        assert report.latency_p50_ms == 900

    def test_path_split_and_daemon_errors(self, tmp_path: Path):
        """daemon vs in-process split, plus the two degradation counters.
        Skips never ran a worker, so they are outside the split; a
        non-skip event with no `x_path` is reported as unknown rather
        than silently attributed to either path."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1, x_path="daemon",
             x_daemon_error=None)
        _v12(log, x_outcome="miss", x_path="daemon", x_degraded=True)
        _v12(log, x_outcome="dedup", x_path="inproc",
             x_daemon_error="connection_refused: no listener")
        _v12(log, x_outcome="error", x_path=OMIT)
        _v12(log, x_outcome="skip", x_skip_reason="slash")

        report = aggregate_events(log)
        assert report.path_split == {"daemon": 2, "inproc": 1, "unknown": 1}
        assert report.daemon_error_count == 1
        assert report.daemon_error_by_reason == {"connection_refused": 1}
        assert report.degraded_count == 1

    def test_daemon_errors_bucketed_by_reason(self, tmp_path: Path):
        """`daemon_error_count` alone cannot tell "the daemon was never
        installed" (`no_socket`) from "the daemon is alive but busy"
        (`timeout`) — and those two need opposite fixes. The hook writes
        `x_daemon_error` as `"<reason>: <detail>"`, so bucket by the
        reason prefix. Anything outside the known reason set lands in
        `other` rather than being dropped: a bucket nobody recognizes is
        still a signal, and silently discarding it would make the
        breakdown disagree with `daemon_error_count`."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="miss", x_path="inproc",
             x_daemon_error="no_socket: /run/recall.sock missing")
        _v12(log, x_outcome="miss", x_path="inproc",
             x_daemon_error="no_socket: /run/recall.sock missing")
        _v12(log, x_outcome="unavailable", x_path="daemon",
             x_daemon_error="timeout: budget 800ms exceeded")
        _v12(log, x_outcome="unavailable", x_path="daemon",
             x_daemon_error="server_error: store locked")
        _v12(log, x_outcome="miss", x_path="inproc",
             x_daemon_error="protocol_error: short read")
        _v12(log, x_outcome="miss", x_path="inproc",
             x_daemon_error="import_error: No module named 'recall'")
        _v12(log, x_outcome="miss", x_path="inproc",
             x_daemon_error="connection_refused: no listener")
        # No `<reason>:` prefix at all — the hook's `fail_reason or
        # "unknown"` fallback and any future reason land here.
        _v12(log, x_outcome="miss", x_path="inproc", x_daemon_error="unknown")
        # Counted but never bucketed away: a skip never started a worker.
        _v12(log, x_outcome="skip", x_skip_reason="slash",
             x_daemon_error="no_socket: never reached")
        _v12(log, x_outcome="hit", x_k_returned=1, x_path="daemon")

        report = aggregate_events(log)
        assert report.daemon_error_count == 8
        assert report.daemon_error_by_reason == {
            "no_socket": 2,
            "connection_refused": 1,
            "timeout": 1,
            "server_error": 1,
            "protocol_error": 1,
            "import_error": 1,
            "other": 1,
        }
        # The breakdown is a partition of the count, never a subset.
        assert sum(report.daemon_error_by_reason.values()) == report.daemon_error_count

    def test_daemon_error_by_reason_empty_when_no_errors(self, tmp_path: Path):
        """No errors means no breakdown — an empty dict, not a dict of
        zeros. `render_human` keys the parenthetical off emptiness."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1, x_path="daemon",
             x_daemon_error=None)
        report = aggregate_events(log)
        assert report.daemon_error_count == 0
        assert report.daemon_error_by_reason == {}

    def test_index_stale_absent_is_unknown_not_false(self, tmp_path: Path):
        """`x_index_stale` is emitted only on the daemon path, and only
        for hit/miss/dedup. The in-process path has no way to know
        whether the index is stale, so an absent flag means UNKNOWN.
        Counting absent as false would report a fresh index the daemon
        never vouched for — which is exactly the failure the flag exists
        to catch. `index_stale_known` is therefore the denominator, not
        the daemon event count."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1, x_path="daemon",
             x_index_stale=True)
        _v12(log, x_outcome="miss", x_path="daemon", x_index_stale=False)
        _v12(log, x_outcome="dedup", x_path="daemon", x_index_stale=False)
        # daemon timeout: the worker was killed before a response arrived
        _v12(log, x_outcome="timeout", x_path="daemon")
        # in-process path never carries the flag
        _v12(log, x_outcome="hit", x_k_returned=1, x_path="inproc")
        _v12(log, x_outcome="miss", x_path="inproc")
        _v12(log, x_outcome="skip", x_skip_reason="too_short")

        report = aggregate_events(log)
        assert report.index_stale_count == 1
        # only the three events that actually carried the flag
        assert report.index_stale_known == 3

    def test_index_stale_known_is_zero_when_no_daemon_events(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1, x_path="inproc")
        _v12(log, x_outcome="miss", x_path="inproc")
        report = aggregate_events(log)
        assert report.index_stale_count == 0
        assert report.index_stale_known == 0

    def test_k_counters_summed_over_non_skip(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=2, x_k_candidates=10,
             x_k_gated_out=4, x_k_dedup=1)
        _v12(log, x_outcome="miss", x_k_candidates=10, x_k_gated_out=4,
             x_k_dedup=1)
        # A skip never ran retrieval; its counters must not leak in.
        _v12(log, x_outcome="skip", x_skip_reason="ack", x_k_candidates=99,
             x_k_gated_out=99, x_k_dedup=99)

        report = aggregate_events(log)
        assert report.k_candidates_total == 20
        assert report.k_gated_out_total == 8
        assert report.k_dedup_total == 2

    def test_repeat_injection_rate_same_session_only(self, tmp_path: Path):
        """A doc re-injected into the SAME session is a repeat (the model
        already saw it). The same doc injected into a different session
        is a fresh injection, not a repeat."""
        from recall.stats import aggregate_events

        cross = tmp_path / "cross.log.jsonl"
        _v12(cross, session_id="a", ts_ms=1_000, x_outcome="hit",
             x_k_returned=1, x_paths=["memory/x.md"])
        _v12(cross, session_id="b", ts_ms=2_000, x_outcome="hit",
             x_k_returned=1, x_paths=["memory/x.md"])
        assert aggregate_events(cross).repeat_injection_rate == pytest.approx(0.0)

        same = tmp_path / "same.log.jsonl"
        _v12(same, session_id="a", ts_ms=1_000, x_outcome="hit",
             x_k_returned=1, x_paths=["memory/x.md"])
        _v12(same, session_id="a", ts_ms=2_000, x_outcome="hit",
             x_k_returned=1, x_paths=["memory/x.md"])
        # 2 injected paths, 1 of them already seen this session
        assert aggregate_events(same).repeat_injection_rate == pytest.approx(0.5)

    def test_rerank_histogram_buckets(self, tmp_path: Path):
        """Cross-encoder scores are raw logits (S4 calibration 2026-09-04:
        jina turbo ranges about -4.3..0.9, MiniLM about -11.4..2.5), so the
        buckets sit at the RERANK_BUCKET_EDGES (-2.5, -1.5, -0.75, 0.0),
        each half-open on the left (a score exactly at an edge belongs to
        the bucket that starts there)."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1,
             x_rerank_scores=[-3.0, -2.5, -1.5])
        _v12(log, x_outcome="hit", x_k_returned=1,
             x_rerank_scores=[-0.75, 0.0, 0.9])
        report = aggregate_events(log)
        assert report.rerank_distribution == {
            "<-2.5": 1, "-2.5..-1.5": 1, "-1.5..-0.75": 1, "-0.75..0": 1, "0+": 2,
        }

    def test_rrf_score_histogram_unchanged(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1,
             x_top_scores=[0.9, 0.75, 0.55, 0.4])
        report = aggregate_events(log)
        assert report.score_distribution == {
            "0.85+": 1, "0.70-0.85": 1, "0.50-0.70": 1, "<0.50": 1,
        }

    def test_top_paths_populated_from_x_paths(self, tmp_path: Path):
        """`top_paths` was permanently empty before v1.2 because paths
        were never logged. v1.2 logs brain-relative paths, so the field
        now carries the ten most-injected docs."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        for i in range(3):
            _v12(log, session_id=f"s{i}", x_outcome="hit", x_k_returned=2,
                 x_paths=["memory/semantic/lessons/foo.md",
                          "imports/claude/plans/bar.md"])
        _v12(log, session_id="s9", x_outcome="hit", x_k_returned=1,
             x_paths=["memory/semantic/lessons/foo.md"])
        report = aggregate_events(log)
        assert report.top_paths[0] == ("memory/semantic/lessons/foo.md", 4)
        assert ("imports/claude/plans/bar.md", 3) in report.top_paths
        assert len(report.top_paths) <= 10

    def test_top_paths_capped_at_ten(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=15,
             x_paths=[f"memory/p{i}.md" for i in range(15)])
        assert len(aggregate_events(log).top_paths) == 10


class TestLegacyBlock:
    """Pre-1.2 events are summarized separately and never merged into the
    1.2 numbers: in 1.1 a logged `hit` could carry zero injected docs, so
    averaging the two populations produces a number that means nothing."""

    def test_v11_events_go_to_legacy_only(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v11(log, x_outcome="hit", x_k_returned=3, x_latency_ms=500,
             x_sources={"imports": 3})
        _v11(log, x_outcome="skip", x_skip_reason="too_short")
        _v11(log, x_outcome="timeout")
        _v12(log, x_outcome="hit", x_k_returned=2, x_sources={"brain": 2})

        report = aggregate_events(log)
        # 1.2 population untouched by the legacy rows
        assert report.fired_count == 1
        assert report.skipped_count == 0
        assert report.other_outcomes == {}
        assert report.total_fires == 1
        assert report.total_prompts == 1
        assert report.surfaced_count == 2
        assert dict(report.top_sources) == {"brain": 2}
        # legacy rolled up on its own
        assert report.legacy["events"] == 3
        assert report.legacy["hit_logged"] == 1
        assert report.legacy["skip"] == 1
        assert report.legacy["timeout"] == 1
        assert report.legacy["surfaced_count"] == 3
        assert dict(report.legacy["top_sources"]) == {"imports": 3}

    def test_phantom_is_hit_with_zero_k(self, tmp_path: Path):
        """The 1.1 hook logged `hit` even when it injected nothing. That
        is the single biggest reason the old coverage number was wrong,
        so the legacy block names it explicitly."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v11(log, x_outcome="hit", x_k_returned=0)
        _v11(log, x_outcome="hit", x_k_returned=0)
        _v11(log, x_outcome="hit", x_k_returned=2)

        legacy = aggregate_events(log).legacy
        assert legacy["hit_logged"] == 3
        assert legacy["phantom_hits"] == 2
        assert legacy["real_hits"] == 1

    def test_v12_hit_without_x_paths_is_legacy(self, tmp_path: Path):
        """A record can claim 1.2 and still be pre-1.2 in substance. A
        hit with no `x_paths` cannot be joined to a transcript and may be
        a phantom, so it is classified legacy — but a 1.2 MISS has no
        paths by definition and stays in the 1.2 population."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=2, x_paths=OMIT)
        _v12(log, x_outcome="miss")

        report = aggregate_events(log)
        assert report.fired_count == 0
        assert report.miss_count == 1
        assert report.total_fires == 1
        assert report.legacy["events"] == 1
        assert report.legacy["hit_logged"] == 1

    def test_legacy_query_latency_from_x_latency_ms(self, tmp_path: Path):
        """In 1.1 `x_latency_ms` measured retrieval only, so it maps to
        the legacy block's query-latency fields — not to the 1.2
        full-worker percentiles."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        for _ in range(3):
            _v11(log, x_outcome="hit", x_k_returned=1, x_latency_ms=500)

        report = aggregate_events(log)
        assert report.legacy["query_p50_ms"] == 500
        assert report.legacy["query_p95_ms"] == 500
        assert report.latency_p50_ms == 0
        assert report.query_p50_ms == 0

    def test_legacy_block_has_documented_keys(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v11(log, x_outcome="hit", x_k_returned=1, x_latency_ms=10)
        legacy = aggregate_events(log).legacy
        for key in ("events", "hit_logged", "phantom_hits", "real_hits",
                    "skip", "timeout", "unavailable", "error",
                    "query_p50_ms", "query_p95_ms", "surfaced_count",
                    "top_sources"):
            assert key in legacy, f"missing legacy field: {key}"

    def test_legacy_empty_when_all_events_are_v12(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1)
        assert aggregate_events(log).legacy["events"] == 0


class TestRawReader:
    """`iter_auto_recall_records` is the tolerant reader that replaced
    `runtime.core.events.load_events` for stats."""

    def test_mixed_schema_lines_do_not_raise(self, tmp_path: Path):
        """The live log spans the 1.1 -> 1.2 upgrade. A strict loader
        raises on whichever version isn't the current constant; this one
        reads both and classifies them."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=2)
        _v11(log, x_outcome="hit", x_k_returned=3)
        _write_event(log, schema_version="1.0", x_outcome="hit", x_k_returned=4)

        report = aggregate_events(log)
        assert report.fired_count == 1
        assert report.surfaced_count == 2
        assert report.legacy["events"] == 2
        assert report.legacy["hit_logged"] == 2
        assert report.legacy["surfaced_count"] == 7

    def test_malformed_line_skipped(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        log.write_text(
            "this is not json\n"
            '{"truncated":\n'
            "\n"
            "[1, 2, 3]\n"
            '"just a string"\n'
        )
        _v12(log, x_outcome="hit", x_k_returned=1)
        report = aggregate_events(log)
        assert report.fired_count == 1

    def test_non_auto_recall_malformed_line_is_skipped(self, tmp_path: Path):
        """The log is tens of MB and only a slice of it is AutoRecall, so
        the reader rejects a line on a substring probe before paying for
        `json.loads`. The probe must not change what the reader accepts:
        a truncated non-AutoRecall line is still skipped silently, and a
        record whose `event` merely *mentions* AutoRecall is still
        rejected on the parsed value rather than on the raw bytes."""
        from recall.stats import iter_auto_recall_records
        log = tmp_path / "events.log.jsonl"
        log.write_text(
            # truncated PostToolUse write — never parses, never matters
            '{"schema_version": "1.2", "event": "PostToolUse", "x_tool": "Ba\n'
            # well-formed, but a different event that names ours in a field
            '{"schema_version": "1.2", "event": "SessionStart",'
            ' "note": "next up: AutoRecall"}\n'
            # compact separators, as a hand-rolled writer emits them
            '{"schema_version":"1.2","event":"AutoRecall","ts_ms":1,'
            '"session_id":"s","turn":0,"x_outcome":"miss","x_path":"daemon"}\n'
        )
        _v12(log, x_outcome="hit", x_k_returned=1)

        records = list(iter_auto_recall_records(log))
        assert [r.get("x_outcome") for r in records] == ["miss", "hit"]

    def test_rotated_siblings_are_read(self, tmp_path: Path):
        """logrotate leaves `events.log.<date>.jsonl` next to the live
        file. A 7d window that ignored them would silently report a
        fraction of the real traffic — and must not double-count the
        live file either. A sibling that merely shares the "events.log"
        stem prefix (`events.log-foo.jsonl`) must not be swept in by a
        loose `<stem>*<suffix>` glob."""
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        rotated = tmp_path / "events.log.2026-09-01.jsonl"
        unrelated = tmp_path / "other.jsonl"
        same_prefix = tmp_path / "events.log-foo.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1)
        _v12(rotated, x_outcome="hit", x_k_returned=1)
        _v12(rotated, x_outcome="hit", x_k_returned=1)
        for _ in range(5):
            _v12(unrelated, x_outcome="hit", x_k_returned=1)
        for _ in range(5):
            _v12(same_prefix, x_outcome="hit", x_k_returned=1)

        report = aggregate_events(log)
        assert report.fired_count == 3

    def test_log_files_anchors_rolled_shape(self, tmp_path: Path):
        """`_log_files` must require the exact `<stem>.<date>[.n]<suffix>`
        shape: `events.log-foo.jsonl` shares the stem prefix but is not a
        roll and must be excluded; `events.log.2026-09-04.1.jsonl` (the
        same-day counter suffix) IS a roll and must be included."""
        from recall.stats import _log_files
        log = tmp_path / "events.log.jsonl"
        log.write_text("{}\n")
        counter_roll = tmp_path / "events.log.2026-09-04.1.jsonl"
        counter_roll.write_text("{}\n")
        (tmp_path / "events.log-foo.jsonl").write_text("{}\n")

        names = {p.name for p in _log_files(log)}
        assert names == {"events.log.jsonl", "events.log.2026-09-04.1.jsonl"}

    def test_iter_yields_only_auto_recall_records_as_dicts(self, tmp_path: Path):
        from recall.stats import iter_auto_recall_records
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=1)
        _v12(log, event="UserPromptSubmit")
        _v12(log, event="PostToolUse")

        records = list(iter_auto_recall_records(log))
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, dict)
        # extensions are flattened to the top level, as the hook writes them
        assert rec["x_outcome"] == "hit"
        assert rec["schema_version"] == "1.2"

    def test_is_v12_predicate(self):
        from recall.stats import is_v12
        assert is_v12({"schema_version": "1.2", "x_outcome": "hit",
                       "x_paths": ["memory/a.md"]}) is True
        assert is_v12({"schema_version": "1.2", "x_outcome": "miss"}) is True
        assert is_v12({"schema_version": "1.2", "x_outcome": "hit"}) is False
        assert is_v12({"schema_version": "1.1", "x_outcome": "hit",
                       "x_paths": ["memory/a.md"]}) is False
        assert is_v12({"x_outcome": "hit"}) is False


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


# ---------------------------------------------------------------------------
# Human renderer
# ---------------------------------------------------------------------------


def _ts_2026_08_21() -> int:
    return int(datetime.datetime(
        2026, 8, 21, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _legacy_block(**overrides) -> dict:
    block = {
        "events": 1204,
        "hit_logged": 900,
        "phantom_hits": 486,
        "real_hits": 414,
        "skip": 200,
        "timeout": 104,
        "unavailable": 0,
        "error": 0,
        "query_p50_ms": 620,
        "query_p95_ms": 2900,
        "surfaced_count": 1500,
        "top_sources": [("imports", 700), ("brain", 300)],
    }
    block.update(overrides)
    return block


def _sample_report(**overrides):
    """The worked example from the stats plan, field for field. The
    rendered output for this report is pinned line by line below."""
    from recall.stats import StatsReport
    fields = dict(
        fired_count=97,
        skipped_count=32,
        skip_reasons={"too_short": 20, "slash": 12},
        miss_count=61,
        dedup_count=14,
        other_outcomes={"timeout": 5, "unavailable": 2, "error": 1},
        total_fires=180,
        total_prompts=212,
        coverage_pct=100 * 97 / 180,
        miss_pct=100 * 61 / 180,
        dedup_pct=100 * 14 / 180,
        timeout_pct=100 * 5 / 180,
        path_split={"daemon": 171, "inproc": 9},
        daemon_error_count=9,
        daemon_error_by_reason={"no_socket": 6, "timeout": 3},
        degraded_count=0,
        index_stale_count=3,
        index_stale_known=160,
        latency_p50_ms=240,
        latency_p95_ms=810,
        query_p50_ms=95,
        query_p95_ms=400,
        surfaced_count=301,
        k_candidates_total=900,
        k_gated_out_total=540,
        k_dedup_total=59,
        repeat_injection_rate=0.0,
        top_sources=[("imports", 160), ("brain", 141)],
        top_paths=[("memory/semantic/lessons/foo.md", 12),
                   ("imports/claude/plans/bar.md", 9)],
        score_distribution={"0.85+": 40, "0.70-0.85": 200, "0.50-0.70": 61},
        rerank_distribution={"<0.1": 12, "0.1-0.3": 30,
                             "0.3-0.5": 110, "0.5-0.7": 149},
        window_start_ts_ms=_ts_2026_08_21(),
        window_end_ts_ms=_ts_2026_08_21() + 86_400_000,
    )
    fields.update(overrides)
    return StatsReport(**fields)


class TestRenderHuman:
    """The pretty-printer is what the user sees. v1.2 replaced the old
    ROI paragraph — which multiplied a phantom-inflated fire count by a
    doc count nobody had opened — with counters that state what actually
    happened. Pin the new lines so the invented framing can't come back.
    """

    def test_render_headline_counts(self):
        from recall.stats import render_human
        out = render_human(_sample_report())
        assert "brainstack: auto-recall (since 2026-08-21)" in out
        assert "  Prompts:      212 (180 fires, 32 skipped: 20 too_short, 12 slash)" in out
        assert "  Injected:     97 / 180 fires (54%) carried at least one doc" in out

    def test_render_honest_lines_no_roi(self):
        from recall.stats import render_human
        out = render_human(_sample_report())
        # The removed copy — it claimed value the telemetry cannot support
        assert "Without auto-recall" not in out
        assert "without auto-recall" not in out.lower()
        assert "would have started with only" not in out
        assert "ROI" not in out
        # Every line of the worked example, verbatim
        assert "  Injected:     97 / 180 fires (54%) carried at least one doc" in out
        assert "  Miss:         61 (34%) nothing passed the relevance gate" in out
        assert "  Dedup:        14 (8%) every passing doc was already shown this session" in out
        assert "  Timeout:      5 (3%) · unavailable 2 · error 1" in out
        assert ("  Path:         daemon 171, inproc 9"
                " (daemon_error 9 (no_socket 6, timeout 3), degraded 0)"
                " · index stale 3 / 160 fires that reported staleness") in out
        assert "  Latency:      worker p50 240ms, p95 810ms · query p50 95ms, p95 400ms" in out
        assert ("  Docs:         301 injected (avg 3.1 per hit) · repeat-injection 0.0%"
                " · candidates 900, gated out 540, dedup 59") in out
        assert "  Sources:      imports (160), brain (141)" in out
        assert "  RRF scores:   40 in 0.85+, 200 in 0.70-0.85, 61 in 0.50-0.70" in out
        assert "  Rerank:       12 in <0.1, 30 in 0.1-0.3, 110 in 0.3-0.5, 149 in 0.5-0.7" in out
        assert ("  Top docs:     memory/semantic/lessons/foo.md (12),"
                " imports/claude/plans/bar.md (9)") in out
        # The honest caveat that replaces the ROI claim
        assert "These count docs injected, not docs used" in out
        assert "recall stats --utilization" in out

    def test_render_path_line_reports_index_staleness(self):
        """The staleness segment reads "N / M fires that reported staleness"
        where M is `index_stale_known`, not the daemon count — the two
        differ whenever a daemon call timed out before reporting, and
        "daemon fires" as the denominator label reads as an arithmetic
        error against the "daemon 171" segment earlier on the same line.
        With nothing known the segment is omitted; printing "0 / 0" would
        read as "the index is fresh" when the truth is "nobody checked"."""
        from recall.stats import render_human
        out = render_human(_sample_report())
        assert ("  Path:         daemon 171, inproc 9"
                " (daemon_error 9 (no_socket 6, timeout 3), degraded 0)"
                " · index stale 3 / 160 fires that reported staleness") in out

        unknown = render_human(_sample_report(index_stale_count=0,
                                              index_stale_known=0))
        assert ("  Path:         daemon 171, inproc 9"
                " (daemon_error 9 (no_socket 6, timeout 3), degraded 0)") in unknown
        assert "index stale" not in unknown

    def test_render_path_line_breaks_daemon_errors_out_by_reason(self):
        """"daemon_error 9" alone reads the same whether the daemon was
        never installed or was merely slow, and those need opposite
        fixes. The breakdown is ordered by count so the dominant reason
        reads first, ties alphabetically so the line is stable between
        runs, and is omitted entirely when there were no errors (an
        empty "()" would look like a truncated report)."""
        from recall.stats import render_human
        out = render_human(_sample_report(
            daemon_error_count=6,
            daemon_error_by_reason={"timeout": 2, "no_socket": 2,
                                    "connection_refused": 2},
        ))
        assert ("daemon_error 6 (connection_refused 2, no_socket 2, timeout 2)"
                in out)

        clean = render_human(_sample_report(daemon_error_count=0,
                                            daemon_error_by_reason={}))
        assert "(daemon_error 0, degraded 0)" in clean

    def test_render_repeat_injection_rate_is_a_percentage(self):
        """`repeat_injection_rate` is a fraction on the report; the human
        view shows it as a percentage."""
        from recall.stats import render_human
        out = render_human(_sample_report(repeat_injection_rate=0.5))
        assert "repeat-injection 50.0%" in out

    def test_render_counts_diagnostic_outcomes(self):
        """When the window contains only timeouts/errors/unavailable
        (no hits, no skips), `render_human` must NOT say "no events" —
        it should report the diagnostic count and roll those into the
        prompt-total denominator. Codex 2026-05-05 P2."""
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            fired_count=0,
            skipped_count=0,
            other_outcomes={"timeout": 5, "unavailable": 2},
            total_fires=7,
            total_prompts=7,
        )
        out = render_human(report)
        # Must NOT be the "no events" message
        assert "no auto-recall events" not in out.lower()
        # Diagnostic count surfaces, and retrieval was attempted 7 times
        assert "Timeout:      5" in out
        assert "Prompts:      7" in out

    def test_render_zero_count_message(self):
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

    def test_render_legacy_block_separate(self):
        """Legacy events get their own block with their own labels. They
        must never be folded into the 1.2 counters above."""
        from recall.stats import render_human
        out = render_human(_sample_report(legacy=_legacy_block()))
        assert "  Legacy (pre-1.2 semantics, not comparable): 1204 events" in out
        assert ("    logged hit 900 (phantom 486 = hit with 0 docs, real 414)"
                " · skip 200 · timeout 104") in out
        assert ("    query-only latency p50 620ms, p95 2900ms"
                " · sources imports (700), brain (300)") in out
        # 1.2 numbers unchanged by the presence of legacy rows
        assert "  Injected:     97 / 180 fires (54%) carried at least one doc" in out
        assert "  Prompts:      212 (180 fires, 32 skipped: 20 too_short, 12 slash)" in out

    def test_render_omits_legacy_block_when_empty(self):
        from recall.stats import render_human
        out = render_human(_sample_report(legacy=_legacy_block(events=0)))
        assert "Legacy" not in out

    def test_render_only_legacy_says_no_v12_events(self):
        """A user who hasn't upgraded their hook has 100% legacy events.
        Rendering zeros would read as "auto-recall did nothing"; the
        honest answer names the cause and the fix."""
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            legacy=_legacy_block(),
            window_start_ts_ms=_ts_2026_08_21(),
        )
        out = render_human(report)
        assert "no auto-recall events" not in out.lower()
        assert ("  No schema-1.2 events in this window — hooks predate v1.2"
                " (./install.sh --upgrade).") in out
        assert "  Legacy (pre-1.2 semantics, not comparable): 1204 events" in out


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

    def test_aggregate_events_with_v12_events_and_no_transcripts(self, tmp_path: Path):
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=2, x_sources={"brain": 2})
        report = aggregate_events(log)
        assert report.fired_count == 1
        assert report.mcp_calls == {}
        assert report.tool_calls_other == {}


class TestRenderCrossSourceSection:
    """`render_human()` must include the "Model-driven tool calls" section
    when the report has tool-call data populated. Empty fields → omit the
    section (don't print empty headers)."""

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

    def test_all_zero_tool_call_dicts_do_not_suppress_no_events(self):
        """A programmatically constructed report with all-zero counts
        in mcp_calls / tool_calls_other (rather than empty dicts) must
        NOT be treated as 'we have data' — otherwise the no-events
        message gets suppressed when nothing real happened.
        Codex 2026-05-06 review of routing-coverage removal."""
        from recall.stats import StatsReport, render_human
        report = StatsReport(
            mcp_calls={"mcp__minerva__*": 0},
            tool_calls_other={"Bash": 0},
        )
        out = render_human(report)
        # All-zero counts = no data → bail to the no-events message
        assert "no auto-recall events" in out.lower()


class TestStatsJsonContract:
    """Regression tests for `recall stats --json` output shape. These
    pin the schema after the routing-coverage removal so a future change
    that re-introduces the field (or removes another) gets caught."""

    def test_routing_coverage_key_absent(self):
        from dataclasses import asdict
        from recall.stats import StatsReport
        data = asdict(StatsReport())
        assert "routing_coverage" not in data, (
            "routing_coverage was removed from the schema in 2026-05-06; "
            "re-introducing it would silently change the --json contract"
        )

    def test_expected_keys_present(self):
        """The fields downstream consumers actually depend on."""
        from dataclasses import asdict
        from recall.stats import StatsReport
        data = asdict(StatsReport())
        for required in [
            # pre-1.2 keys, kept with 1.2-only semantics
            "fired_count", "skipped_count", "skip_reasons",
            "latency_p50_ms", "latency_p95_ms", "surfaced_count",
            "top_sources", "top_paths", "score_distribution",
            "window_start_ts_ms", "window_end_ts_ms",
            "other_outcomes", "mcp_calls", "tool_calls_other",
            # v1.2 additions
            "miss_count", "dedup_count",
            "total_fires", "total_prompts",
            "coverage_pct", "miss_pct", "dedup_pct", "timeout_pct",
            "path_split", "daemon_error_count", "daemon_error_by_reason",
            "degraded_count",
            "index_stale_count", "index_stale_known",
            "query_p50_ms", "query_p95_ms",
            "k_candidates_total", "k_gated_out_total", "k_dedup_total",
            "repeat_injection_rate", "rerank_distribution",
            "legacy",
        ]:
            assert required in data, f"missing schema field: {required}"

    def test_legacy_is_nested_dict(self):
        """`legacy` is a nested object, never flattened into the
        top-level counters — a consumer must not be able to read a
        phantom-inflated hit count by accident."""
        from dataclasses import asdict
        from recall.stats import StatsReport
        data = asdict(StatsReport())
        assert isinstance(data["legacy"], dict)
        assert int(data["legacy"].get("events", 0)) == 0
        assert "phantom_hits" not in data

    def test_json_serializable_after_aggregate(self, tmp_path: Path):
        """`recall stats --json` dumps `asdict(report)` after converting
        tuple lists; the rest must already be JSON-native."""
        from dataclasses import asdict
        from recall.stats import aggregate_events
        log = tmp_path / "events.log.jsonl"
        _v12(log, x_outcome="hit", x_k_returned=2, x_sources={"brain": 2},
             x_top_scores=[0.9], x_rerank_scores=[0.6])
        _v11(log, x_outcome="hit", x_k_returned=0, x_sources={"imports": 1})

        data = asdict(aggregate_events(log))
        data["top_sources"] = [list(t) for t in data["top_sources"]]
        data["top_paths"] = [list(t) for t in data["top_paths"]]
        data["legacy"]["top_sources"] = [list(t) for t in data["legacy"]["top_sources"]]
        text = json.dumps(data)
        assert json.loads(text)["legacy"]["phantom_hits"] == 1
