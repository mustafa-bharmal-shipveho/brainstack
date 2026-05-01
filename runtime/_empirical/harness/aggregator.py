#!/usr/bin/env python3
"""Aggregate hook telemetry from runtime/_empirical/harness/_data/events.jsonl.

Computes deliverability per event type, tool, and run_tag. Output is a markdown
table + JSON summary, both written to stdout. No external deps beyond stdlib.

Usage:
    python3 aggregator.py [--data-dir _data] [--expected expected_runs.json]

`expected_runs.json` (optional) tells the aggregator which sessions/runs were
fired and which event types each was supposed to produce. Without it, the
aggregator computes only observed counts; deliverability % requires expected.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_events(data_dir: Path) -> list[dict]:
    p = data_dir / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Corrupted line — concurrent-write bug if this happens.
            print(f"[aggregator] WARN corrupted line: {line[:120]}", file=sys.stderr)
    return out


def load_expected(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(events: list[dict], expected: dict) -> dict:
    by_event = Counter(e["event"] for e in events)
    by_tool = Counter((e["event"], e["tool_name"]) for e in events if e.get("tool_name"))
    by_run = defaultdict(lambda: Counter())
    sessions_seen = set()
    bytes_by_event = defaultdict(int)
    for e in events:
        if e.get("run_tag"):
            by_run[e["run_tag"]][e["event"]] += 1
        if e.get("session_id"):
            sessions_seen.add(e["session_id"])
        bytes_by_event[e["event"]] += int(e.get("payload_bytes", 0))

    # Deliverability: compare observed vs expected per (run_tag, event)
    deliverability: dict[str, dict] = {}
    if expected:
        for tag, expected_events in expected.get("runs", {}).items():
            observed = by_run.get(tag, Counter())
            for ev, want in expected_events.items():
                got = observed.get(ev, 0)
                bucket = deliverability.setdefault(ev, {"want": 0, "got": 0, "runs": 0})
                bucket["want"] += want
                bucket["got"] += got
                bucket["runs"] += 1

    return {
        "events_observed_total": sum(by_event.values()),
        "by_event": dict(by_event),
        "by_event_tool": {f"{k[0]}:{k[1]}": v for k, v in by_tool.items()},
        "sessions_seen": sorted(sessions_seen),
        "n_sessions": len(sessions_seen),
        "bytes_by_event": dict(bytes_by_event),
        "deliverability": deliverability,
    }


def render_markdown(summary: dict) -> str:
    lines: list[str] = []
    lines.append("# Hook telemetry summary")
    lines.append("")
    lines.append(f"- Total events observed: **{summary['events_observed_total']}**")
    lines.append(f"- Distinct sessions: **{summary['n_sessions']}**")
    lines.append("")
    lines.append("## Events by type")
    lines.append("")
    lines.append("| event | count | total bytes |")
    lines.append("|---|---|---|")
    for ev, n in sorted(summary["by_event"].items(), key=lambda x: -x[1]):
        b = summary["bytes_by_event"].get(ev, 0)
        lines.append(f"| {ev} | {n} | {b} |")
    if summary["by_event_tool"]:
        lines.append("")
        lines.append("## PostToolUse / PreToolUse by tool")
        lines.append("")
        lines.append("| event:tool | count |")
        lines.append("|---|---|")
        for k, n in sorted(summary["by_event_tool"].items(), key=lambda x: -x[1]):
            lines.append(f"| {k} | {n} |")
    if summary["deliverability"]:
        lines.append("")
        lines.append("## Deliverability vs expected")
        lines.append("")
        lines.append("| event | got | want | rate |")
        lines.append("|---|---|---|---|")
        for ev, d in sorted(summary["deliverability"].items()):
            rate = (d["got"] / d["want"] * 100) if d["want"] else 0
            lines.append(f"| {ev} | {d['got']} | {d['want']} | {rate:.1f}% |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="_data")
    ap.add_argument("--expected", default=None)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    data_dir = (here / args.data_dir).resolve()
    expected_path = (here / args.expected).resolve() if args.expected else None

    events = load_events(data_dir)
    expected = load_expected(expected_path) if expected_path else {}
    summary = summarize(events, expected)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_markdown(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
