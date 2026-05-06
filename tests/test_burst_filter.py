"""Tests for the burst-cluster detector.

The dream pipeline's clusterer can generate noise candidates from a
single session burst — many events, narrow time window, one (skill,
result) bucket. The recurring 6,768-event "FAILURE / claude-code"
candidate from 2026-04-27 is the canonical example: rejected three
times by the user, re-staged on every dream cycle because cluster ids
regenerate.

`cluster._is_burst_cluster()` is the upstream gate: when ALL three
thresholds trip simultaneously (count > 500 AND window < 30min AND
single bucket), the cluster is dropped before pattern extraction. The
candidate never reaches `write_candidates`, so there is no candidate-id
to reject and no rejection-history loophole.

`promote.cluster_and_extract()` integrates the detector and exposes
per-skip telemetry via the `telemetry=` kwarg, kept backward-compatible
so existing callers see no behavior change.

Both layers are tested here; cluster.py-level tests are pure (no env, no
disk), promote.py-level tests assert end-to-end behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Autouse fixture: scrub every DREAM_BURST_* env var before each test so a
# stale shell env can't change the threshold mid-suite. Codex 2026-05-06.
@pytest.fixture(autouse=True)
def _clear_dream_burst_env(monkeypatch):
    for var in [
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
# Helpers — synthetic episode builders matching the real schema
# ---------------------------------------------------------------------------

def _ts(minutes: int = 0, seconds: float = 0.0) -> str:
    """ISO-8601 timestamp offset from a fixed anchor.

    Anchor is 2026-04-27T03:28:32+00:00 — the actual start of the burst
    in the user's brain, kept consistent so test data and field-graded
    examples line up.
    """
    base_seconds = (3 * 3600) + (28 * 60) + 32  # 03:28:32 of the day
    total = base_seconds + (minutes * 60) + seconds
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"2026-04-27T{h:02d}:{m:02d}:{s:09.6f}+00:00"


def _ep(
    *,
    timestamp: str | None = "default",
    skill: str = "claude-code",
    result: str = "failure",
    text: str = "gh pr create failed",
) -> dict:
    """Build a minimal episode dict with the fields _is_burst_cluster reads."""
    e = {
        "skill": skill,
        "result": result,
        "action": text,
        "reflection": text,
        "detail": text,
        "pain_score": 5,
        "importance": 5,
    }
    # `timestamp="default"` means use a fixed value; pass `None` to omit;
    # pass any string to set an explicit value.
    if timestamp == "default":
        e["timestamp"] = _ts(0, 0)
    elif timestamp is not None:
        e["timestamp"] = timestamp
    return e


def _burst(n: int = 600, window_min: float = 5.0,
           skill: str = "claude-code", result: str = "failure") -> list[dict]:
    """Construct n episodes evenly spaced across `window_min` minutes,
    all from one (skill, result) bucket. Default values trip the burst
    detector at production thresholds (>500, <30min, single bucket)."""
    if n < 1:
        return []
    span_seconds = window_min * 60.0
    step = span_seconds / max(n - 1, 1)
    return [
        _ep(timestamp=_ts(0, i * step), skill=skill, result=result)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Unit tests for _is_burst_cluster — pure function, no env, no disk
# ---------------------------------------------------------------------------

class TestIsBurstClusterCore:
    """Core threshold logic. Defaults: count>500, window<30min, single bucket."""

    def test_below_evidence_threshold_is_not_burst(self):
        """100 events in a tight window from one bucket is below the
        500-event threshold and must NOT be flagged as a burst.
        Routine successful work (a focused 100-event session) must
        survive the detector even if the other two signals trip."""
        c = _burst(n=100, window_min=5.0)
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False
        assert reason == ""

    def test_above_threshold_narrow_window_single_bucket_is_burst(self):
        """The canonical burst signature: 6,768-equivalent events, one
        bucket, sub-30min window. The reason string must include the
        count, window seconds, and bucket so logs are grep-able."""
        c = _burst(n=600, window_min=5.0,
                   skill="claude-code", result="failure")
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True
        assert "burst" in reason
        assert "n=600" in reason
        assert "claude-code" in reason
        assert "failure" in reason

    def test_above_threshold_wide_window_is_not_burst(self):
        """700 events spread across 45 minutes (>30min default) is dense
        but not bursty. Sustained productive work, not a retry storm.
        Crucial false-positive prevention case."""
        c = _burst(n=700, window_min=45.0)
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False
        assert reason == ""

    def test_multi_bucket_via_result_does_not_trip_strict(self):
        """600 events in a tight window split across two buckets via
        `result` (claude-code/failure + claude-code/success) is mixed,
        not a pure failure storm. With the default
        `require_single_bucket=True` it must NOT be flagged."""
        c = _burst(n=300, window_min=5.0,
                   skill="claude-code", result="failure")
        c.extend(_burst(n=300, window_min=5.0,
                        skill="claude-code", result="success"))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False, (
            f"strict single-bucket should reject mixed-result clusters, "
            f"got reason={reason!r}"
        )

    def test_multi_bucket_via_skill_does_not_trip_strict(self):
        """Same shape but the bucket varies on `skill` instead of
        `result`. Bucket key is `(skill, result)` so a detector that
        only inspected one of the two would fail this case. Independent
        coverage is critical — the default test only varies result."""
        c = _burst(n=300, window_min=5.0,
                   skill="claude-code", result="failure")
        c.extend(_burst(n=300, window_min=5.0,
                        skill="codex", result="failure"))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False, (
            f"strict single-bucket should reject mixed-skill clusters, "
            f"got reason={reason!r}"
        )

    def test_multi_bucket_with_relaxed_flag_flags_as_multi_bucket_burst(self):
        """When the operator deliberately relaxes `require_single_bucket=False`,
        the same mixed-result cluster IS flagged but with a distinguished
        reason ('burst_multi_bucket') so logs differentiate the strict
        path from the relaxed one."""
        c = _burst(n=300, window_min=5.0, result="failure")
        c.extend(_burst(n=300, window_min=5.0, result="success"))
        is_burst, reason = cluster._is_burst_cluster(
            c, require_single_bucket=False,
        )
        assert is_burst is True
        assert "burst_multi_bucket" in reason
        assert "bucket_count=2" in reason


class TestIsBurstClusterChronicPath:
    """Path B — chronic single-bucket dominance, no window constraint.

    Field-graded against the real `1de077bc2298` candidate: 6,768 events
    spanning 9.5 days, all `claude-code/failure`, with the bulk
    distributed across 5 days (~700–1,600 events/day). Path A's
    window check (< 30 min) misses it because the cluster's tail has
    grown over time. Path B catches it on count alone.
    """

    def test_chronic_single_bucket_above_threshold_trips(self):
        """7,000 events all from one bucket spread across many days
        (well outside any tight burst window) must still trip the
        detector via Path B."""
        # Spread across 9 days — explicitly NOT a tight burst
        c = []
        for i in range(2500):  # > 2000 chronic threshold default
            day = 27 + (i // 280)  # ~280 events/day for 9 days
            sec = (i % 280) * (24 * 3600 // 280)
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            c.append(_ep(
                timestamp=f"2026-04-{day:02d}T{h:02d}:{m:02d}:{s:02d}+00:00",
                skill="claude-code", result="failure",
            ))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True
        assert "chronic_noise" in reason
        assert "n=2500" in reason
        assert "claude-code" in reason
        assert "failure" in reason

    def test_chronic_below_threshold_does_not_trip(self):
        """1,500 events spread over many days — well above the time-burst
        evidence count (500) but below the chronic threshold (2000), and
        the time window is too wide for Path A. Must NOT trip — this
        represents a genuinely high-volume but not pathologically dominant
        single-bucket cluster."""
        # 1500 events spread across 9 days — single bucket
        c = []
        for i in range(1500):
            day = 27 + (i // 167)
            sec = (i % 167) * (24 * 3600 // 167)
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            c.append(_ep(
                timestamp=f"2026-04-{day:02d}T{h:02d}:{m:02d}:{s:02d}+00:00",
                skill="claude-code", result="failure",
            ))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False, (
            f"1500-event cluster should NOT trip with chronic_threshold=2000, "
            f"got reason={reason!r}"
        )

    def test_chronic_multi_bucket_does_not_trip_strict(self):
        """Even with 5,000 events, if they span MULTIPLE buckets and
        require_single_bucket=True (default), Path B does NOT fire.
        Mixed buckets are signal that the cluster has internal variety."""
        c = _burst(n=2500, window_min=60.0, result="failure")
        c.extend(_burst(n=2500, window_min=60.0, result="success"))
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is False

    def test_chronic_multi_bucket_with_relaxed_flag_trips(self):
        """Same 5,000-event mixed-bucket cluster, relaxed bucket check —
        Path B trips with the chronic_multi_bucket reason."""
        c = _burst(n=2500, window_min=60.0, result="failure")
        c.extend(_burst(n=2500, window_min=60.0, result="success"))
        is_burst, reason = cluster._is_burst_cluster(
            c, require_single_bucket=False,
        )
        assert is_burst is True
        assert "chronic_multi_bucket" in reason
        assert "n=5000" in reason
        assert "bucket_count=2" in reason

    def test_chronic_threshold_tunable_via_kwarg(self):
        """Operator can lower the chronic threshold via kwarg. With
        chronic_evidence_count=600, a 700-event single-bucket cluster
        spread over many days trips."""
        c = []
        for i in range(700):
            day = 27 + (i // 80)
            c.append(_ep(
                timestamp=f"2026-04-{day:02d}T12:00:00+00:00",
                skill="claude-code", result="failure",
            ))
        is_burst_default, _ = cluster._is_burst_cluster(c)
        # Default chronic=2000 — does NOT trip
        assert is_burst_default is False
        # Lowered chronic=600 — DOES trip
        is_burst_strict, reason = cluster._is_burst_cluster(
            c, chronic_evidence_count=600,
        )
        assert is_burst_strict is True
        assert "chronic_noise" in reason


class TestIsBurstClusterDominance:
    """The real-world bug. A cluster where 99% of events are from one
    bucket and 1% from another shouldn't get a free pass on the strict-
    single-bucket check. Field-graded: the actual brain had a 7,097-
    event cluster of which 7,021 were `claude-code/success` and 76 were
    `claude-code/failure`. Bucket count = 2, but it's effectively one.
    """

    def test_dominant_bucket_above_threshold_trips_chronic(self):
        """7,000 events: 6,930 (99%) from one bucket, 70 (1%) from
        another. With default `min_dominant_bucket_fraction=0.95`, the
        cluster is treated as effectively single-bucket and trips
        Path B (chronic). Reason includes the dominance percentage."""
        c = []
        for i in range(6930):
            c.append(_ep(skill="claude-code", result="success",
                         timestamp=f"2026-04-{27 + i // 770:02d}T12:00:00+00:00"))
        for i in range(70):
            c.append(_ep(skill="claude-code", result="failure",
                         timestamp=f"2026-04-{27 + i // 8:02d}T12:00:00+00:00"))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True
        assert "chronic_dominant" in reason
        assert "frac=0.99" in reason
        # Surface the dominant bucket, not the minority one
        assert "claude-code/success" in reason

    def test_balanced_two_bucket_cluster_does_not_trip(self):
        """Critical false-positive guard. A cluster with 60/40 split is
        NOT dominated; should NOT be treated as single-bucket."""
        c = []
        for i in range(4200):
            c.append(_ep(skill="claude-code", result="success",
                         timestamp=f"2026-04-27T12:{i % 60:02d}:00+00:00"))
        for i in range(2800):
            c.append(_ep(skill="claude-code", result="failure",
                         timestamp=f"2026-04-27T12:{i % 60:02d}:00+00:00"))
        is_burst, reason = cluster._is_burst_cluster(c)
        # 60/40 → dominant fraction ≈ 0.60, well below 0.95 default
        assert is_burst is False, (
            f"60/40-split cluster should not trip dominant check, "
            f"got reason={reason!r}"
        )

    def test_dominance_threshold_tunable(self):
        """Operator can lower the dominance threshold via kwarg. With
        `min_dominant_bucket_fraction=0.6`, a 60/40 split DOES trip."""
        c = []
        for _ in range(4200):
            c.append(_ep(skill="claude-code", result="success",
                         timestamp="2026-04-27T12:00:00+00:00"))
        for _ in range(2800):
            c.append(_ep(skill="claude-code", result="failure",
                         timestamp="2026-04-27T12:00:00+00:00"))
        is_burst, _ = cluster._is_burst_cluster(
            c, min_dominant_bucket_fraction=0.6,
        )
        assert is_burst is True

    def test_single_bucket_does_not_emit_dominance_suffix(self):
        """A pure single-bucket cluster emits the simple reason form
        ('chronic_noise', 'burst') — NOT the dominant variants. Keeps
        log noise low when there's no actual minority to surface."""
        c = _burst(n=2500, window_min=60.0)
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True
        assert "chronic_noise" in reason
        assert "chronic_dominant" not in reason
        assert "frac=" not in reason


