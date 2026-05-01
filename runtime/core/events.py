"""Event log schema v1.0 — the per-hook record stream (contract; writer in Phase 4).

Each hook firing produces one EventRecord. Records are appended to a JSONL
log (`runtime/.../events.log.jsonl`) under the same flock pattern as the
empirical harness (sub-phase 0c). When phases 3+4 are complete, the log
becomes the substrate the manifest writer reads from and the substrate
`recall runtime replay` will reconstruct from. Phase 1 ships the schema +
load/dump + atomic append; the wiring lives in the adapter (Phase 4).

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
    """Reference-only summary of a tool's output.

    Security default (v0.2): the sha256 field is **empty by default** because
    a stable hash of secret-bearing output is a stable fingerprint that an
    attacker with a breach DB could correlate against. Callers opt in to
    sha256 at the surface that constructs the OutputSummary (e.g., via
    `summarize_output(text, include_hash=True)` or by calling sha256_of()
    explicitly). byte_len is always populated.

    The codex security persona BLOCK on this surface drove the change. v0.x
    will add an HMAC-keyed alternative for users who want correlation
    without the fingerprint risk.
    """

    sha256: str
    byte_len: int


@dataclass(frozen=True)
class EventRecord:
    """One hook firing.

    The natural key is `(session_id, turn, ts_ms, event)`, but consumers
    often need a single string id (replay correlation, raw-payload pairing
    when capture_raw=true). The `event_id` field is that single id; if the
    caller doesn't supply one, the runtime computes a deterministic id from
    the natural key via `event_id_for(...)`.
    """

    schema_version: str
    ts_ms: int
    event: str
    session_id: str
    turn: int
    event_id: str = ""  # auto-derived if empty; see event_id_for()
    tool_name: str = ""
    tool_input_keys: list[str] = field(default_factory=list)
    tool_output_summary: OutputSummary | None = None
    bucket: str = ""
    item_ids_added: list[str] = field(default_factory=list)
    item_ids_evicted: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)


def event_id_for(session_id: str, turn: int, ts_ms: int, event: str, nonce: str = "") -> str:
    """Deterministic event id derived from the natural key.

    Returns a 16-char hex digest. Stable across runs given the same inputs.
    The optional `nonce` is for the rare case of multiple events with the
    same (session, turn, ts_ms, event); the adapter passes a sequence
    counter or pid+random-bytes there.
    """
    payload = f"{session_id}|{turn}|{ts_ms}|{event}|{nonce}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def summarize_output(text: str, *, include_hash: bool = False) -> OutputSummary:
    """Build an OutputSummary from raw text. The raw bytes are NOT retained.

    Security default: sha256 is empty unless `include_hash=True`. Hashing
    secret-bearing output produces a stable fingerprint that an attacker
    with a breach DB could correlate against. Callers who want correlation
    (e.g., a debug session) opt in explicitly.
    """
    encoded = text.encode("utf-8")
    return OutputSummary(
        sha256=(hashlib.sha256(encoded).hexdigest() if include_hash else ""),
        byte_len=len(encoded),
    )


# Maximum bytes any single x_* extension value may serialize to. Codex security
# persona BLOCK: x_* keys must not become a backdoor for stuffing raw payloads
# into default-on synced logs. 1 KiB is enough for legitimate metadata
# (small flags, identifiers, structured booleans) and will reject anything
# accidentally pushed into x_full_payload-shaped fields.
MAX_EXTENSION_BYTES = 1024


def _validate_extension_size(key: str, value: Any) -> None:
    """Reject extension values larger than MAX_EXTENSION_BYTES bytes.

    Raised at dump time to prevent the documented data policy from being
    silently bypassed by an adapter that stuffs raw content into x_*."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EXTENSION_BYTES:
        raise ValueError(
            f"extension {key!r} serializes to {len(encoded)} bytes; "
            f"max is {MAX_EXTENSION_BYTES}. extensions are metadata, not payload."
        )


def _event_to_dict(e: EventRecord) -> dict[str, Any]:
    # Sort the order-sensitive fields at dump time so deterministic output
    # is enforced regardless of how the caller built them. tool_input_keys is
    # documented as "sorted top-level key names" but if a caller passes an
    # unsorted list, we fix it here. item_ids_added is order-sensitive
    # because eviction order matters for replay; that one is preserved as-is.
    eid = e.event_id or event_id_for(e.session_id, e.turn, e.ts_ms, e.event)
    out: dict[str, Any] = {
        "schema_version": e.schema_version,
        "ts_ms": e.ts_ms,
        "event": e.event,
        "event_id": eid,
        "session_id": e.session_id,
        "turn": e.turn,
        "tool_name": e.tool_name,
        "tool_input_keys": sorted(e.tool_input_keys),
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
        _validate_extension_size(k, v)
        out[k] = v
    return out


def dump_event(e: EventRecord) -> str:
    return json.dumps(_event_to_dict(e), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


_REQUIRED_KEYS = frozenset({
    "schema_version", "ts_ms", "event", "session_id", "turn",
})

# Optional fields that the loader recognizes. Anything outside this set or the
# x_*-prefixed extension space is rejected (forces explicit schema bumps,
# matches manifest.py's discipline).
_OPTIONAL_KEYS = frozenset({
    "event_id",
    "tool_name", "tool_input_keys", "tool_output_summary",
    "bucket", "item_ids_added", "item_ids_evicted",
})

_KNOWN_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS


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

    # Reject unknown non-x_ keys (mirror manifest.py's discipline; codex
    # review caught the inconsistency).
    for k in data.keys():
        if k in _KNOWN_KEYS:
            continue
        if k.startswith("x_"):
            continue
        raise ValueError(
            f"unknown event key {k!r}; non-x_ extensions require a schema_version bump"
        )

    summary_raw = data.get("tool_output_summary")
    if summary_raw is None:
        summary: OutputSummary | None = None
    elif isinstance(summary_raw, dict):
        summary = OutputSummary(sha256=summary_raw["sha256"], byte_len=int(summary_raw["byte_len"]))
    else:
        raise ValueError("tool_output_summary must be an object or null")

    extras = {k: v for k, v in data.items() if k.startswith("x_")}

    # event_id is auto-derived from the natural key if absent. This keeps
    # round-trip symmetry: dump always emits event_id, load always returns
    # a record with event_id populated.
    raw_event_id = str(data.get("event_id", ""))
    if not raw_event_id:
        raw_event_id = event_id_for(
            str(data["session_id"]), int(data["turn"]),
            int(data["ts_ms"]), str(data["event"]),
        )
    return EventRecord(
        schema_version=data["schema_version"],
        ts_ms=int(data["ts_ms"]),
        event=str(data["event"]),
        event_id=raw_event_id,
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
    "event_id_for",
    "load_event",
    "load_events",
    "summarize_output",
]
