"""Rotation of `events.log.jsonl` and the readers that must follow it (S5 / R5).

The runtime event log is the other unbounded tracked file in the brain
(66 MB on the machine that motivated S5). It rotates on the same rule as
the episodic stream: check the size before appending, rename the current
file to `<stem>.<YYYY-MM-DD><suffix>`, then append to a fresh file.

Reader split — deliberate, pinned below:
    `load_events`      current file only. Reinjection and
                       `_session_current_ts_ms` only care about the recent
                       tail, and globbing would make every session pay for
                       the whole history.
    `load_events_all`  rolled + current, for anything auditing history.
    `aggregate_events` rolled + current, but skips a rolled file whose
                       filename date is entirely before the `--since`
                       window. The date in the name is the day the file was
                       closed, so `date + 1 day < since` means every record
                       inside predates the window.

Two fixture shapes, on purpose
------------------------------
The loader tests build real `EventRecord`s at `EVENT_LOG_SCHEMA_VERSION`,
because `load_event` validates the version it is compiled against.

The stats tests write RAW JSON lines at schema **1.2** with `x_paths`,
matching `tests/recall/test_stats.py::_write_event`. Under the v1.2
telemetry contract an AutoRecall `hit` without `x_paths` is classified
LEGACY and is never merged into `fired_count` / `surfaced_count`, so a
fixture missing that key would assert nothing about rolled-file reading.
Every 1.2 hit here therefore carries `x_paths`, `x_paths_truncated`,
`x_path`, and `x_k_returned == len(x_paths)`.

Cross-slice note: the two `test_stats_*` cases need BOTH this slice's
rotation glob AND the corpus-stats slice's `aggregate_events` rewrite (raw
line reader, no strict schema check). They stay red until both land.

Boundary rule
-------------
Rotation triggers when the file is strictly LARGER than the threshold.
Exactly at the threshold is not oversize. (`plans/guards.md` writes this
as `size >= threshold`; the tests below pin `>`, so the implementation
needs the strict comparison in all three rotation sites.)

Hermetic: tmp paths only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from runtime.core.events import (
    EVENT_LOG_ROTATE_BYTES,
    EVENT_LOG_SCHEMA_VERSION,
    EventRecord,
    append_event,
    dump_event,
    load_events,
    load_events_all,
)
from runtime.core.locking import (
    iter_log_paths,
    locked_append,
    rolled_name,
    sentinel_lock_path,
)

LOG = "events.log.jsonl"
LOG_GLOB = "events.log*.jsonl"
ROLLED_RE = re.compile(r"^events\.log\.\d{4}-\d{2}-\d{2}(\.\d+)?\.jsonl$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _day_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _record(ts_ms: int, session_id: str = "s") -> EventRecord:
    """One event at the version `load_event` accepts — loader fixtures only."""
    return EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=ts_ms,
        event="AutoRecall",
        session_id=session_id,
        turn=0,
        extensions={"x_outcome": "hit", "x_k_requested": 5, "x_k_returned": 1},
    )


def _v12_hit(ts_ms: int, paths: list[str], session_id: str = "s") -> dict:
    """One schema-1.2 AutoRecall hit as a raw record.

    Shape mirrors tests/recall/test_stats.py::_write_event: extensions are
    flattened to the top level exactly as the hook writes them, and the hit
    carries the `x_paths` that keep it out of the legacy bucket.
    """
    return {
        "schema_version": "1.2",
        "ts_ms": ts_ms,
        "event": "AutoRecall",
        "session_id": session_id,
        "turn": 0,
        "x_outcome": "hit",
        "x_path": "daemon",
        "x_latency_ms": 20,
        "x_query_ms": 12,
        "x_k_requested": 5,
        "x_k_returned": len(paths),
        "x_paths": list(paths),
        "x_paths_truncated": False,
        "x_sources": {"brain": len(paths)},
    }


def _write_log(path: Path, records: list[EventRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(dump_event(r) + "\n" for r in records), encoding="utf-8")


def _write_raw(path: Path, records: list[dict]) -> None:
    """Write raw JSON lines — no schema validation, same as the hook does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )


