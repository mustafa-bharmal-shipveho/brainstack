"""Aggregator for AutoRecall events.

Reads the runtime's `events.log.jsonl` (where the auto-recall hook writes
one event per UserPromptSubmit) plus its rotated siblings, filters
AutoRecall records, and produces a `StatsReport` of what retrieval
actually did: how many prompts reached the hook, how many carried a doc,
how many missed the relevance gate, how slow the worker was, and which
docs were injected.

Two populations, never mixed. Telemetry contract v1.2 logs an outcome per
prompt (hit/miss/dedup/skip/timeout/unavailable/error), the injected doc
paths, and separate worker/query latencies. Anything older — or anything
claiming 1.2 while logging a `hit` with no `x_paths` — carries the old
semantics, where a logged "hit" could mean zero docs were injected. Those
records are summarized in `StatsReport.legacy` rather than averaged into
numbers they would silently corrupt.

Reading is deliberately raw: `runtime.core.events.load_events` rejects any
record whose `schema_version` differs from the runtime constant, so one
strict read of a log spanning a hook upgrade raises on the first line
written by the other version.

Surfaced via `recall stats [--since <window>] [--session-current]`.
"""
from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# Cross-encoder score histogram bucket edges for `rerank_distribution`.
#
# These are RAW LOGITS, not probabilities. The S4 calibration measured
# jina turbo at −4.3 … +0.9 and MiniLM at −11.4 … +2.5 on real pairs, so
# the earlier 0–1 edges put every score in one bucket and read as if the
# reranker emitted confidences. Labels below render the numeric range
# (`<-2.5`, `-2.5..-1.5`, …) for the same reason.
#
# One constant, one label builder: retuning against a new observed range
# (see eval/RESULTS.md) is a one-line change.
RERANK_BUCKET_EDGES: tuple[float, float, float, float] = (-2.5, -1.5, -0.75, 0.0)

# Outcomes that mean retrieval actually ran. `skip` is the only outcome
# that never starts a worker, so it is the only one outside this set.
_FIRE_OUTCOMES = ("hit", "miss", "dedup")

# `<stem>.<YYYY-MM-DD>[.<n>]<suffix>` — the name logrotate leaves behind.
_ROLLED_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.\d+)?$")

_DAY_MS = 24 * 60 * 60 * 1000


@dataclass
class StatsReport:
    """What auto-recall did over a time window, counted not claimed.

    Every field except ``legacy`` describes the schema-1.2 population
    only. ``legacy`` holds the pre-1.2 rollup; merging the two would
    average a count of injected docs with a count of prompts where the
    hook merely logged the word "hit".

    ``top_paths`` is populated since v1.2, which logs brain-relative
    paths for the docs it injected (``x_paths``). Before that the field
    existed but was always empty.

    The cross-source fields (``mcp_calls``, ``tool_calls_other``) come
    from a different data source than the auto-recall fields — they're
    parsed from Claude Code transcripts rather than ``events.log.jsonl``.
    The runtime PostToolUse hook only captures Bash/Edit/Read/Write tool
    names today, so the events log isn't a reliable source for MCP /
    Agent / Skill usage.
    """

    fired_count: int = 0
    skipped_count: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    latency_p50_ms: int = 0
    latency_p95_ms: int = 0
    surfaced_count: int = 0
    top_sources: list[tuple[str, int]] = field(default_factory=list)
    top_paths: list[tuple[str, int]] = field(default_factory=list)
    score_distribution: dict[str, int] = field(default_factory=dict)
    window_start_ts_ms: int | None = None
    window_end_ts_ms: int | None = None
    # outcomes other than hit/skip — surfaced for diagnostics. Includes
    # timeout, unavailable, error counts when present.
    other_outcomes: dict[str, int] = field(default_factory=dict)
    # Cross-source observability. Populated when the CLI was invoked with
    # transcript scanning enabled. We surface RAW MCP / builtin call
    # counts here — interpretation (e.g. "is the model calling the right
    # MCP for this question type") is deliberately punted to the user
    # because that decision is org-specific (CLAUDE.md routing rules
    # vary), and a regex-based classifier inside this open-source repo
    # had ~0% precision on the only category that mattered. See
    # CHANGELOG entry on routing-coverage removal.
    mcp_calls: dict[str, int] = field(default_factory=dict)
    tool_calls_other: dict[str, int] = field(default_factory=dict)

    # -----------------------------------------------------------------
    # v1.2 telemetry-contract fields. Every one defaults to a zero value
    # so `asdict(StatsReport())` is a stable, complete schema even for a
    # window with no events at all.
    # -----------------------------------------------------------------
    miss_count: int = 0
    dedup_count: int = 0
    total_fires: int = 0
    total_prompts: int = 0
    coverage_pct: float = 0.0
    miss_pct: float = 0.0
    dedup_pct: float = 0.0
    timeout_pct: float = 0.0
    path_split: dict[str, int] = field(default_factory=dict)
    daemon_error_count: int = 0
    degraded_count: int = 0
    index_stale_count: int = 0
    index_stale_known: int = 0
    query_p50_ms: int = 0
    query_p95_ms: int = 0
    k_candidates_total: int = 0
    k_gated_out_total: int = 0
    k_dedup_total: int = 0
    repeat_injection_rate: float = 0.0
    rerank_distribution: dict[str, int] = field(default_factory=dict)
    # Pre-1.2 events summarized separately; never merged into the fields
    # above. See `_build_legacy` for the expected key set.
    legacy: dict = field(default_factory=dict)


