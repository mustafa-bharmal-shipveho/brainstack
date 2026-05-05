"""Aggregator for AutoRecall events.

Reads the runtime's `events.log.jsonl` (where the auto-recall hook writes
one event per UserPromptSubmit), filters AutoRecall records, and produces
a `StatsReport` summarizing fire rate, latency, source distribution, and
ROI framing for the user.

Surfaced via `recall stats [--since <window>] [--session-current]`.
"""
from __future__ import annotations

import datetime
import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from runtime.core.events import EventRecord, load_events


@dataclass
class StatsReport:
    """Aggregate ROI snapshot for auto-recall over a time window.

    ``top_paths`` is currently always empty. We deliberately don't log
    surfaced paths to telemetry (they can leak project structure). If we
    add an opt-in path-emission flag later, this field becomes populated.
    Documenting it now keeps the schema forward-compatible.

    The cross-source fields (``mcp_calls``, ``tool_calls_other``,
    ``routing_coverage``) come from a different data source than the
    auto-recall fields — they're parsed from Claude Code transcripts
    rather than ``events.log.jsonl``. The runtime PostToolUse hook only
    captures Bash/Edit/Read/Write tool names today, so the events log
    isn't a reliable source for MCP / Agent / Skill usage.
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
    # Cross-source observability (Phase 1). Populated when the CLI was
    # invoked with transcript scanning enabled.
    mcp_calls: dict[str, int] = field(default_factory=dict)
    tool_calls_other: dict[str, int] = field(default_factory=dict)
    routing_coverage: dict[str, int | float] = field(default_factory=dict)


def aggregate_events(
    log_path: Path | str,
    *,
    since_ts_ms: int | None = None,
) -> StatsReport:
    """Read the events log and roll up AutoRecall events into a report.

    Honors the `since_ts_ms` window if provided. Returns a zero-valued
    report when no events match — `render_human` will display a clear
    "no fires yet" message rather than a divide-by-zero or empty box.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return StatsReport()

    events = load_events(log_path)
    auto_recall_events: list[EventRecord] = [
        e for e in events
        if e.event == "AutoRecall"
        and (since_ts_ms is None or e.ts_ms >= since_ts_ms)
    ]
    if not auto_recall_events:
        return StatsReport(window_start_ts_ms=since_ts_ms)

    return _build_report(auto_recall_events, since_ts_ms=since_ts_ms)


def _build_report(events: list[EventRecord],
                  *, since_ts_ms: int | None) -> StatsReport:
    fired: list[EventRecord] = []
    skipped: list[EventRecord] = []
    other: list[EventRecord] = []
    for e in events:
        outcome = e.extensions.get("x_outcome", "")
        if outcome == "hit":
            fired.append(e)
        elif outcome == "skip":
            skipped.append(e)
        else:
            other.append(e)

    skip_reasons: Counter[str] = Counter(
        e.extensions.get("x_skip_reason", "unknown") for e in skipped
    )
    other_outcomes: Counter[str] = Counter(
        e.extensions.get("x_outcome", "unknown") for e in other
    )
    surfaced_count = sum(
        int(e.extensions.get("x_k_returned", 0)) for e in fired
    )

    latencies = [
        int(e.extensions.get("x_latency_ms", 0)) for e in fired
        if "x_latency_ms" in e.extensions
    ]
    p50 = _percentile(latencies, 50) if latencies else 0
    p95 = _percentile(latencies, 95) if latencies else 0

    source_counts: Counter[str] = Counter()
    for e in fired:
        for src, count in (e.extensions.get("x_sources") or {}).items():
            source_counts[src] += int(count)

    score_buckets: Counter[str] = Counter()
    for e in fired:
        for s in e.extensions.get("x_top_scores") or []:
            score_buckets[_bucket_score(float(s))] += 1

    return StatsReport(
        fired_count=len(fired),
        skipped_count=len(skipped),
        skip_reasons=dict(skip_reasons),
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        surfaced_count=surfaced_count,
        top_sources=source_counts.most_common(),
        top_paths=[],  # see class docstring
        score_distribution=dict(score_buckets),
        window_start_ts_ms=since_ts_ms,
        window_end_ts_ms=max((e.ts_ms for e in events), default=None),
        other_outcomes=dict(other_outcomes),
    )


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


