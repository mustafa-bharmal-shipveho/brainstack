"""Event log schema v1.0 — the per-hook record stream.

Each hook firing produces one EventRecord. Records are appended to a JSONL
log (`runtime/.../events.log.jsonl`) under the same flock pattern as the
empirical harness (sub-phase 0c). The log is what the manifest is built
from and what `recall runtime replay` reads.

Data policy (codex review fix):

  - `tool_input_keys` is a sorted list of TOP-LEVEL key names from the tool
    input. The VALUES are never recorded.
  - `tool_output_summary` is {sha256, byte_len}. The OUTPUT TEXT is never
    recorded here.
  - Any raw-content capture is opt-in and goes to a separate file (the
    harness's payload-samples.jsonl, or a runtime-side flag set explicitly).

Schema is versioned; loaders reject unknown versions. Forward-compat keys
under `x_*` prefix are preserved across round-trips.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

EVENT_LOG_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class OutputSummary:
    """Reference-only summary of a tool's output. No raw text."""

    sha256: str
    byte_len: int


@dataclass(frozen=True)
class EventRecord:
    """One hook firing."""

    schema_version: str
    ts_ms: int
    event: str
    session_id: str
    turn: int
    tool_name: str = ""
    tool_input_keys: list[str] = field(default_factory=list)
    tool_output_summary: OutputSummary | None = None
    bucket: str = ""
    item_ids_added: list[str] = field(default_factory=list)
    item_ids_evicted: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)


def summarize_output(text: str) -> OutputSummary:
    """Build an OutputSummary from raw text. The raw bytes are NOT retained."""
    encoded = text.encode("utf-8")
    return OutputSummary(
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_len=len(encoded),
    )


def _event_to_dict(e: EventRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": e.schema_version,
        "ts_ms": e.ts_ms,
        "event": e.event,
        "session_id": e.session_id,
        "turn": e.turn,
        "tool_name": e.tool_name,
        "tool_input_keys": list(e.tool_input_keys),
        "bucket": e.bucket,
        "item_ids_added": list(e.item_ids_added),
        "item_ids_evicted": list(e.item_ids_evicted),
    }
    if e.tool_output_summary is not None:
        out["tool_output_summary"] = asdict(e.tool_output_summary)
    else:
        out["tool_output_summary"] = None
    for k, v in e.extensions.items():
        if not k.startswith("x_"):
            raise ValueError(f"event extension keys must start with 'x_'; got {k!r}")
        out[k] = v
    return out


def dump_event(e: EventRecord) -> str:
    return json.dumps(_event_to_dict(e), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


_REQUIRED_KEYS = frozenset({
    "schema_version", "ts_ms", "event", "session_id", "turn",
})


def load_event(raw: str | bytes | Mapping[str, Any]) -> EventRecord:
    if isinstance(raw, (str, bytes)):
        data = json.loads(raw)
    else:
        data = dict(raw)

    if not isinstance(data, dict):
        raise ValueError("event must be a JSON object")
    if data.get("schema_version") != EVENT_LOG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported event schema_version: {data.get('schema_version')!r} "
            f"(this runtime understands {EVENT_LOG_SCHEMA_VERSION!r})"
        )
    missing = _REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"event missing required fields: {sorted(missing)}")

    summary_raw = data.get("tool_output_summary")
    if summary_raw is None:
        summary: OutputSummary | None = None
    elif isinstance(summary_raw, dict):
        summary = OutputSummary(sha256=summary_raw["sha256"], byte_len=int(summary_raw["byte_len"]))
    else:
        raise ValueError("tool_output_summary must be an object or null")

    extras = {k: v for k, v in data.items() if k.startswith("x_")}

    return EventRecord(
        schema_version=data["schema_version"],
        ts_ms=int(data["ts_ms"]),
        event=str(data["event"]),
        session_id=str(data["session_id"]),
        turn=int(data["turn"]),
        tool_name=str(data.get("tool_name", "")),
        tool_input_keys=list(data.get("tool_input_keys", [])),
        tool_output_summary=summary,
        bucket=str(data.get("bucket", "")),
        item_ids_added=list(data.get("item_ids_added", [])),
        item_ids_evicted=list(data.get("item_ids_evicted", [])),
        extensions=extras,
    )


def append_event(log_path: Path | str, event: EventRecord) -> None:
    """Atomic append to the JSONL log, flock-guarded.

    Sentinel pattern: lock a sibling `.lock` file, not the log itself. This
    is the same lesson encoded by brainstack's _atomic.py (don't lock the
    data file, lock a sentinel)."""
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.parent / f".{p.name}.lock"
    lock.touch(exist_ok=True)
    line = dump_event(event) + "\n"
    with lock.open("a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def load_events(log_path: Path | str) -> list[EventRecord]:
    """Read every event from the log, skipping blank lines."""
    p = Path(log_path)
    if not p.exists():
        return []
    out: list[EventRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(load_event(line))
    return out


__all__ = [
    "EVENT_LOG_SCHEMA_VERSION",
    "EventRecord",
    "OutputSummary",
    "append_event",
    "dump_event",
    "load_event",
    "load_events",
    "summarize_output",
]
