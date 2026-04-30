"""Codex CLI adapter for the migrate dispatcher.

Ingests OpenAI Codex CLI session rollouts + command history into the
brain's episodic stream under a per-tool `codex` namespace at
`<dst>/memory/episodic/codex/AGENT_LEARNINGS.jsonl`.

Source layout (real shape, verified on disk):
    ~/.codex/
        sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl
            One JSON-line per event. Each line: {type, timestamp, payload}.
            Common types: session_meta, event_msg, response_item, …
        history.jsonl
            One JSON-line per command-history entry: {session_id, text, ts}.
        config.toml, state_*.sqlite, models_cache.json
            Skipped — not memory.

Each rollout event becomes one brainstack episode. The `origin` field
(per v0.3 episodic schema) is `codex.cli.<event-type>` so cluster.py
groups them separately from coding.tool_call episodes.

Idempotency
-----------

A sidecar at `<dst>/memory/episodic/codex/_imported.jsonl` records the
SHA256 of every imported source file plus its absolute path and import
timestamp. Re-running migrate skips files whose hash is already in the
sidecar — so adding a new rollout to the source between runs only
imports the new file, not the whole 66MB tree again.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import ClassVar, Iterable, Optional

_HERE = Path(__file__).resolve().parent
_BASE = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_BASE / "memory"))

from _atomic import atomic_write_text  # noqa: E402
from migrate_dispatcher import (  # noqa: E402
    AdapterRegistrationError,
    MigrationResult,
    register_adapter,
    registered_adapters,
)


_TARGET_NAMESPACE = "codex"
_EPISODIC_REL = Path("episodic") / _TARGET_NAMESPACE / "AGENT_LEARNINGS.jsonl"
_SIDECAR_REL = Path("episodic") / _TARGET_NAMESPACE / "_imported.jsonl"


def _file_hash(path: Path) -> str:
    """SHA256 of file contents — keys the idempotency sidecar."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_sidecar(sidecar_path: Path) -> dict[str, int]:
    """Map of `file_path -> byte offset already imported up to`.

    Per codex review P2: append-only Codex files (especially `history.jsonl`)
    grow between migrations. A whole-file hash key fails to dedupe — the
    file's hash changes when a single line is appended, and the entire
    prior content gets re-imported. Tracking byte offset lets each re-run
    pick up where the previous one left off.

    Returns empty dict if the sidecar is missing or unparseable.
    """
    seen: dict[str, int] = {}
    if not sidecar_path.is_file():
        return seen
    try:
        for line in sidecar_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = row.get("file_path")
            offset = row.get("offset", 0)
            if isinstance(path, str) and isinstance(offset, int):
                # Last write wins — sidecar is append-only, latest entry
                # for a path reflects the highest offset imported.
                seen[path] = max(seen.get(path, 0), offset)
    except OSError:
        pass
    return seen


def _append_sidecar(sidecar_path: Path, entries: list[dict]) -> None:
    """Atomically merge new entries into the sidecar."""
    if not entries:
        return
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if sidecar_path.is_file():
        try:
            existing = sidecar_path.read_text()
        except OSError:
            existing = ""
    new = existing + "".join(json.dumps(e) + "\n" for e in entries)
    atomic_write_text(sidecar_path, new)


def _parse_rollout(path: Path, start_offset: int = 0) -> tuple[list[dict], int, list[str]]:
    """Parse a rollout JSONL from `start_offset` byte to EOF.

    Returns (episodes, end_offset, warnings). `end_offset` is the byte
    position after the last fully-parsed line — store this in the
    sidecar to resume on next run. A trailing partial line (mid-write)
    is left for the next run by NOT advancing past it.
    """
    episodes: list[dict] = []
    warnings: list[str] = []
    try:
        with path.open("rb") as f:
            f.seek(start_offset)
            data = f.read()
    except OSError as e:
        warnings.append(f"could not read {path}: {e}")
        return episodes, start_offset, warnings
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        warnings.append(f"non-utf8 content in {path}: {e}")
        return episodes, start_offset, warnings

    # Split on '\n', keeping a partial trailing line for next time.
    lines = text.split("\n")
    if text and not text.endswith("\n"):
        # Last segment is partial — skip it and don't advance past it.
        partial = lines[-1]
        lines = lines[:-1]
        consumed_bytes = len(text) - len(partial.encode("utf-8"))
    else:
        consumed_bytes = len(text)

    line_no = 0
    for line in lines:
        line_no += 1
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"skipped malformed line {path}:line+{line_no}")
            continue
        episodes.append(_rollout_to_episode(obj, source=path))
    return episodes, start_offset + consumed_bytes, warnings


