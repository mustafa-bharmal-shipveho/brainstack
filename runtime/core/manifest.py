"""Manifest schema v1.0 — the runtime's primary artifact.

The manifest is what the runtime writes after every turn: a deterministic,
machine-readable record of what is in the injected context. It is the
contract between runtime and consumers (replay, audit, CLI).

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


class SchemaVersionError(ValueError):
    """Raised when a manifest's schema_version is missing or unrecognized."""


@dataclass(frozen=True)
class InjectionItemSnapshot:
    """A single injected-context item as it appeared at a specific turn.

    Reference-only: `source_path` + `sha256` identify the content; the
    actual bytes live wherever the storage layer keeps them. The runtime
    never stores raw content here.
    """

    id: str
    bucket: str
    source_path: str
    sha256: str
    token_count: int
    retrieval_reason: str
    last_touched_turn: int
    pinned: bool


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


def dump_manifest(m: Manifest) -> str:
    """Serialize to deterministic JSON. Sorted keys, no insignificant whitespace."""
    payload: dict[str, Any] = {
        "schema_version": m.schema_version,
        "turn": m.turn,
        "ts_ms": m.ts_ms,
        "session_id": m.session_id,
        "budget_total": m.budget_total,
        "budget_used": m.budget_used,
        "items": [asdict(it) for it in m.items],
    }
    for k, v in m.extensions.items():
        if not k.startswith("x_"):
            raise ValueError(f"extension keys must start with 'x_'; got {k!r}")
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
    for i, raw_item in enumerate(items_raw):
        if not isinstance(raw_item, dict):
            raise ValueError(f"item {i} must be an object")
        missing_item = _REQUIRED_ITEM_KEYS - set(raw_item.keys())
        if missing_item:
            raise ValueError(f"item {i} missing required fields: {sorted(missing_item)}")
        items.append(InjectionItemSnapshot(
            id=raw_item["id"],
            bucket=raw_item["bucket"],
            source_path=raw_item["source_path"],
            sha256=raw_item["sha256"],
            token_count=int(raw_item["token_count"]),
            retrieval_reason=raw_item["retrieval_reason"],
            last_touched_turn=int(raw_item["last_touched_turn"]),
            pinned=bool(raw_item["pinned"]),
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
