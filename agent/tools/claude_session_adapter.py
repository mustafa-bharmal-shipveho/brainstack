#!/usr/bin/env python3
"""Claude Code session-transcript adapter for brainstack.

Ingests `~/.claude/projects/<slug>/<sessionUUID>.jsonl` transcripts into the
brain's episodic stream under the `claude-sessions` namespace at
`<brain>/memory/episodic/claude-sessions/AGENT_LEARNINGS.jsonl`.

Source layout:
    ~/.claude/projects/<slug>/<uuid>.jsonl
        One JSON-line per event. Event types include:
          user            - user prompt or tool_result content
          assistant       - assistant response (may contain tool_use blocks)
          system          - system events
          permission-mode - permission state changes (skipped)
          file-history-snapshot - file backup snapshots (skipped)
          attachment      - paste-cache references (skipped)
          queue-operation - internal scheduling (skipped)
          last-prompt     - last-prompt cache (skipped)

Each (tool_use, tool_result) pair becomes one brainstack episode keyed by
`origin = claude.session.<tool_name>` so cluster.py groups them separately
from live-hook captures and codex sessions.

Idempotency
-----------

A sidecar at `<brain>/memory/episodic/claude-sessions/_imported.jsonl`
records SHA256 of every imported source file. Re-running skips files whose
hash is in the sidecar. Sessions are append-only-during-write but
sealed-once-closed; whole-file hash is sufficient (no byte-offset
required like codex history.jsonl).

Redaction
---------

Every emitted episode's `detail` field passes through `redact.py` patterns
before write. The `detail` is also truncated to 2 KB to bound output size.

CLI
---

    claude_session_adapter.py [--source DIR] [--dst BRAIN] [--dry-run] [--limit N]

  --source DIR   Default: ~/.claude/projects
  --dst BRAIN    Default: ~/.agent
  --dry-run      Scan + count + redaction-hits, write nothing.
  --limit N      Process at most N session files (for testing).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Iterator, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "memory"))

from _atomic import atomic_write_text  # noqa: E402

# Redaction: re-use redact_jsonl.py's `redact_string()` which itself wraps the
# pattern set from redact.py (BUILTIN_PATTERNS + MULTILINE_PATTERNS). Keeps a
# single source of truth for what counts as a secret.
try:
    from redact import BUILTIN_PATTERNS, MULTILINE_PATTERNS  # noqa: E402
    from redact_jsonl import redact_string  # noqa: E402
    _REDACT_PATTERNS = BUILTIN_PATTERNS  # MULTILINE used internally by redact_string
    _REDACT_AVAILABLE = True
except ImportError:
    _REDACT_PATTERNS = []
    redact_string = None  # type: ignore
    _REDACT_AVAILABLE = False


_NAMESPACE = "claude-sessions"
_EPISODIC_REL = Path("episodic") / _NAMESPACE / "AGENT_LEARNINGS.jsonl"
_SIDECAR_REL = Path("episodic") / _NAMESPACE / "_imported.jsonl"

_DETAIL_CAP = 2048
_SKIP_EVENT_TYPES = frozenset({
    "permission-mode",
    "file-history-snapshot",
    "attachment",
    "queue-operation",
    "last-prompt",
})
_LOW_SIGNAL_TOOLS = frozenset({"Read", "Glob"})


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_sidecar(sidecar_path: Path) -> set[str]:
    seen: set[str] = set()
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
            sha = row.get("sha256")
            if isinstance(sha, str):
                seen.add(sha)
    except OSError:
        pass
    return seen


def _append_sidecar(sidecar_path: Path, entries: list[dict]) -> None:
    if not entries:
        return
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    existing = sidecar_path.read_text() if sidecar_path.is_file() else ""
    new = existing + "".join(json.dumps(e) + "\n" for e in entries)
    atomic_write_text(sidecar_path, new)


def _redact(s: str) -> tuple[str, int]:
    """Apply redaction. Returns (redacted_text, n_hits).

    Wraps `redact_jsonl.redact_string` — single source of truth for the
    pattern set. Falls back to identity if redact_jsonl is unavailable.
    """
    if not s or not _REDACT_AVAILABLE:
        return s, 0
    try:
        new_s, hits = redact_string(s, _REDACT_PATTERNS)  # type: ignore
        return new_s, len(hits)
    except Exception:
        return s, 0


def _truncate(s: str, cap: int = _DETAIL_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n...[truncated {len(s) - cap} bytes]"


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """One-line summary of a tool_use input for the `action` field."""
    if not isinstance(tool_input, dict):
        return tool_name
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        return f"Bash: {cmd[:120]}"
    if tool_name == "Edit":
        return f"Edit: {tool_input.get('file_path', '')}"
    if tool_name == "Write":
        return f"Write: {tool_input.get('file_path', '')}"
    if tool_name == "Read":
        return f"Read: {tool_input.get('file_path', '')}"
    if tool_name == "Grep":
        return f"Grep: {tool_input.get('pattern', '')}"
    if tool_name == "Glob":
        return f"Glob: {tool_input.get('pattern', '')}"
    if tool_name == "Task":
        return f"Task: {tool_input.get('description', tool_input.get('subagent_type', ''))}"
    # Generic fallback: first string value, truncated
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return f"{tool_name}: {v[:120]}"
    return tool_name


def _walk_session(path: Path) -> Iterator[dict]:
    """Yield event dicts from one session JSONL. Tolerant to malformed lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _extract_episodes(
    session_path: Path,
    project_slug: str,
    session_id: str,
) -> Iterator[dict]:
    """Walk a session JSONL and yield one episode per (tool_use, tool_result)
    pair. Pairs matched by tool_use_id."""
    pending: dict[str, dict] = {}  # tool_use_id -> tool_use info

    for ev in _walk_session(session_path):
        ev_type = ev.get("type")
        if ev_type in _SKIP_EVENT_TYPES:
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        if ev_type == "assistant":
            ts = ev.get("timestamp") or msg.get("timestamp") or ""
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_use_id = block.get("id")
                tool_name = block.get("name", "?")
                tool_input = block.get("input", {})
                if not isinstance(tool_use_id, str):
                    continue
                if tool_name in _LOW_SIGNAL_TOOLS:
                    continue
                pending[tool_use_id] = {
                    "name": tool_name,
                    "input": tool_input,
                    "ts": ts,
                }

        elif ev_type == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    continue
                paired = pending.pop(tool_use_id, None)
                if paired is None:
                    continue  # tool_result without seen tool_use — skip

                tool_name = paired["name"]
                tool_input = paired["input"]
                ts = paired["ts"] or ev.get("timestamp") or ""
                is_error = bool(block.get("is_error"))
                result_content = block.get("content")
                if isinstance(result_content, list):
                    out_parts = []
                    for rc in result_content:
                        if isinstance(rc, dict) and rc.get("type") == "text":
                            out_parts.append(str(rc.get("text", "")))
                        elif isinstance(rc, str):
                            out_parts.append(rc)
                    result_text = "\n".join(out_parts)
                elif isinstance(result_content, str):
                    result_text = result_content
                else:
                    result_text = json.dumps(result_content, ensure_ascii=False) if result_content else ""

                input_blob = json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
                detail_raw = f"INPUT:\n{input_blob}\n\nOUTPUT:\n{result_text}"
                detail_red, hits_d = _redact(detail_raw)
                detail = _truncate(detail_red)

                action = _summarize_tool_input(tool_name, tool_input if isinstance(tool_input, dict) else {})
                action_red, hits_a = _redact(action)
                reflection = f"{tool_name} {'failed' if is_error else 'ok'} in {project_slug}"
                _hits_total = hits_d + hits_a

                yield {
                    "timestamp": ts or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "skill": "claude-code",
                    "action": action_red[:200],
                    "result": "failure" if is_error else "success",
                    "detail": detail,
                    "pain_score": 4 if is_error else 1,
                    "importance": 5,
                    "reflection": reflection,
                    "confidence": 0.7,
                    "source": {
                        "adapter": "claude-sessions",
                        "profile": "transcript-backfill",
                        "session_id": session_id,
                        "project_slug": project_slug,
                    },
                    "evidence_ids": [],
                    "origin": f"claude.session.{tool_name}",
                    "summary": action_red[:120],
                    "_redaction_hits": _hits_total,
                }