def aggregate_events(
    log_path: Path | str,
    *,
    since_ts_ms: int | None = None,
) -> StatsReport:
    """Read the events log and roll up AutoRecall events into a report.

    Reads `log_path` AND its rotated siblings, honoring `since_ts_ms`.
    Returns a zero-valued report when no events match — `render_human`
    displays a clear "no events" message rather than dividing by zero.
    """
    log_path = Path(log_path)
    records = [
        rec for rec in iter_auto_recall_records(log_path, since_ts_ms=since_ts_ms)
        if since_ts_ms is None or _as_int(rec.get("ts_ms")) >= since_ts_ms
    ]
    return _build_report(records, since_ts_ms=since_ts_ms)


def _build_report(records: list[dict],
                  *, since_ts_ms: int | None) -> StatsReport:
    # Chronological order is load-bearing for `repeat_injection_rate`: a
    # path is only a repeat relative to what the session already saw.
    records = sorted(records, key=lambda r: _as_int(r.get("ts_ms")))
    v12 = [r for r in records if is_v12(r)]
    legacy = [r for r in records if not is_v12(r)]

    hits = [r for r in v12 if r.get("x_outcome") == "hit"]
    misses = [r for r in v12 if r.get("x_outcome") == "miss"]
    dedups = [r for r in v12 if r.get("x_outcome") == "dedup"]
    skips = [r for r in v12 if r.get("x_outcome") == "skip"]
    others = [r for r in v12
              if r.get("x_outcome") not in ("hit", "miss", "dedup", "skip")]
    # A worker ran for everything except a skip, so `x_latency_ms`,
    # `x_path` and the k-counters are read over exactly this population.
    non_skip = [r for r in v12 if r.get("x_outcome") != "skip"]

    skip_reasons: Counter[str] = Counter(
        str(r.get("x_skip_reason") or "unknown") for r in skips
    )
    other_outcomes: Counter[str] = Counter(
        str(r.get("x_outcome") or "unknown") for r in others
    )

    total_fires = len(hits) + len(misses) + len(dedups) + len(others)
    total_prompts = total_fires + len(skips)

    latencies = [_as_int(r["x_latency_ms"]) for r in non_skip
                 if r.get("x_latency_ms") is not None]
    query_ms = [_as_int(r["x_query_ms"]) for r in non_skip
                if r.get("x_query_ms") is not None]

    path_split: Counter[str] = Counter(
        str(r.get("x_path") or "unknown") for r in non_skip
    )
    daemon_error_count = sum(
        1 for r in non_skip if r.get("x_daemon_error") is not None
    )
    degraded_count = sum(1 for r in non_skip if r.get("x_degraded") is True)
    # An absent flag is UNKNOWN, never False: the in-process path has no
    # way to vouch for index freshness, and counting silence as "fresh"
    # hides exactly the failure the flag exists to catch.
    stale_known = [r for r in non_skip
                   if isinstance(r.get("x_index_stale"), bool)]

    source_counts: Counter[str] = Counter()
    score_buckets: Counter[str] = Counter()
    rerank_buckets: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    for r in hits:
        for src, count in _as_mapping(r.get("x_sources")).items():
            source_counts[str(src)] += _as_int(count)
        for s in _as_list(r.get("x_top_scores")):
            score_buckets[_bucket_score(_as_float(s))] += 1
        for s in _as_list(r.get("x_rerank_scores")):
            rerank_buckets[_bucket_rerank(_as_float(s))] += 1
        for p in _as_list(r.get("x_paths")):
            path_counts[str(p)] += 1

    return StatsReport(
        fired_count=len(hits),
        skipped_count=len(skips),
        skip_reasons=dict(skip_reasons),
        miss_count=len(misses),
        dedup_count=len(dedups),
        other_outcomes=dict(other_outcomes),
        total_fires=total_fires,
        total_prompts=total_prompts,
        coverage_pct=_pct(len(hits), total_fires),
        miss_pct=_pct(len(misses), total_fires),
        dedup_pct=_pct(len(dedups), total_fires),
        timeout_pct=_pct(other_outcomes.get("timeout", 0), total_fires),
        path_split=dict(path_split),
        daemon_error_count=daemon_error_count,
        degraded_count=degraded_count,
        index_stale_count=sum(1 for r in stale_known if r["x_index_stale"]),
        index_stale_known=len(stale_known),
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        query_p50_ms=_percentile(query_ms, 50),
        query_p95_ms=_percentile(query_ms, 95),
        surfaced_count=sum(_as_int(r.get("x_k_returned")) for r in hits),
        k_candidates_total=sum(_as_int(r.get("x_k_candidates")) for r in non_skip),
        k_gated_out_total=sum(_as_int(r.get("x_k_gated_out")) for r in non_skip),
        k_dedup_total=sum(_as_int(r.get("x_k_dedup")) for r in non_skip),
        repeat_injection_rate=_repeat_injection_rate(hits),
        top_sources=source_counts.most_common(),
        top_paths=path_counts.most_common(10),
        score_distribution=dict(score_buckets),
        rerank_distribution=dict(rerank_buckets),
        legacy=_build_legacy(legacy),
        window_start_ts_ms=since_ts_ms,
        window_end_ts_ms=max((_as_int(r.get("ts_ms")) for r in records),
                             default=None),
    )