def _rollout_to_episode(rollout: dict, source: Path) -> dict:
    """Map one rollout JSON line to a brainstack episode dict."""
    event_type = str(rollout.get("type", "unknown"))
    timestamp = rollout.get("timestamp") or datetime.datetime.utcnow().isoformat()
    payload = rollout.get("payload", {})
    detail = json.dumps(payload, ensure_ascii=False) if payload else ""
    # Derive a short summary for the dream-cycle clusterer (v0.3 contract).
    summary_seed = ""
    if isinstance(payload, dict):
        for k in ("content", "text", "type", "id"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                summary_seed = v
                break
    summary = (summary_seed or f"codex {event_type}")[:120]
    return {
        "timestamp": timestamp,
        "skill": "codex-cli",
        "action": f"codex {event_type}",
        "result": "captured",
        "detail": detail[:4000],  # cap to avoid runaway sizes
        "pain_score": 0,
        "importance": 5,
        "reflection": "",
        "confidence": 0.7,
        "source": {"adapter": "codex-cli", "rollout_file": source.name},
        "evidence_ids": [],
        "origin": f"codex.cli.{event_type}",
        "summary": summary,
    }


def _ts_to_iso(ts_raw) -> str:
    """Convert a Codex history timestamp to ISO 8601 UTC.

    Per codex review P1: Codex CLI's `history.jsonl` stores `ts` in
    unix-SECONDS, not unix-milliseconds. The earlier `ts / 1000` divide
    produced 1970-era timestamps — chronological ordering broken AND
    decay-cutoff filters drop them as ancient.

    Heuristic: a 13-digit timestamp is ms; a 10-digit timestamp is
    seconds. Anything else falls back to "now". Reasonable for any
    timestamp between 2001-09-09 and 5138-11-16.
    """
    if not isinstance(ts_raw, (int, float)):
        return str(ts_raw) if ts_raw else datetime.datetime.utcnow().isoformat()
    ts = float(ts_raw)
    # 1e12 = 2001 in ms, ~33000 in s — easy disambiguator.
    if ts >= 1e12:
        ts /= 1000.0  # ms → s
    try:
        return datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc
        ).isoformat()
    except (OSError, OverflowError, ValueError):
        return str(ts_raw)


def _parse_history(path: Path, start_offset: int = 0) -> tuple[list[dict], int, list[str]]:
    """Parse history.jsonl from `start_offset` to EOF.

    Returns (episodes, end_offset, warnings). Same offset-tracking
    contract as `_parse_rollout` so re-runs only import newly-appended
    history lines (codex review P2).
    """
    episodes: list[dict] = []
    warnings: list[str] = []
    try:
        with path.open("rb") as f:
            f.seek(start_offset)
            data = f.read()
    except OSError as e:
        warnings.append(f"could not read {path}: {e}")
        return episodes, start_offset, warnings
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        warnings.append(f"non-utf8 history content: {e}")
        return episodes, start_offset, warnings

    lines = text.split("\n")
    if text and not text.endswith("\n"):
        partial = lines[-1]
        lines = lines[:-1]
        consumed_bytes = len(text) - len(partial.encode("utf-8"))
    else:
        consumed_bytes = len(text)

    line_no = 0
    for line in lines:
        line_no += 1
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"skipped malformed history line+{line_no}")
            continue
        ts_iso = _ts_to_iso(obj.get("ts"))
        text_field = str(obj.get("text", ""))
        episodes.append({
            "timestamp": ts_iso,
            "skill": "codex-cli",
            "action": "codex history command",
            "result": "captured",
            "detail": text_field[:4000],
            "pain_score": 0,
            "importance": 4,
            "reflection": "",
            "confidence": 0.7,
            "source": {
                "adapter": "codex-cli",
                "history": True,
                "session_id": obj.get("session_id"),
            },
            "evidence_ids": [],
            "origin": "codex.cli.history",
            "summary": text_field[:120],
        })
    return episodes, start_offset + consumed_bytes, warnings


def _enumerate_rollouts(src: Path) -> list[Path]:
    """Sorted list of `*.jsonl` rollout files under sessions/."""
    sessions_dir = src / "sessions"
    if not sessions_dir.is_dir():
        return []
    files = []
    for p in sessions_dir.rglob("rollout-*.jsonl"):
        if p.is_symlink():
            continue
        if not p.is_file():
            continue
        files.append(p)
    files.sort()
    return files