def _rolled(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob(LOG_GLOB) if p.name != LOG)


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# runtime/core/locking.py
# --------------------------------------------------------------------------


def test_locked_append_rolls_at_threshold(tmp_path):
    """With `rotate_bytes` set, an oversize log is renamed away before the
    next append; without it, behaviour is unchanged."""
    log = tmp_path / LOG
    line = "x" * 1500

    locked_append(log, line, rotate_bytes=1024)
    assert _rolled(tmp_path) == [], "must not roll before the threshold is crossed"

    locked_append(log, "second " + line, rotate_bytes=1024)
    rolls = _rolled(tmp_path)
    assert len(rolls) == 1, f"expected one roll, found {[p.name for p in rolls]}"
    assert ROLLED_RE.match(rolls[0].name), f"bad rolled name {rolls[0].name}"
    assert _lines(rolls[0]) == [line]
    assert _lines(log) == ["second " + line], "current file holds post-roll lines only"

    # No rotate_bytes → never rolls, however large the file gets.
    plain = tmp_path / "plain" / LOG
    for _ in range(4):
        locked_append(plain, line)
    assert _rolled(plain.parent) == []
    assert len(_lines(plain)) == 4


def test_locked_append_boundary_rolls_only_past_threshold(tmp_path):
    """Exactly at `rotate_bytes` is not oversize; one byte over is.

    `locked_append` adds the trailing newline, so a line of N-1 characters
    lands the file at exactly N bytes.
    """
    threshold = 512

    at = tmp_path / "at" / LOG
    locked_append(at, "x" * (threshold - 1), rotate_bytes=threshold)
    assert at.stat().st_size == threshold
    locked_append(at, "after-boundary", rotate_bytes=threshold)
    assert _rolled(at.parent) == [], (
        "a file sitting exactly at the threshold must not roll"
    )
    assert len(_lines(at)) == 2

    over = tmp_path / "over" / LOG
    locked_append(over, "x" * threshold, rotate_bytes=threshold)
    assert over.stat().st_size == threshold + 1
    locked_append(over, "after-boundary", rotate_bytes=threshold)
    rolls = _rolled(over.parent)
    assert len(rolls) == 1, (
        f"one byte over the threshold must roll; found {[p.name for p in rolls]}"
    )
    assert _lines(rolls[0]) == ["x" * threshold]
    assert _lines(over) == ["after-boundary"]


def test_rolled_name_events_log(tmp_path):
    """`events.log.jsonl` keeps its compound stem; the day goes before the
    final suffix, and a same-day collision takes a counter."""
    assert rolled_name(Path("events.log.jsonl"), "2026-09-04") == Path(
        "events.log.2026-09-04.jsonl"
    )

    (tmp_path / "events.log.2026-09-04.jsonl").write_text("taken\n")
    assert rolled_name(tmp_path / LOG, "2026-09-04") == (
        tmp_path / "events.log.2026-09-04.1.jsonl"
    )

    (tmp_path / "events.log.2026-09-04.1.jsonl").write_text("taken too\n")
    assert rolled_name(tmp_path / LOG, "2026-09-04") == (
        tmp_path / "events.log.2026-09-04.2.jsonl"
    )


def test_iter_log_paths_orders_rolled_then_current(tmp_path):
    """Rolled files ascending by name, current last. Sentinel locks, temp
    files and unrelated logs are not part of the stream."""
    for name in (
        "events.log.2026-09-04.jsonl",
        "events.log.2026-09-01.jsonl",
        "events.log.2026-09-04.1.jsonl",
        LOG,
    ):
        (tmp_path / name).write_text("{}\n")
    sentinel_lock_path(tmp_path / LOG).write_text("")
    (tmp_path / "events.log.jsonl.tmp").write_text("partial")
    (tmp_path / "other.log.jsonl").write_text("{}\n")

    names = [p.name for p in iter_log_paths(tmp_path / LOG)]
    # Byte order puts ".1" before the bare dated name ('1' < 'j'); the
    # contract is name-ascending, not chronological.
    assert names == [
        "events.log.2026-09-01.jsonl",
        "events.log.2026-09-04.1.jsonl",
        "events.log.2026-09-04.jsonl",
        LOG,
    ]