def render_human(report: StatsReport) -> str:
    """Format `report` as the user-facing block. The ROI line ("without
    auto-recall …") is the load-bearing part — pin it in tests.

    `total` includes ALL outcomes — fired + skipped + diagnostic
    (timeout/unavailable/error). Without this, a window where the only
    events are timeouts would render as "no events recorded," hiding a
    real availability problem from the user. Codex 2026-05-05 P2.
    """
    other_total = sum(report.other_outcomes.values())
    grand_total = report.fired_count + report.skipped_count + other_total
    has_cross_source = bool(report.mcp_calls or report.tool_calls_other or report.routing_coverage)
    # Only fully bail when there's NOTHING to report — including no
    # cross-source tool calls. A user with auto-recall disabled but
    # active Claude Code transcripts should still see the tool-call
    # breakdown. Codex 2026-05-05 P2.
    if grand_total == 0 and not has_cross_source:
        return (
            "brainstack: no auto-recall events recorded in this window.\n"
            "  Enable with: ./install.sh --enable-auto-recall\n"
            "  Or check the runtime log directory for events.log.jsonl"
        )

    coverage_pct = int(100 * report.fired_count / grand_total) if grand_total else 0
    lines: list[str] = []
    window = _format_window(report)
    lines.append(f"brainstack: auto-recall ROI{window}\n")
    if grand_total > 0:
        lines.append(f"  Fired:        {report.fired_count} turns / {grand_total} prompts ({coverage_pct}% coverage)")
    else:
        lines.append("  Fired:        0 (auto-recall disabled or no events in this window)")
    # Only emit auto-recall-specific lines when we have AutoRecall events.
    # Without this guard, a transcript-only run would render meaningless
    # "p50 0ms, 0 docs total" lines.
    if grand_total > 0:
        if report.skipped_count:
            skip_breakdown = ", ".join(
                f"{n} {reason}" for reason, n in sorted(
                    report.skip_reasons.items(), key=lambda kv: -kv[1]
                )
            )
            lines.append(f"  Skipped:      {report.skipped_count} ({skip_breakdown})")
        lines.append(f"  Latency:      p50 {report.latency_p50_ms}ms, p95 {report.latency_p95_ms}ms")
        lines.append(f"  Surfaced:     {report.surfaced_count} docs total"
                     f" (avg {_avg(report.surfaced_count, report.fired_count):.1f} per fire)")
        if report.top_sources:
            sources_str = ", ".join(f"{name} ({n})" for name, n in report.top_sources[:5])
            lines.append(f"  Top sources:  {sources_str}")
        if report.score_distribution:
            dist_str = ", ".join(f"{n} in {b}"
                                  for b, n in sorted(report.score_distribution.items()))
            lines.append(f"  Scores:       {dist_str}")
        if report.other_outcomes:
            diag = ", ".join(f"{n} {kind}" for kind, n in report.other_outcomes.items())
            lines.append(f"  Diagnostics:  {diag}")
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

    if report.routing_coverage and report.routing_coverage.get("system_level_total", 0) + \
       report.routing_coverage.get("code_level_total", 0) > 0:
        lines.append("")
        lines.append("  Coverage check (CLAUDE.md routing rules):")
        rc = report.routing_coverage
        if rc.get("system_level_total", 0):
            pct = int(rc.get("system_level_coverage", 0.0) * 100)
            lines.append(
                f"    System-level questions    : {rc['system_level_total']} detected, "
                f"{rc.get('system_level_notebooklm', 0)} of those triggered notebooklm "
                f"({pct}%)"
            )
        if rc.get("code_level_total", 0):
            pct = int(rc.get("code_level_coverage", 0.0) * 100)
            lines.append(
                f"    Code-level questions      : {rc['code_level_total']} detected, "
                f"{rc.get('code_level_minerva', 0)} of those triggered minerva ({pct}%)"
            )

    lines.append("")
    lines.append("  Without auto-recall, all "
                 f"{report.fired_count} turns would have started with only "
                 "static MEMORY.md as memory context. Auto-recall added "
                 f"{report.surfaced_count} dynamic docs scoped to each prompt.")
    return "\n".join(lines)