class CodexCliAdapter:
    """Migrates Codex CLI rollouts + history into the brain's episodic
    stream under namespace=`codex`."""

    name = "codex-cli"
    supported_formats: ClassVar[frozenset[str]] = frozenset({"codex-cli"})

    def supports(self, fmt: str) -> bool:
        return fmt in self.supported_formats

    def migrate(
        self,
        src: Path,
        dst: Path,
        dry_run: bool,
        options: Optional[dict] = None,
    ) -> MigrationResult:
        options = options or {}
        namespace = options.get("namespace", _TARGET_NAMESPACE)

        rollouts = _enumerate_rollouts(src)
        history_path = src / "history.jsonl"
        has_history = history_path.is_file() and not history_path.is_symlink()

        episodic_path = dst / "memory" / _EPISODIC_REL
        sidecar_path = dst / "memory" / _SIDECAR_REL
        # offset map: per-file byte offset already imported. Treats append-only
        # sources correctly (codex review P2).
        seen_offsets = _read_sidecar(sidecar_path)

        def _now() -> str:
            return datetime.datetime.now(datetime.timezone.utc).isoformat()

        # ---- Plan (dry-run) ----
        if dry_run:
            episodes_planned = 0
            rollouts_to_import = 0
            warnings: list[str] = []
            for path in rollouts:
                key = str(path)
                start = seen_offsets.get(key, 0)
                try:
                    size = path.stat().st_size
                except OSError as e:
                    warnings.append(f"stat failed {path}: {e}")
                    continue
                if start >= size:
                    continue  # file unchanged since last import
                events, _new_off, w = _parse_rollout(path, start_offset=start)
                if events:
                    rollouts_to_import += 1
                    episodes_planned += len(events)
                warnings.extend(w)
            history_to_import = 0
            if has_history:
                key = str(history_path)
                start = seen_offsets.get(key, 0)
                try:
                    size = history_path.stat().st_size
                except OSError as e:
                    warnings.append(f"stat failed {history_path}: {e}")
                    size = start  # treat as unchanged
                if size > start:
                    events, _new_off, w = _parse_history(history_path, start_offset=start)
                    if events:
                        history_to_import = 1
                        episodes_planned += len(events)
                    warnings.extend(w)
            return MigrationResult(
                format="codex-cli",
                files_written=0,
                files_planned=rollouts_to_import + history_to_import,
                warnings=warnings,
                dry_run=True,
                namespace=namespace,
                source_path=src,
                tool_specific={
                    "episodes_planned": episodes_planned,
                    "rollouts_planned": rollouts_to_import,
                    "history_planned": history_to_import,
                },
            )

        # ---- Execute ----
        all_episodes: list[dict] = []
        new_sidecar: list[dict] = []
        warnings = []
        rollouts_imported = 0
        for path in rollouts:
            key = str(path)
            start = seen_offsets.get(key, 0)
            try:
                size = path.stat().st_size
            except OSError as e:
                warnings.append(f"stat failed {path}: {e}")
                continue
            if start >= size:
                continue  # nothing new
            events, end_offset, w = _parse_rollout(path, start_offset=start)
            warnings.extend(w)
            if not events:
                # Stat said there was new data but we parsed nothing — still
                # advance the offset to skip whitespace/empty lines.
                if end_offset > start:
                    new_sidecar.append({
                        "file_path": key,
                        "offset": end_offset,
                        "imported_at": _now(),
                        "kind": "rollout",
                    })
                continue
            all_episodes.extend(events)
            new_sidecar.append({
                "file_path": key,
                "offset": end_offset,
                "imported_at": _now(),
                "kind": "rollout",
            })
            rollouts_imported += 1

        history_imported = 0
        if has_history:
            key = str(history_path)
            start = seen_offsets.get(key, 0)
            try:
                size = history_path.stat().st_size
            except OSError as e:
                warnings.append(f"stat failed {history_path}: {e}")
                size = start
            if size > start:
                events, end_offset, w = _parse_history(history_path, start_offset=start)
                warnings.extend(w)
                if events:
                    all_episodes.extend(events)
                    history_imported = 1
                new_sidecar.append({
                    "file_path": key,
                    "offset": end_offset,
                    "imported_at": _now(),
                    "kind": "history",
                })

        # Append episodes to the codex episodic JSONL.
        if all_episodes:
            episodic_path.parent.mkdir(parents=True, exist_ok=True)
            existing_text = ""
            if episodic_path.is_file():
                try:
                    existing_text = episodic_path.read_text()
                except OSError:
                    existing_text = ""
            new_text = existing_text + "".join(
                json.dumps(e, ensure_ascii=False) + "\n" for e in all_episodes
            )
            atomic_write_text(episodic_path, new_text)

        _append_sidecar(sidecar_path, new_sidecar)

        return MigrationResult(
            format="codex-cli",
            files_written=rollouts_imported + history_imported,
            files_planned=rollouts_imported + history_imported,
            warnings=warnings,
            dry_run=False,
            namespace=namespace,
            source_path=src,
            tool_specific={
                "episodes_imported": len(all_episodes),
                "rollouts_imported": rollouts_imported,
                "history_imported": history_imported,
            },
        )


def _register_once() -> None:
    if "codex-cli" in registered_adapters():
        return
    try:
        register_adapter(CodexCliAdapter())
    except AdapterRegistrationError:
        pass


_register_once()