class TestIsBurstClusterEdgeCases:

    def test_all_missing_timestamps_returns_not_burst(self):
        """Cannot prove burst when no timestamps are present — window is
        undefined. Must NOT flag (defensive default — false negative is
        cheaper than false positive in this gate). Otherwise corrupt
        episode data could nuke legitimate clusters."""
        c = [_ep(timestamp=None) for _ in range(600)]
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False
        assert reason == ""

    def test_single_valid_timestamp_among_many_returns_not_burst(self):
        """Need at least 2 timestamps to compute a window. One valid +
        599 missing → window undefined → not burst. Same defensive
        principle as all-missing."""
        c = [_ep(timestamp=None) for _ in range(599)]
        c.append(_ep(timestamp=_ts(0, 0)))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False
        assert reason == ""

    def test_one_missing_timestamp_does_not_break_otherwise_valid_burst(self):
        """One bad apple shouldn't spoil the bunch. 599 valid timestamps
        in a tight window plus 1 missing → still flagged as burst. The
        full count (600) trips the evidence threshold; the 599 valid
        timestamps span a window < 30min."""
        c = _burst(n=599, window_min=5.0)
        c.append(_ep(timestamp=None))  # corrupt member
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True
        assert "n=600" in reason

    def test_naive_timestamp_treated_as_utc(self):
        """If episode timestamps lack timezone info, treat them as UTC.
        Matches how `_age_factor` and `_count_recent_failures` handle
        this in the rest of brainstack. Without this, naive timestamps
        would either crash or compare-as-naive (ambiguous).

        Test design: build a cluster of 600 entirely-naive timestamps in
        a 5-minute window. If naive timestamps are silently ignored,
        only the (single) aware tail-padding event remains and the
        window collapses → not burst. If naive are treated as UTC,
        the cluster trips. Asserts the naive→UTC fallback path is hit."""
        c = []
        for i in range(600):
            secs = (i / 599) * 300  # 5 min span
            m, s = divmod(secs, 60)
            # ISO-8601 with NO trailing tz — naive
            c.append(_ep(timestamp=f"2026-04-27T03:28:{int(m):02d}.{int(s*1000):06d}"))
        # No aware-timestamp padding here — if naive are dropped, this
        # cluster has 0 valid timestamps → not burst → test fails.
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is True

    def test_malformed_timestamps_dropped_not_crashed(self):
        """Random garbage in the timestamp field shouldn't crash the
        detector. Treat malformed entries the same as missing
        timestamps: exclude from window calc, include in count.
        With 599 valid + 1 malformed, the cluster still trips."""
        c = _burst(n=599, window_min=5.0)
        c.append(_ep(timestamp="not-a-date-at-all"))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True
        assert "n=600" in reason

    def test_burst_spanning_midnight_utc_handled_correctly(self):
        """The window is `max(ts) - min(ts)`, so an 03:55 → 04:25
        burst computes the same as one that spans 23:55 → 00:24:59.
        No special case needed — pin it so future refactors don't break it.
        Span is 29m59s (strictly < 30min) so the < threshold trips."""
        # 600 events spanning UTC midnight (23:55:00 → 00:24:59 next day)
        c = []
        for i in range(600):
            # span = 29m59s = 1799s. Strict < 1800 trips the window check.
            offset_seconds = (i / 599) * 1799.0
            total = 23 * 3600 + 55 * 60 + offset_seconds
            day = "2026-04-27" if total < 86400 else "2026-04-28"
            t = total % 86400
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = t % 60
            c.append(_ep(timestamp=f"{day}T{h:02d}:{m:02d}:{s:09.6f}+00:00"))
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is True

    def test_exactly_at_threshold_is_not_burst(self):
        """Boundary case: count == 500 (not strictly greater) and window
        == 1800s (not strictly less). Neither AND condition trips when
        equal — both use strict comparators per the spec."""
        c = _burst(n=500, window_min=30.0)
        is_burst, reason = cluster._is_burst_cluster(c)
        assert is_burst is False, (
            f"at-threshold values must use strict (>, <) comparators "
            f"to avoid surprising the operator at boundaries, got "
            f"reason={reason!r}"
        )

    def test_count_at_500_window_inside_is_not_burst(self):
        """Off-by-one isolation: only the count is at threshold; window
        and bucket would otherwise trip. Asserts `count > 500` is strict
        (a `count >= 500` bug would flag this)."""
        c = _burst(n=500, window_min=5.0)
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is False

    def test_count_above_threshold_window_at_30min_is_not_burst(self):
        """Off-by-one isolation: only the window is at threshold; count
        is above. Asserts `window < 1800` is strict (a `window <= 1800`
        bug would flag this)."""
        c = _burst(n=501, window_min=30.0)
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is False

    def test_count_just_above_threshold_window_just_inside_is_burst(self):
        """Smallest legitimate burst: 501 events in 29m59s. If the
        comparators are strict `>` and `<`, this trips. If either is
        `>=` or `<=`, this also trips — but the two negative cases above
        will catch each kind of bug independently."""
        # 501 events spread across exactly 1799 seconds (29m59s).
        c = []
        for i in range(501):
            t = (i / 500) * 1799.0
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = t % 60
            c.append(_ep(
                timestamp=f"2026-04-27T03:{m:02d}:{s:09.6f}+00:00"
                if h == 0 else f"2026-04-27T{3 + h:02d}:{m:02d}:{s:09.6f}+00:00",
            ))
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is True

    def test_empty_cluster_returns_not_burst(self):
        """Defensive: an empty cluster cannot be a burst. Don't crash on
        empty input — return (False, '') quietly."""
        is_burst, reason = cluster._is_burst_cluster([])
        assert is_burst is False
        assert reason == ""

    def test_single_event_cluster_returns_not_burst(self):
        """Single-event cluster: zero window, single bucket, but well
        under the count threshold. Must NOT be flagged."""
        c = [_ep(timestamp=_ts(0, 0))]
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is False