def _avg(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


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


# Heuristic regexes for routing coverage. Tunable; will produce false
# positives. The point is signal, not precision.
_SYSTEM_LEVEL_PATTERNS = [
    re.compile(r"\bhow does\b.*\bwork\b", re.IGNORECASE),
    re.compile(r"\bwho owns\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s the difference\b", re.IGNORECASE),
    re.compile(r"\bwalk me through\b", re.IGNORECASE),
    re.compile(r"\bend[- ]to[- ]end\b", re.IGNORECASE),
    re.compile(r"\bhow do .* communicate\b", re.IGNORECASE),
    re.compile(r"\barchitecture\b", re.IGNORECASE),
]
_CODE_LEVEL_PATTERNS = [
    re.compile(r"\bwhere is\b.*\bused\b", re.IGNORECASE),
    re.compile(r"\bwhat repos depend on\b", re.IGNORECASE),
    re.compile(r"\bwhat events does\b.*\bemit\b", re.IGNORECASE),
    re.compile(r"\bblast radius\b", re.IGNORECASE),
    re.compile(r"\bcross[- ]repo\b", re.IGNORECASE),
    re.compile(r"\bwhich repos? (call|use|reference|depend)\b", re.IGNORECASE),
]


def classify_prompt(text: str) -> str | None:
    """Classify a user prompt as system-level, code-level, or neither.

    Returns ``"system-level"``, ``"code-level"``, or ``None``. Heuristic-
    based — regex patterns derived from the CLAUDE.md routing rules.
    Code-level is checked first because it's the more specific category;
    a prompt like "what events does the package service emit" matches
    code-level even though it could also kind-of match "how does X work".
    """
    if not text or not isinstance(text, str):
        return None
    for pat in _CODE_LEVEL_PATTERNS:
        if pat.search(text):
            return "code-level"
    for pat in _SYSTEM_LEVEL_PATTERNS:
        if pat.search(text):
            return "system-level"
    return None


def compute_routing_coverage(
    transcripts_dir: Path | str,
    *,
    since_ts_ms: int | None = None,
) -> dict[str, int | float]:
    """For each session, count user prompts by category and check whether
    the appropriate MCP got called. Returns a dict with totals + coverage
    rates per category.

    Per-session pairing: a "session" is one transcript file. If the user
    asked a system-level question in the session and ``mcp__notebooklm__*``
    was called anywhere in the same session, we count that as covered.
    Imperfect (the call might have been for a different question), but
    the grain is sessions-with-the-pattern, not turn-level pairing.
    """
    root = Path(transcripts_dir)
    if not root.is_dir():
        return _empty_coverage()
    sys_total = 0
    sys_covered = 0
    code_total = 0
    code_covered = 0
    for jsonl in root.rglob("*.jsonl"):
        sys_in_session, code_in_session, n_notebook, n_minerva = _classify_session(
            jsonl, since_ts_ms
        )
        sys_total += sys_in_session
        # Per-session pairing capped at the number of relevant calls:
        # 4 sys-level questions + 2 notebooklm calls → 2 covered, not 4.
        # Reflects "how many qualifying questions actually got the right
        # tool fired alongside them" without trying to do turn-level
        # pairing (which would need ts ordering and is brittle).
        sys_covered += min(sys_in_session, n_notebook)
        code_total += code_in_session
        code_covered += min(code_in_session, n_minerva)
    return {
        "system_level_total": sys_total,
        "system_level_notebooklm": sys_covered,
        "system_level_coverage": round(sys_covered / sys_total, 2) if sys_total else 0.0,
        "code_level_total": code_total,
        "code_level_minerva": code_covered,
        "code_level_coverage": round(code_covered / code_total, 2) if code_total else 0.0,
    }


def _empty_coverage() -> dict[str, int | float]:
    return {
        "system_level_total": 0,
        "system_level_notebooklm": 0,
        "system_level_coverage": 0.0,
        "code_level_total": 0,
        "code_level_minerva": 0,
        "code_level_coverage": 0.0,
    }


def _classify_session(jsonl: Path, since_ts_ms: int | None) -> tuple[int, int, int, int]:
    """Walk one transcript. Return (sys_q_count, code_q_count,
    notebooklm_call_count, minerva_call_count).

    Only USER-role text is classified — without this, assistant
    explanations and tool-result text get counted as user prompts,
    inflating the totals 10x+ on a real transcript (every "the WMS
    architecture works like..." in an assistant reply matches the
    system-level pattern).
    """
    sys_q = 0
    code_q = 0
    notebook = 0
    minerva = 0
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                if since_ts_ms is not None:
                    ts_ms = _parse_iso_to_ms(rec.get("timestamp"))
                    if ts_ms is None or ts_ms < since_ts_ms:
                        continue
                role = rec.get("type")
                msg = rec.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                # Real Claude Code transcripts encode user turns two ways:
                # (a) `content` is a plain string with the prompt text;
                # (b) `content` is a list of blocks (tool_use, text, etc).
                # Without handling (a), routing coverage reports zero
                # qualifying questions even on transcripts with hundreds.
                # Codex 2026-05-05 P2.
                if isinstance(content, str) and role == "user":
                    cls = classify_prompt(content)
                    if cls == "system-level":
                        sys_q += 1
                    elif cls == "code-level":
                        code_q += 1
                    continue
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    # Only count user-authored text as prompts. Assistant
                    # text is the model's reply (which may discuss how X
                    # works), and tool_result is search output — neither
                    # is a user "question."
                    if btype == "text" and role == "user":
                        cls = classify_prompt(block.get("text", ""))
                        if cls == "system-level":
                            sys_q += 1
                        elif cls == "code-level":
                            code_q += 1
                    elif btype == "tool_use":
                        name = block.get("name") or ""
                        if name.startswith("mcp__notebooklm__"):
                            notebook += 1
                        elif name.startswith("mcp__minerva__"):
                            minerva += 1
    except OSError:
        pass
    return sys_q, code_q, notebook, minerva
