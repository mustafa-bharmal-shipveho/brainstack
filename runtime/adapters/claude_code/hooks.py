"""Claude Code hook entrypoints.

Each function here is a callable entry point invoked by Claude Code via a
shell command that runs `python -m runtime.adapters.claude_code.hooks <event>`
and pipes the hook's JSON payload to stdin.

Design: hooks are append-only loggers. They write one EventRecord per hook
invocation to the configured events.log.jsonl. They do NOT run the Engine
or enforce budgets — that work happens lazily when someone asks for the
manifest via `recall runtime ls` or `recall runtime replay`. Reasoning:

  - hooks must be fast (<50ms p95 target). Running the full Engine on
    every hook would add O(N) replay cost per invocation.
  - replay-from-events is already proven byte-equal to live engine via
    test_integration_live_replay. We don't need to run the engine twice.
  - Errors in the engine layer don't block Claude Code; the hook just
    appends what it knows.

The adapter does compute token_count for content-producing tools
(Read/Grep/Glob) so that information lands in the events.log.jsonl
items_added entries. The CLI / replay engine consumes those.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.core.events import (
    EVENT_LOG_SCHEMA_VERSION,
    EventRecord,
    OutputSummary,
    append_event,
    summarize_output,
)
from runtime.core.manifest import InjectionItemSnapshot
from runtime.core.tokens import OfflineTokenCounter

# Mapping from Claude Code event names (passed as the first CLI arg) to
# our internal EventRecord.event values. We keep them aligned (no rename).
_KNOWN_HOOK_EVENTS = frozenset({
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "SubagentStop", "Notification", "PostCompact",
    "PostToolUseFailure",
})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_stdin_json() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _content_id(content: str, tool_name: str) -> str:
    """Stable id for a piece of injected content. Hash content + tool to
    avoid collisions when the same string appears via different tools."""
    h = hashlib.sha256(f"{tool_name}\x00{content}".encode("utf-8")).hexdigest()
    return f"c-{h[:16]}"


def _items_for_post_tool_use(payload: dict[str, Any], config: RuntimeConfig) -> list[InjectionItemSnapshot]:
    """Translate a PostToolUse payload into 0+ InjectionItemSnapshot entries."""
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if tool_name not in {"Read", "Glob", "Grep", "Bash", "Edit", "Write"}:
        return []

    # Try a few common shapes for the tool's input + output. Claude Code's
    # exact schema is documented as evolving; we look for the obvious ones.
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_response = payload.get("tool_response") or payload.get("toolResponse") or ""
    if isinstance(tool_response, dict):
        # Some hooks deliver structured responses; serialize for token counting.
        text = json.dumps(tool_response, sort_keys=True, ensure_ascii=False)
    else:
        text = str(tool_response or "")
    if not text:
        return []

    counter = OfflineTokenCounter()
    token_count = counter.count(text)
    file_path = ""
    if isinstance(tool_input, dict):
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    source_path = file_path or f"<tool:{tool_name}>"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    item = InjectionItemSnapshot(
        id=_content_id(text, tool_name),
        bucket=config.tool_to_bucket(tool_name),
        source_path=source_path,
        sha256=sha,
        token_count=token_count,
        retrieval_reason=f"post-tool-use:{tool_name}",
        last_touched_turn=0,  # the engine sets this during replay
        pinned=False,
        score=0.0,
    )
    return [item]


def handle_hook(event_name: str, *, config: RuntimeConfig | None = None) -> int:
    """Generic hook entry. Returns 0 to keep Claude Code happy.

    This function is the seam tested in tests/runtime/test_adapter_hooks.py.
    """
    if event_name not in _KNOWN_HOOK_EVENTS:
        # Tolerant: unknown event names are noops, not errors. Future-proof.
        return 0

    config = config or RuntimeConfig.load()
    payload = _read_stdin_json()
    session_id = (
        payload.get("session_id") or payload.get("sessionId") or "unknown"
    )

    items_added: list[InjectionItemSnapshot] = []
    if event_name == "PostToolUse":
        items_added = _items_for_post_tool_use(payload, config)

    record = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=_now_ms(),
        event=event_name,
        session_id=str(session_id),
        turn=0,  # the engine assigns turn numbers during replay
        tool_name=str(payload.get("tool_name") or payload.get("toolName") or ""),
        tool_input_keys=sorted(
            list(
                (payload.get("tool_input") or payload.get("toolInput") or {}).keys()
            )
        ) if isinstance(payload.get("tool_input") or payload.get("toolInput"), dict) else [],
        tool_output_summary=(
            summarize_output(
                str(payload.get("tool_response") or payload.get("toolResponse") or ""),
                include_hash=False,
            )
            if event_name == "PostToolUse"
            else None
        ),
        items_added=items_added,
    )
    append_event(config.event_log_path, record)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint when invoked as `python -m ...claude_code.hooks <event>`."""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 0
    event = args[0]
    return handle_hook(event)


if __name__ == "__main__":
    sys.exit(main())
