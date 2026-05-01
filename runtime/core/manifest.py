"""Manifest schema v1.0 — the runtime's primary artifact (contract; writer in Phase 3).

The manifest is the format the runtime writes after every turn: a deterministic,
machine-readable record of what is in the injected context. This module
defines the schema and the load/dump round-trip; the actual per-turn writer
that consumes events and emits manifests lives in Phase 3 (`runtime/core/budget.py`
and friends).

Key properties:

  - **Versioned.** `schema_version` is required and validated on load.
    Loaders refuse versions they don't recognize. v0.2 ships with "1.0".
  - **Round-trip byte-identical.** `dump_manifest(load_manifest(s)) == s`
    when `s` was produced by `dump_manifest`. Tested in test_manifest.py.
  - **Forward-compatible via `x_*`.** Unknown keys prefixed with `x_` are
    preserved. Unknown non-prefixed keys are rejected (forces explicit
    schema bumps for additions).
  - **Reference-only by default.** Items store `source_path` + `sha256` +
    `token_count`, NOT the raw content. Users opt in to raw capture
    elsewhere; this type never holds payloads.

Tool-specific item fields (e.g., parsed Read.file_path) are TBD pending
sub-phase 0b empirical telemetry. The schema reserves `x_tool_*` fields
for adapter-specific extensions.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"

# Maximum bytes any single x_* extension value may serialize to. Codex security
# persona BLOCK: x_* keys must not become a backdoor for stuffing raw payloads
# into default-on synced logs. Mirrors runtime/core/events.MAX_EXTENSION_BYTES.
MAX_EXTENSION_BYTES = 1024

_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "turn",
    "ts_ms",
    "session_id",
    "budget_total",
    "budget_used",
    "items",
})

_REQUIRED_ITEM_KEYS = frozenset({
    "id",
    "bucket",
    "source_path",
    "sha256",
    "token_count",
    "retrieval_reason",
    "last_touched_turn",
    "pinned",
})

_OPTIONAL_ITEM_KEYS = frozenset({"score", "extensions"})


class SchemaVersionError(ValueError):
    """Raised when a manifest's schema_version is missing or unrecognized."""


@dataclass(frozen=True)
class InjectionItemSnapshot:
    """A single injected-context item as it appeared at a specific turn.

    Reference-only: `source_path` + `sha256` identify the content; the
    actual bytes live wherever the storage layer keeps them. The runtime
    never stores raw content here.

    `score` is the value that some policies (e.g., recency-weighted) use
    for eviction ranking. It is included in the snapshot so replay can
    reconstruct the policy's input deterministically (codex Skeptic
    finding #3: RecencyWeightedPolicy was previously unreplayable from
    manifests because score wasn't snapshotted).

    `extensions` allows per-item x_* fields to round-trip, mirroring the
    top-level Manifest extensions (codex Skeptic finding #4).
    """

    id: str
    bucket: str
    source_path: str
    sha256: str
    token_count: int
    retrieval_reason: str
    last_touched_turn: int
    pinned: bool
    score: float = 0.0
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    """Turn N's snapshot of the injected context."""

    schema_version: str
    turn: int
    ts_ms: int
    session_id: str
    budget_total: int
    budget_used: int
    items: list[InjectionItemSnapshot]
    # Forward-compat passthrough. Keys must start with "x_".
    extensions: dict[str, Any] = field(default_factory=dict)


def _item_to_dict(it: InjectionItemSnapshot) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": it.id,
        "bucket": it.bucket,
        "source_path": it.source_path,
        "sha256": it.sha256,
        "token_count": it.token_count,
        "retrieval_reason": it.retrieval_reason,
        "last_touched_turn": it.last_touched_turn,
        "pinned": it.pinned,
        "score": it.score,
    }
    for k, v in it.extensions.items():
        if not k.startswith("x_"):
            raise ValueError(f"item extension keys must start with 'x_'; got {k!r}")
        encoded_v = json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded_v) > MAX_EXTENSION_BYTES:
            raise ValueError(
                f"item extension {k!r} serializes to {len(encoded_v)} bytes; "
                f"max is {MAX_EXTENSION_BYTES}."
            )
        out[k] = v
    return out