class TestIsBurstClusterTunables:

    def test_tightening_evidence_threshold_flags_smaller_clusters(self):
        """An operator who lowers `max_evidence_count` to 50 should see
        a 100-event burst flagged that the default 500 would have passed.
        Confirms the kwarg actually matters."""
        c = _burst(n=100, window_min=5.0)
        # default → not burst
        is_burst_default, _ = cluster._is_burst_cluster(c)
        assert is_burst_default is False
        # tightened → burst
        is_burst_strict, reason = cluster._is_burst_cluster(
            c, max_evidence_count=50,
        )
        assert is_burst_strict is True
        assert "n=100" in reason

    def test_narrowing_window_threshold_skips_more_clusters(self):
        """When the operator believes legitimate work never spans more
        than 5 minutes (rare but possible), they should be able to
        narrow the window threshold so only the tightest bursts trip."""
        c = _burst(n=600, window_min=10.0)
        # default 30min window — 10min < 30min trips → burst
        is_burst, _ = cluster._is_burst_cluster(c)
        assert is_burst is True
        # narrow window threshold to 5min — 10min no longer < 5min
        is_burst_narrow, _ = cluster._is_burst_cluster(
            c, max_window_seconds=300,
        )
        assert is_burst_narrow is False


# ---------------------------------------------------------------------------
# Integration tests — cluster_and_extract filters bursts
# ---------------------------------------------------------------------------