# --------------------------------------------------------------------------
# runtime/core/events.py
# --------------------------------------------------------------------------


def test_append_event_uses_rotate_constant(tmp_path, monkeypatch):
    """`append_event` is the only writer of the event log, so it owns the
    threshold: it hands `EVENT_LOG_ROTATE_BYTES` to `locked_append`."""
    assert EVENT_LOG_ROTATE_BYTES == 20 * 1024 * 1024

    import runtime.core.locking as locking

    real = locking.locked_append
    captured: dict = {}

    def _spy(path, line, **kwargs):
        captured.update(kwargs)
        return real(path, line, **kwargs)

    monkeypatch.setattr(locking, "locked_append", _spy)

    log = tmp_path / LOG
    append_event(log, _record(_now_ms()))
    assert captured.get("rotate_bytes") == EVENT_LOG_ROTATE_BYTES

    # And the threshold is honoured end to end.
    monkeypatch.setattr("runtime.core.events.EVENT_LOG_ROTATE_BYTES", 512)
    for i in range(6):
        append_event(log, _record(_now_ms() + i, session_id=f"s{i}"))
    assert _rolled(tmp_path), (
        f"append_event never rolled; found {[p.name for p in tmp_path.glob(LOG_GLOB)]}"
    )


def test_load_events_current_only_unchanged(tmp_path):
    """Reinjection keeps reading the current file only; `load_events_all`
    is the opt-in that spans rolled history."""
    log = tmp_path / LOG
    rolled = tmp_path / f"events.log.{_day_ago(1)}.jsonl"
    _write_log(rolled, [_record(_now_ms() - 90_000, session_id="rolled")])
    _write_log(log, [_record(_now_ms(), session_id="current")])

    current_only = load_events(log)
    assert [e.session_id for e in current_only] == ["current"]

    everything = load_events_all(log)
    assert [e.session_id for e in everything] == ["rolled", "current"]


# --------------------------------------------------------------------------
# recall/stats.py
# --------------------------------------------------------------------------


def _docs(*names: str) -> list[str]:
    return [f"memory/semantic/lessons/{n}.md" for n in names]


def test_stats_aggregate_reads_rolled_files(tmp_path):
    """`recall stats` would silently lose most of its history the day the
    log rolls unless the aggregator globs."""
    from recall.stats import aggregate_events

    log = tmp_path / LOG
    now = _now_ms()
    _write_raw(tmp_path / f"events.log.{_day_ago(1)}.jsonl", [
        _v12_hit(now - 86_400_000, _docs("a", "b", "c")),
        _v12_hit(now - 80_000_000, _docs("d", "e", "f")),
    ])
    _write_raw(log, [_v12_hit(now, _docs("g", "h", "i", "j"))])

    report = aggregate_events(log)
    assert report.fired_count == 3
    assert report.surfaced_count == 10  # 3 + 3 + 4


def test_stats_since_skips_rolls_older_than_window(tmp_path):
    """A rolled file whose name predates the window is not opened at all.

    The records inside are valid 1.2 hits carrying recent timestamps, so a
    report that counts them proves the aggregator read a file it should
    have skipped on the filename date alone.
    """
    from recall.stats import aggregate_events

    log = tmp_path / LOG
    now = _now_ms()
    stale_roll = tmp_path / f"events.log.{_day_ago(30)}.jsonl"
    _write_raw(stale_roll, [
        _v12_hit(now, _docs("s1", "s2", "s3")),
        _v12_hit(now, _docs("s4", "s5", "s6")),
    ])
    recent_roll = tmp_path / f"events.log.{_day_ago(1)}.jsonl"
    _write_raw(recent_roll, [_v12_hit(now, _docs("r1", "r2"))])
    _write_raw(log, [_v12_hit(now, _docs("c1", "c2", "c3", "c4"))])

    since = now - 7 * 24 * 60 * 60 * 1000
    report = aggregate_events(log, since_ts_ms=since)
    assert report.fired_count == 2, (
        "the 30-day-old roll must be skipped on its filename date"
    )
    assert report.surfaced_count == 6  # 2 from the recent roll + 4 from current