def _repeat_injection_rate(hits: list[dict]) -> float:
    """Fraction of injected paths the session had already been shown.

    Same doc, same session, second time = the model already has it in
    context. Same doc in a different session is a fresh injection.
    `hits` must already be in chronological order.
    """
    seen: dict[str, set[str]] = {}
    total = 0
    repeats = 0
    for r in hits:
        session = str(r.get("session_id") or "")
        shown = seen.setdefault(session, set())
        for raw in _as_list(r.get("x_paths")):
            path = str(raw)
            total += 1
            if path in shown:
                repeats += 1
            else:
                shown.add(path)
    return repeats / total if total else 0.0


def iter_auto_recall_records(
    log_path: Path | str, *, since_ts_ms: int | None = None
) -> Iterator[dict]:
    """Tolerant raw-JSON reader over `events.log.jsonl` + rotated siblings.

    Replaces `runtime.core.events.load_events` for stats: that loader
    rejects any line whose `schema_version` isn't the runtime's current
    constant, so a log spanning a schema upgrade raises on the first line
    written by the other version. This reader streams raw dicts (no
    `EventRecord` construction), skips malformed lines, and also reads
    `events.log*.jsonl` siblings in the same directory so a rotated log
    doesn't silently drop out of the window.

    `since_ts_ms` additionally skips a rotated file whose filename date is
    entirely before the window — the date is the day the file was closed,
    so `date + 1 day < since` means every record inside predates it and
    the file never has to be opened.
    """
    for path in _log_files(Path(log_path), since_ts_ms=since_ts_ms):
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                # Cheap prefix check first: the log is tens of MB and most
                # non-record lines are truncated writes, not objects.
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("event") != "AutoRecall":
                    continue
                yield rec