class TestClusterAndExtractFiltersBursts:
    """End-to-end: a burst-shaped event list goes in, an empty patterns
    dict comes out (and the per-skip telemetry list is populated)."""

    def test_burst_yields_empty_patterns_dict(self):
        """The user's bug, distilled: 600 near-duplicate events from a
        single session must NOT produce a candidate. Before this fix,
        cluster_and_extract returned a single pattern; now it must
        return {}. (Env autouse fixture clears DREAM_BURST_* vars.)"""
        entries = _burst(n=600, window_min=5.0)
        result = promote.cluster_and_extract(entries)
        assert result == {}, (
            f"burst events must produce no candidates, got {list(result)!r}"
        )

    def test_burst_skip_appears_in_telemetry_list(self):
        """When the caller passes `telemetry=[]`, every skipped burst
        must append a structured entry. Lets `run_dream_cycle` count
        bursts and emit a `burst_skipped=N` log line without parsing
        stdout."""
        entries = _burst(n=600, window_min=5.0)
        telemetry: list = []
        promote.cluster_and_extract(entries, telemetry=telemetry)
        assert len(telemetry) == 1
        entry = telemetry[0]
        assert "reason" in entry
        assert "cluster_size" in entry
        assert entry["cluster_size"] == 600
        assert "burst" in entry["reason"]

    def test_normal_cluster_passes_through_unchanged(self):
        """The detector must only fire on bursts. A normal small cluster
        must produce its pattern as before. Without this, the integration
        is over-eager and breaks legitimate dream cycles.

        Strong assertion: the produced pattern's `claim` reflects the
        normal cluster's text, NOT a burst signature. Catches the case
        where a refactor accidentally drops the normal cluster but
        coincidentally produces some other pattern."""
        normal_text = "fix the lockfile race condition"
        entries = [_ep(text=normal_text) for _ in range(5)]
        result = promote.cluster_and_extract(entries)
        assert len(result) == 1, (
            f"expected exactly 1 pattern from a single legitimate cluster, "
            f"got {list(result)!r}"
        )
        pattern = next(iter(result.values()))
        assert "lockfile" in pattern["claim"].lower(), (
            f"pattern claim should reflect the normal cluster's text, got "
            f"claim={pattern['claim']!r}"
        )

    def test_mixed_burst_and_normal_skips_only_burst(self):
        """The most realistic case: one burst + one legitimate cluster
        in the same dream cycle. The burst is filtered, the legitimate
        pattern survives. Asserts the filter is per-cluster, not global.

        Strong assertion: the surviving pattern's claim matches the
        normal cluster's text AND no surviving pattern claim contains
        the burst's signature. Catches the inverse failure mode where
        the burst slipped through and the normal cluster was dropped."""
        # 600 burst events — distinct text from normal cluster
        entries = _burst(n=600, window_min=5.0)  # text="gh pr create failed"
        # 5 unrelated normal events with deterministic timestamps spread
        # across multiple days so they cluster on text alone, not time.
        normal_text = "rebase merge conflict in tests"
        entries.extend([
            _ep(text=normal_text, timestamp=f"2026-04-29T1{i}:00:00+00:00")
            for i in range(5)
        ])
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        assert len(telemetry) == 1, (
            f"expected exactly 1 burst skip, got telemetry={telemetry!r}"
        )
        assert telemetry[0]["cluster_size"] == 600
        # At least one pattern survived AND it's the rebase-merge one.
        assert len(result) >= 1, (
            f"expected normal cluster to survive, got {list(result)!r}"
        )
        surviving_claims = [p["claim"].lower() for p in result.values()]
        assert any("rebase" in c or "merge" in c for c in surviving_claims), (
            f"normal cluster claim missing from surviving patterns: "
            f"{surviving_claims!r}"
        )
        # No surviving pattern reflects the burst (sanity guard).
        assert not any("gh pr create" in c for c in surviving_claims), (
            f"burst pattern leaked through: {surviving_claims!r}"
        )