def dump_manifest(m: Manifest) -> str:
    """Serialize to deterministic JSON. Sorted keys, no insignificant whitespace."""
    payload: dict[str, Any] = {
        "schema_version": m.schema_version,
        "turn": m.turn,
        "ts_ms": m.ts_ms,
        "session_id": m.session_id,
        "budget_total": m.budget_total,
        "budget_used": m.budget_used,
        "items": [_item_to_dict(it) for it in m.items],
    }
    for k, v in m.extensions.items():
        if not k.startswith("x_"):
            raise ValueError(f"extension keys must start with 'x_'; got {k!r}")
        encoded_v = json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded_v) > MAX_EXTENSION_BYTES:
            raise ValueError(
                f"manifest extension {k!r} serializes to {len(encoded_v)} bytes; "
                f"max is {MAX_EXTENSION_BYTES}. extensions are metadata, not payload."
            )
        payload[k] = v
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def load_manifest(raw: str | bytes | Mapping[str, Any]) -> Manifest:
    """Parse + validate a manifest. Raises SchemaVersionError or ValueError."""
    if isinstance(raw, (str, bytes)):
        data = json.loads(raw)
    else:
        data = dict(raw)

    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")

    version = data.get("schema_version")
    if version is None:
        raise SchemaVersionError("manifest missing required field 'schema_version'")
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"unsupported schema_version: {version!r} "
            f"(this runtime understands {SCHEMA_VERSION!r})"
        )

    # Required top-level fields
    missing = _ALLOWED_TOP_LEVEL_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"manifest missing required fields: {sorted(missing)}")

    # Reject unknown non-x_ keys (forces explicit schema bump)
    extras: dict[str, Any] = {}
    for k, v in data.items():
        if k in _ALLOWED_TOP_LEVEL_KEYS:
            continue
        if k.startswith("x_"):
            extras[k] = v
            continue
        raise ValueError(f"unknown manifest key {k!r}; non-x_ extensions require a schema_version bump")

    items_raw = data["items"]
    if not isinstance(items_raw, list):
        raise ValueError("manifest 'items' must be a list")
    items: list[InjectionItemSnapshot] = []
    known_item_keys = _REQUIRED_ITEM_KEYS | _OPTIONAL_ITEM_KEYS
    for i, raw_item in enumerate(items_raw):
        if not isinstance(raw_item, dict):
            raise ValueError(f"item {i} must be an object")
        missing_item = _REQUIRED_ITEM_KEYS - set(raw_item.keys())
        if missing_item:
            raise ValueError(f"item {i} missing required fields: {sorted(missing_item)}")
        # Per-item x_*-prefixed extensions are preserved across round-trip
        # (codex Skeptic finding #4: silent drop was a forward-compat hole).
        item_extras: dict[str, Any] = {}
        for k, v in raw_item.items():
            if k in known_item_keys:
                continue
            if k.startswith("x_"):
                item_extras[k] = v
                continue
            raise ValueError(
                f"item {i} unknown key {k!r}; non-x_ extensions require a schema_version bump"
            )
        items.append(InjectionItemSnapshot(
            id=raw_item["id"],
            bucket=raw_item["bucket"],
            source_path=raw_item["source_path"],
            sha256=raw_item["sha256"],
            token_count=int(raw_item["token_count"]),
            retrieval_reason=raw_item["retrieval_reason"],
            last_touched_turn=int(raw_item["last_touched_turn"]),
            pinned=bool(raw_item["pinned"]),
            score=float(raw_item.get("score", 0.0)),
            extensions=item_extras,
        ))

    return Manifest(
        schema_version=version,
        turn=int(data["turn"]),
        ts_ms=int(data["ts_ms"]),
        session_id=str(data["session_id"]),
        budget_total=int(data["budget_total"]),
        budget_used=int(data["budget_used"]),
        items=items,
        extensions=extras,
    )


__all__ = [
    "InjectionItemSnapshot",
    "Manifest",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "dump_manifest",
    "load_manifest",
]