def _log_files(log_path: Path, *, since_ts_ms: int | None = None) -> list[Path]:
    """`log_path` plus its rotated siblings, oldest name first.

    Rotation renames `events.log.jsonl` to `events.log.<date>.jsonl`, so
    the siblings share the stem and suffix. Deduplicated by path — the
    glob matches the live file too, and counting it twice would double
    every number in the report.
    """
    name = log_path.name
    suffix = ".jsonl" if name.endswith(".jsonl") else ""
    stem = name[: len(name) - len(suffix)] if suffix else name
    paths = {log_path}
    parent = log_path.parent
    if parent.is_dir():
        try:
            paths.update(p for p in parent.glob(f"{stem}*{suffix}") if p.is_file())
        except OSError:
            pass
    keep: list[Path] = []
    for path in sorted(paths):
        if path == log_path or _roll_in_window(path, stem, suffix, since_ts_ms):
            keep.append(path)
    return keep


def _roll_in_window(path: Path, stem: str, suffix: str,
                    since_ts_ms: int | None) -> bool:
    """False only when `path` is a dated roll that closed before the window."""
    if since_ts_ms is None:
        return True
    middle = path.name[len(stem) + 1: len(path.name) - len(suffix)]
    m = _ROLLED_DATE_RE.match(middle)
    if not m:
        return True
    try:
        day = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return True
    # The stamp is the day the file was closed, so everything inside is
    # older than the end of that day.
    return int(day.timestamp() * 1000) + _DAY_MS >= since_ts_ms


def is_v12(rec: dict) -> bool:
    """True when `rec` carries full v1.2 AutoRecall semantics.

    A record can claim ``schema_version == "1.2"`` and still be pre-1.2 in
    substance: a ``hit`` with no ``x_paths`` cannot be joined to a
    transcript and may be a phantom, so it is classified legacy. A v1.2
    ``miss``/``dedup``/etc. has no paths by definition and stays in the
    1.2 population.
    """
    if rec.get("schema_version") != "1.2":
        return False
    if rec.get("x_outcome") == "hit" and "x_paths" not in rec:
        return False
    return True


def _rerank_labels() -> list[str]:
    """Bucket labels derived from `RERANK_BUCKET_EDGES`, low to high, so
    retuning the edges renames the buckets without touching the renderer.

    Ranges join on `..` rather than `-`: the edges are signed logits, and
    `-2.5--1.5` is unreadable.
    """
    edges = RERANK_BUCKET_EDGES
    labels = [f"<{edges[0]:g}"]
    labels += [f"{lo:g}..{hi:g}" for lo, hi in zip(edges, edges[1:])]
    labels.append(f"{edges[-1]:g}+")
    return labels


def _bucket_rerank(score: float) -> str:
    """Bucket a raw cross-encoder score at `RERANK_BUCKET_EDGES`.

    Half-open on the left: a score exactly at an edge belongs to the
    bucket that starts there.
    """
    labels = _rerank_labels()
    edges = RERANK_BUCKET_EDGES
    if score < edges[0]:
        return labels[0]
    for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
        if lo <= score < hi:
            return labels[i + 1]
    return labels[-1]


def _build_legacy(records: list[dict]) -> dict:
    """Summarize pre-1.2 (or 1.2-without-x_paths) records.

    Keys mirror the pre-1.2 semantics on purpose: `hit_logged` is what the
    old hook wrote, `phantom_hits` is how often it wrote "hit" while
    injecting nothing, and the latency is labelled query-only because in
    1.1 `x_latency_ms` measured retrieval alone. Never merged into the
    1.2 fields on `StatsReport`.
    """
    outcomes: Counter[str] = Counter(
        str(r.get("x_outcome") or "unknown") for r in records
    )
    hits = [r for r in records if r.get("x_outcome") == "hit"]
    phantom = sum(1 for r in hits if _as_int(r.get("x_k_returned")) == 0)
    latencies = [_as_int(r["x_latency_ms"]) for r in records
                 if r.get("x_outcome") != "skip"
                 and r.get("x_latency_ms") is not None]
    sources: Counter[str] = Counter()
    for r in hits:
        for src, count in _as_mapping(r.get("x_sources")).items():
            sources[str(src)] += _as_int(count)
    return {
        "events": len(records),
        "hit_logged": len(hits),
        "phantom_hits": phantom,
        "real_hits": len(hits) - phantom,
        "skip": outcomes.get("skip", 0),
        "timeout": outcomes.get("timeout", 0),
        "unavailable": outcomes.get("unavailable", 0),
        "error": outcomes.get("error", 0),
        "query_p50_ms": _percentile(latencies, 50),
        "query_p95_ms": _percentile(latencies, 95),
        "surfaced_count": sum(_as_int(r.get("x_k_returned")) for r in hits),
        "top_sources": sources.most_common(),
    }