class TestEnvVarOverrides:
    """Env vars match the brainstack convention (DREAM_BURST_* prefix).
    Read once at the top of cluster_and_extract and forwarded to
    _is_burst_cluster as kwargs. Tests pin the contract."""

    def test_disabled_kill_switch_passes_burst_through(self, monkeypatch):
        """Operator escape hatch: when `DREAM_BURST_DISABLED=1` is set,
        the detector is bypassed and bursts pass through to candidates.
        Useful for one-off forensic dream runs where you WANT the burst
        in the candidates view."""
        monkeypatch.setenv("DREAM_BURST_DISABLED", "1")
        entries = _burst(n=600, window_min=5.0)
        result = promote.cluster_and_extract(entries)
        assert len(result) >= 1, (
            "DREAM_BURST_DISABLED=1 must bypass the detector"
        )

    def test_lowered_evidence_threshold_via_env(self, monkeypatch):
        """Operator can tune the evidence threshold lower via env. With
        a 50-event threshold, a 100-event tight cluster trips."""
        monkeypatch.setenv("DREAM_BURST_MAX_EVIDENCE", "50")
        entries = _burst(n=100, window_min=5.0)
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        assert result == {}
        assert len(telemetry) == 1

    def test_lowered_window_threshold_via_env(self, monkeypatch):
        """`DREAM_BURST_MAX_WINDOW_SECONDS` independently tunable.
        Setting it to 60 makes a 1-min window trip even if window was
        previously >5min. Confirms the window env var is wired up."""
        monkeypatch.setenv("DREAM_BURST_MAX_WINDOW_SECONDS", "60")
        # 600 events in 5 min — default window threshold trips already,
        # so we need the WIDE counter-test: 600 events in 90s. Default
        # 1800s would trip, narrow 60s wouldn't, so we check 600 events
        # in 90s with default → trips, and 600 events in 90s after
        # narrowing → does NOT trip.
        entries = _burst(n=600, window_min=1.5)  # 90 sec window
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        # 90s > 60s narrowed threshold → no longer < threshold → not burst
        assert len(telemetry) == 0, (
            f"narrowed window threshold should let 90s windows through, "
            f"got {telemetry!r}"
        )
        assert len(result) >= 1

    def test_relaxed_single_bucket_via_env_flags_mixed(self, monkeypatch):
        """`DREAM_BURST_REQUIRE_SINGLE_BUCKET=0` flips the strict
        single-bucket check off. Now a mixed-bucket burst trips."""
        monkeypatch.setenv("DREAM_BURST_REQUIRE_SINGLE_BUCKET", "0")
        entries = _burst(n=300, window_min=5.0, result="failure")
        entries.extend(_burst(n=300, window_min=5.0, result="success"))
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        assert result == {}
        assert len(telemetry) == 1
        assert "burst_multi_bucket" in telemetry[0]["reason"]

    @pytest.mark.parametrize("falsy", ["0", "false", "False", "FALSE",
                                          "no", "No", "off", " 0 ", ""])
    def test_disabled_env_falsy_values_keep_detector_active(
        self, monkeypatch, falsy,
    ):
        """The kill switch is bool-typed: falsy values should NOT bypass
        the detector. Without this, `DREAM_BURST_DISABLED=0` and
        `DREAM_BURST_DISABLED=false` would both silently disable the
        detector — exactly opposite to operator intent.
        Codex 2026-05-06 BLOCKING."""
        monkeypatch.setenv("DREAM_BURST_DISABLED", falsy)
        entries = _burst(n=600, window_min=5.0)
        result = promote.cluster_and_extract(entries)
        # Detector active → burst dropped
        assert result == {}, (
            f"DREAM_BURST_DISABLED={falsy!r} should NOT disable the "
            f"detector (falsy values keep it on), got {list(result)!r}"
        )

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "TRUE",
                                          "yes", "Yes", "on", " 1 "])
    def test_disabled_env_truthy_values_bypass_detector(
        self, monkeypatch, truthy,
    ):
        """Truthy values disable the detector. Tests the same set of
        case/whitespace variants the bool parser claims to handle."""
        monkeypatch.setenv("DREAM_BURST_DISABLED", truthy)
        entries = _burst(n=600, window_min=5.0)
        result = promote.cluster_and_extract(entries)
        assert len(result) >= 1, (
            f"DREAM_BURST_DISABLED={truthy!r} must bypass the detector"
        )

    def test_chronic_count_env_var(self, monkeypatch):
        """`DREAM_BURST_CHRONIC_COUNT` lowers the chronic threshold.
        With chronic=600, a 700-event multi-day single-bucket cluster
        trips Path B."""
        monkeypatch.setenv("DREAM_BURST_CHRONIC_COUNT", "600")
        # 700 events spread across many days, single bucket — would
        # not have tripped at default chronic=2000
        entries = []
        for i in range(700):
            entries.append(_ep(
                timestamp=f"2026-04-{27 + i // 80:02d}T12:00:00+00:00",
                skill="claude-code", result="failure",
            ))
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        assert result == {}
        assert len(telemetry) == 1
        assert "chronic" in telemetry[0]["reason"]

    def test_dominant_fraction_env_var(self, monkeypatch):
        """`DREAM_BURST_DOMINANT_FRACTION` lowers the dominance threshold.
        With dominance=0.6, a 60/40 split single-skill cluster trips."""
        monkeypatch.setenv("DREAM_BURST_DOMINANT_FRACTION", "0.6")
        entries = []
        for _ in range(4200):
            entries.append(_ep(skill="claude-code", result="success"))
        for _ in range(2800):
            entries.append(_ep(skill="claude-code", result="failure"))
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        assert result == {}
        assert len(telemetry) == 1
        assert "dominant" in telemetry[0]["reason"]

    def test_malformed_env_value_falls_back_to_default(self, monkeypatch):
        """Garbage in env shouldn't crash the dream cycle. Garbage
        DREAM_BURST_MAX_EVIDENCE → fall back to default (500) so the
        burst (600 events) still trips."""
        monkeypatch.setenv("DREAM_BURST_MAX_EVIDENCE", "not-a-number")
        entries = _burst(n=600, window_min=5.0)
        telemetry: list = []
        result = promote.cluster_and_extract(entries, telemetry=telemetry)
        # Default applies, 600 > 500, burst trips.
        assert result == {}
        assert len(telemetry) == 1