def _enumerate_sessions(source_root: Path) -> list[Path]:
    """All session JSONL files under ~/.claude/projects/, sorted.

    Includes:
      - top-level: <project>/<sessionUUID>.jsonl
      - subagent:  <project>/<sessionUUID>/subagents/agent-*.jsonl
                   <project>/<sessionUUID>/<other>/*.jsonl
    """
    if not source_root.is_dir():
        return []
    files: list[Path] = []
    try:
        for proj_dir in sorted(source_root.iterdir()):
            if not proj_dir.is_dir() or proj_dir.is_symlink():
                continue
            # Recurse the entire project — picks up both top-level session
            # JSONLs and nested subagent transcripts.
            for jf in proj_dir.rglob("*.jsonl"):
                if jf.is_symlink() or not jf.is_file():
                    continue
                files.append(jf)
    except OSError:
        pass
    files.sort()
    return files


def _slug_and_session_id(path: Path, source_root: Path) -> tuple[str, str]:
    """Extract project_slug and session_id from the file path.

    Top-level: <slug>/<uuid>.jsonl       → (slug, uuid)
    Subagent:  <slug>/<uuid>/subagents/agent-X.jsonl
                                          → (slug, "<uuid>/subagents/<stem>")
    """
    try:
        rel = path.relative_to(source_root)
    except ValueError:
        # Defensive: fall back to old behavior
        return path.parent.name, path.stem
    parts = rel.parts
    if len(parts) >= 2:
        slug = parts[0]
        # Everything after the slug is the session_id (uses / as separator
        # so it stays human-readable in the sidecar)
        session_id = "/".join(parts[1:])
        if session_id.endswith(".jsonl"):
            session_id = session_id[: -len(".jsonl")]
        return slug, session_id
    return path.parent.name, path.stem


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude_session_adapter",
        description=__doc__.split("\n")[0],
    )
    p.add_argument("--source", default=str(Path.home() / ".claude" / "projects"),
                   help="Source root (default: ~/.claude/projects)")
    p.add_argument("--dst", default=str(Path.home() / ".agent"),
                   help="Brain root (default: ~/.agent)")
    p.add_argument("--dry-run", action="store_true", help="Scan and report, write nothing")
    p.add_argument("--limit", type=int, default=0, help="Process at most N session files")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    source_root = Path(args.source).expanduser()
    brain_root = Path(args.dst).expanduser()

    if not source_root.is_dir():
        print(f"ERROR: source not a directory: {source_root}", file=sys.stderr)
        return 2

    episodic_path = brain_root / "memory" / _EPISODIC_REL
    sidecar_path = brain_root / "memory" / _SIDECAR_REL

    seen_hashes = _read_sidecar(sidecar_path)
    sessions = _enumerate_sessions(source_root)
    if args.limit:
        sessions = sessions[: args.limit]

    n_total = len(sessions)
    n_skipped = 0
    n_imported = 0
    n_episodes = 0
    n_redacted = 0
    new_episodes: list[str] = []
    new_sidecar: list[dict] = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for i, path in enumerate(sessions):
        try:
            sha = _file_hash(path)
        except OSError as e:
            print(f"  WARN stat-failed {path}: {e}", file=sys.stderr)
            continue
        if sha in seen_hashes:
            n_skipped += 1
            continue

        slug, sid = _slug_and_session_id(path, source_root)
        before_count = len(new_episodes)
        for ep in _extract_episodes(path, slug, sid):
            n_redacted += ep.pop("_redaction_hits", 0)
            new_episodes.append(json.dumps(ep, ensure_ascii=False))
            n_episodes += 1
        added = len(new_episodes) - before_count
        n_imported += 1
        new_sidecar.append({
            "sha256": sha,
            "file_path": str(path),
            "project_slug": slug,
            "session_id": sid,
            "episodes_emitted": added,
            "imported_at": now_iso,
        })
        if args.verbose:
            print(f"  [{i+1}/{n_total}] {slug}/{sid[:8]}: {added} episodes")

    print(f"\nClaude session adapter — {'DRY-RUN' if args.dry_run else 'COMPLETE'}")
    print(f"  source:           {source_root}")
    print(f"  dst:              {brain_root}")
    print(f"  sessions found:   {n_total}")
    print(f"  already imported: {n_skipped}")
    print(f"  newly imported:   {n_imported}")
    print(f"  episodes emitted: {n_episodes}")
    print(f"  redaction hits:   {n_redacted}")

    if args.dry_run:
        # Estimate output bytes
        sample = new_episodes[:100]
        avg = sum(len(s) for s in sample) / max(1, len(sample))
        est_bytes = int(avg * n_episodes)
        print(f"  estimated output: {est_bytes // 1024} KB")
        return 0

    if new_episodes:
        episodic_path.parent.mkdir(parents=True, exist_ok=True)
        existing_text = episodic_path.read_text() if episodic_path.is_file() else ""
        new_text = existing_text + "\n".join(new_episodes) + ("\n" if new_episodes else "")
        atomic_write_text(episodic_path, new_text)
        print(f"  wrote:            {episodic_path}")
    _append_sidecar(sidecar_path, new_sidecar)
    if new_sidecar:
        print(f"  sidecar updated:  {sidecar_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