# ---------------------------------------------------------------------------
# Coercion helpers — every value here came off a disk line written by some
# older version of the hook, so nothing about its type is guaranteed.
# ---------------------------------------------------------------------------


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _as_mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _pct(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def _percentile(values: Iterable[int], p: int) -> int:
    """Approximate p-th percentile. statistics.quantiles needs >= 2 values
    so we fall back to `min`/`max`/single-value for tiny samples."""
    vals = sorted(values)
    if not vals:
        return 0
    if len(vals) == 1:
        return vals[0]
    if p >= 100:
        return vals[-1]
    if p <= 0:
        return vals[0]
    # Linear interpolation
    idx = (p / 100.0) * (len(vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(vals) - 1)
    weight = idx - lo
    return int(vals[lo] * (1 - weight) + vals[hi] * weight)


def _bucket_score(score: float) -> str:
    if score >= 0.85:
        return "0.85+"
    if score >= 0.70:
        return "0.70-0.85"
    if score >= 0.50:
        return "0.50-0.70"
    return "<0.50"


# ---------------------------------------------------------------------------
# Time-window parsing — `--since 7d`, `24h`, `1h`, ISO date
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([dhms])$")


def parse_since(value: str | None, *, now_ms: int | None = None) -> int | None:
    """Parse a `--since` argument to a UNIX-ms timestamp.

    Accepts:
        - duration: "7d", "24h", "30m", "60s" → relative to ``now_ms``
        - ISO date: "2026-01-01" → midnight UTC of that date
        - empty / None → None (no window)

    `now_ms` is injectable for test determinism — pass a fixed timestamp
    so duration parses produce the same result regardless of clock.
    Defaults to the current time when omitted.

    Raises ValueError on anything else so the user gets a clear error
    instead of silently scanning all-time when they meant a window.
    """
    if not value:
        return None
    value = value.strip()
    m = _DURATION_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit] * n
        if now_ms is None:
            now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        return now_ms - seconds * 1000
    # ISO date
    try:
        d = datetime.datetime.fromisoformat(value).replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        raise ValueError(
            f"--since: expected '7d' / '24h' / '30m' / 'YYYY-MM-DD', got {value!r}"
        )
    return int(d.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Human renderer
# ---------------------------------------------------------------------------


_LABEL_WIDTH = 14

# RRF buckets, strongest first — `_bucket_score` produces exactly these.
_SCORE_BUCKET_ORDER = ("0.85+", "0.70-0.85", "0.50-0.70", "<0.50")


def render_human(report: StatsReport) -> str:
    """Format `report` as the user-facing block.

    Every number here is a count of something that happened. The old
    closing paragraph ("without auto-recall, all N turns would have
    started with only MEMORY.md…") is deliberately gone: it multiplied a
    phantom-inflated fire count by a doc count nobody had opened, and the
    telemetry cannot support that claim. `recall stats --utilization` is
    the honest version of that question.

    Legacy events render in their own block, under their own labels, so a
    pre-1.2 hook's numbers can never be read as if they meant the same
    thing.
    """
    other_total = sum(_as_int(v) for v in report.other_outcomes.values())
    counter_total = (report.fired_count + report.miss_count
                     + report.dedup_count + other_total)
    # Hand-built reports (tests, other callers) may set the outcome
    # counters without the derived denominators; recompute rather than
    # rendering a coverage line divided by a zero the caller didn't mean.
    total_fires = report.total_fires or counter_total
    total_prompts = report.total_prompts or (total_fires + report.skipped_count)
    legacy_events = _as_int(report.legacy.get("events"))
    # Check if cross-source tool-call data is populated. Use any-positive-value
    # check (not bool(dict)) so a programmatically constructed all-zero dict
    # doesn't suppress the no-events message. Today aggregate_tool_calls only
    # returns positive counts so the dict form would be safe, but pinning the
    # invariant keeps render_human robust against future producers.
    cross_source_populated = (
        any(int(v) > 0 for v in report.mcp_calls.values())
        or any(int(v) > 0 for v in report.tool_calls_other.values())
    )
    # Only fully bail when there's NOTHING to report — including no
    # cross-source tool calls. A user with auto-recall disabled but
    # active Claude Code transcripts should still see the tool-call
    # breakdown. Codex 2026-05-05 P2.
    if total_prompts == 0 and legacy_events == 0 and not cross_source_populated:
        return (
            "brainstack: no auto-recall events recorded in this window.\n"
            "  Enable with: ./install.sh --enable-auto-recall\n"
            "  Or check the runtime log directory for events.log.jsonl"
        )

    lines: list[str] = [f"brainstack: auto-recall{_format_window(report)}\n"]
    if total_prompts > 0:
        lines.extend(_render_v12(report, total_fires, total_prompts))
    elif legacy_events:
        # Rendering zeros here would read as "auto-recall did nothing".
        # The honest answer names the cause and the fix.
        lines.append("  No schema-1.2 events in this window — hooks predate"
                     " v1.2 (./install.sh --upgrade).")
    if legacy_events:
        lines.append("")
        lines.extend(_render_legacy(report.legacy, legacy_events))
    # Cross-source sections — only render when populated. An empty
    # mcp_calls / tool_calls_other dict means the CLI was invoked with
    # --no-tools or there's no transcripts dir; either way, omit the
    # header rather than show "(empty)".
    if report.mcp_calls or report.tool_calls_other:
        lines.append("")
        lines.append("  Model-driven tool calls (in same window):")
        for name, n in sorted(report.mcp_calls.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {name:<26}: {n} calls")
        # tool_calls_other displayed compactly — high-frequency builtins
        # like Bash dominate; surface as one summary line
        if report.tool_calls_other:
            top = sorted(report.tool_calls_other.items(), key=lambda kv: -kv[1])[:6]
            summary = ", ".join(f"{k} ({v})" for k, v in top)
            lines.append(f"    {'builtins':<26}: {summary}")
    return "\n".join(lines)


def _row(label: str, value: str) -> str:
    return f"  {label + ':':<{_LABEL_WIDTH}}{value}"


def _render_v12(report: StatsReport, total_fires: int,
                total_prompts: int) -> list[str]:
    lines: list[str] = []
    prompts = f"{total_prompts} ({total_fires} fires"
    if report.skipped_count:
        breakdown = ", ".join(
            f"{n} {reason}" for reason, n in sorted(
                report.skip_reasons.items(), key=lambda kv: -kv[1]
            )
        )
        prompts += f", {report.skipped_count} skipped: {breakdown}"
    lines.append(_row("Prompts", prompts + ")"))

    if total_fires > 0:
        lines.append(_row(
            "Injected",
            f"{report.fired_count} / {total_fires} fires"
            f" ({_round_pct(report.fired_count, total_fires)}%)"
            " carried at least one doc"))
        lines.append(_row(
            "Miss",
            f"{report.miss_count} ({_round_pct(report.miss_count, total_fires)}%)"
            " nothing passed the relevance gate"))
        lines.append(_row(
            "Dedup",
            f"{report.dedup_count} ({_round_pct(report.dedup_count, total_fires)}%)"
            " every passing doc was already shown this session"))
    timeouts = _as_int(report.other_outcomes.get("timeout"))
    unavailable = _as_int(report.other_outcomes.get("unavailable"))
    errors = _as_int(report.other_outcomes.get("error"))
    if timeouts or unavailable or errors:
        lines.append(_row(
            "Timeout",
            f"{timeouts} ({_round_pct(timeouts, total_fires)}%)"
            f" · unavailable {unavailable} · error {errors}"))

    if report.path_split or report.daemon_error_count or report.degraded_count:
        # daemon / inproc first (the split users act on), anything else —
        # including "unknown" for an event that never named its path — after.
        known = [n for n in ("daemon", "inproc") if n in report.path_split]
        extra = sorted(k for k in report.path_split if k not in ("daemon", "inproc"))
        split = ", ".join(f"{n} {report.path_split[n]}" for n in known + extra)
        path_line = (f"{split} (daemon_error {report.daemon_error_count},"
                     f" degraded {report.degraded_count})")
        if report.index_stale_known:
            # Denominator is what actually reported, not the daemon count:
            # "0 / 0" would read as "the index is fresh" when the truth is
            # "nobody checked".
            path_line += (f" · index stale {report.index_stale_count}"
                          f" / {report.index_stale_known} daemon fires")
        lines.append(_row("Path", path_line))

    if total_fires > 0 and (report.latency_p50_ms or report.latency_p95_ms
                            or report.query_p50_ms or report.query_p95_ms):
        lines.append(_row(
            "Latency",
            f"worker p50 {report.latency_p50_ms}ms, p95 {report.latency_p95_ms}ms"
            f" · query p50 {report.query_p50_ms}ms, p95 {report.query_p95_ms}ms"))

    docs_shown = bool(report.surfaced_count or report.k_candidates_total
                      or report.k_gated_out_total or report.k_dedup_total)
    if total_fires > 0 and docs_shown:
        avg = report.surfaced_count / report.fired_count if report.fired_count else 0.0
        lines.append(_row(
            "Docs",
            f"{report.surfaced_count} injected (avg {avg:.1f} per hit)"
            f" · repeat-injection {report.repeat_injection_rate * 100:.1f}%"
            f" · candidates {report.k_candidates_total},"
            f" gated out {report.k_gated_out_total},"
            f" dedup {report.k_dedup_total}"))
    if report.top_sources:
        lines.append(_row("Sources", ", ".join(
            f"{name} ({n})" for name, n in report.top_sources[:5])))
    if report.score_distribution:
        lines.append(_row("RRF scores", _render_histogram(
            report.score_distribution, _SCORE_BUCKET_ORDER)))
    if report.rerank_distribution:
        lines.append(_row("Rerank", _render_histogram(
            report.rerank_distribution, _rerank_labels())))
    if report.top_paths:
        lines.append(_row("Top docs", ", ".join(
            f"{path} ({n})" for path, n in report.top_paths[:10])))
    if docs_shown or report.top_paths:
        lines.append("  These count docs injected, not docs used —"
                     " see `recall stats --utilization`.")
    return lines


def _render_legacy(legacy: dict, events: int) -> list[str]:
    counts = " · ".join(
        [f"logged hit {_as_int(legacy.get('hit_logged'))}"
         f" (phantom {_as_int(legacy.get('phantom_hits'))} = hit with 0 docs,"
         f" real {_as_int(legacy.get('real_hits'))})"]
        + [f"{label} {_as_int(legacy.get(label))}"
           for label in ("skip", "timeout", "unavailable", "error")
           if _as_int(legacy.get(label))]
    )
    detail = (f"query-only latency p50 {_as_int(legacy.get('query_p50_ms'))}ms,"
              f" p95 {_as_int(legacy.get('query_p95_ms'))}ms")
    sources = legacy.get("top_sources") or []
    if sources:
        detail += " · sources " + ", ".join(
            f"{name} ({n})" for name, n in list(sources)[:5])
    return [
        f"  Legacy (pre-1.2 semantics, not comparable): {events} events",
        f"    {counts}",
        f"    {detail}",
    ]


def _round_pct(numerator: int, denominator: int) -> int:
    return round(_pct(numerator, denominator))


def _render_histogram(distribution: dict[str, int],
                      order: Iterable[str]) -> str:
    """`N in <bucket>` pairs, known buckets in bucket order first.

    Anything the current bucket edges don't name still prints, after the
    known ones: a report can arrive from `--json` written by a build with
    different `RERANK_BUCKET_EDGES`, and silently dropping those counts
    would understate the histogram instead of showing it is stale.
    """
    known = [b for b in order if b in distribution]
    rest = [b for b in distribution if b not in set(known)]
    return ", ".join(f"{distribution[b]} in {b}" for b in known + rest)


def _format_window(report: StatsReport) -> str:
    if report.window_start_ts_ms is None:
        return " (all time)"
    start = datetime.datetime.fromtimestamp(
        report.window_start_ts_ms / 1000, tz=datetime.timezone.utc
    ).date().isoformat()
    return f" (since {start})"


# ---------------------------------------------------------------------------
# Cross-source observability — Phase 1
# ---------------------------------------------------------------------------


def aggregate_tool_calls(
    transcripts_dir: Path | str,
    *,
    since_ts_ms: int | None = None,
) -> dict[str, int]:
    """Walk Claude Code session transcripts and count `tool_use` blocks.

    The transcripts live at ``~/.claude/projects/<slug>/<sid>.jsonl`` (or
    a custom path passed in). Each line is a JSON record; assistant
    messages contain ``message.content`` arrays where ``tool_use`` blocks
    carry a ``name`` field (e.g. ``mcp__minerva__search_code``, ``Bash``,
    ``Agent``).

    MCP tools (prefix ``mcp__``) are aggregated by namespace —
    ``mcp__minerva__search_code`` and ``mcp__minerva__get_file`` both
    roll up under ``mcp__minerva__*``. Non-MCP tools keep their literal
    name (Bash, Edit, etc.).

    Why this aggregator (rather than reading events.log.jsonl): the
    runtime's PostToolUse hook only captures Bash/Edit/Read/Write tool
    names — most MCP and Agent calls don't surface in events.log. Raw
    transcripts are the authoritative source.
    """
    root = Path(transcripts_dir)
    if not root.is_dir():
        return {}
    counts: Counter[str] = Counter()
    for jsonl in root.rglob("*.jsonl"):
        try:
            with jsonl.open() as f:
                for line in f:
                    counts.update(_extract_tool_names(line, since_ts_ms))
        except OSError:
            continue
    return dict(counts)


def _extract_tool_names(line: str, since_ts_ms: int | None) -> Iterable[str]:
    """Parse one transcript line, yield namespaced tool names for any
    tool_use blocks whose timestamp is in window. Bad lines yield
    nothing (caller continues — real transcripts have malformed rows)."""
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ()
    if not isinstance(rec, dict):
        return ()
    if since_ts_ms is not None:
        ts_ms = _parse_iso_to_ms(rec.get("timestamp"))
        if ts_ms is None or ts_ms < since_ts_ms:
            return ()
    msg = rec.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return ()
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if isinstance(name, str) and name:
            out.append(_namespace_tool_name(name))
    return out


def _namespace_tool_name(name: str) -> str:
    """`mcp__minerva__search_code` → `mcp__minerva__*`. Everything else
    keeps its literal name."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"{parts[0]}__{parts[1]}__*"
    return name


def _parse_iso_to_ms(iso: str | None) -> int | None:
    """Best-effort ISO-8601 → UNIX-ms. Returns None on failure."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        # Handle trailing Z (Python 3.10 fromisoformat needs +00:00)
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# NOTE: an earlier version of this module included a regex-based
# `classify_prompt()` and `compute_routing_coverage()` that scanned
# transcripts to compute "is the model calling the right MCP for this
# kind of question" coverage percentages.
#
# That feature was removed because:
#   1. The regex classifier had 0% precision and 0% recall on the only
#      category that mattered when measured against 50 hand-labeled
#      prompts. Generic English patterns like "how does X work" don't
#      identify domain-specific questions; they fire on chitchat, on
#      compaction summaries, on assistant-quoted text — and miss real
#      domain prompts that happen to use different phrasing.
#   2. Any classifier that could work would be org-specific (product keyword
#      list, internal-tool-name lexicon, etc.) and
#      this is an open-source tool.
#   3. The raw MCP call counts (above, in StatsReport.mcp_calls) are
#      already useful and org-agnostic — we leave the interpretation to
#      the operator.
#
# If a future user wants per-org routing coverage, the right shape is a
# config-driven feature (declare keywords + expected MCP per rule in a
# user-owned config file) rather than hardcoded patterns in this repo.
