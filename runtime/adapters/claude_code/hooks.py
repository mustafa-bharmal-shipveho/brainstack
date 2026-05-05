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

    # Re-injection: on UserPromptSubmit, when enabled, emit a small text
    # block to stdout that Claude Code may append to the prompt. This is
    # the v0.3 inject-loop closure — the runtime stops being purely
    # observational when this fires.
    if event_name == "UserPromptSubmit" and config.enable_reinjection:
        try:
            block = _build_reinjection_for_session(config)
        except Exception as e:  # pragma: no cover - defensive
            print(f"[runtime] re-injection skipped: {e!r}", file=sys.stderr)
            block = ""
        if block:
            print(block)

    # Auto-recall: sibling to the reinjection branch above. Fail-open on
    # every error path — never block a user's prompt.
    if event_name == "UserPromptSubmit" and config.enable_auto_recall:
        _handle_auto_recall(payload, config, str(session_id))
    return 0


def _handle_auto_recall(payload: dict[str, Any], config: RuntimeConfig,
                        session_id: str) -> None:
    """Run the auto-recall flow + emit the injection block + AutoRecall
    telemetry event. Catches all exceptions; never raises to the hook
    entrypoint. Failure modes (skip / timeout / unavailable / error) are
    distinguished in telemetry so `recall stats` can report them."""
    from runtime.adapters.claude_code import auto_recall

    prompt = str(
        payload.get("prompt") or payload.get("user_prompt")
        or payload.get("text") or ""
    )

    skip, reason = auto_recall.should_skip(
        prompt, min_chars=config.auto_recall_min_chars
    )
    if skip:
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "skip", "x_skip_reason": reason or "unknown"},
        )
        return

    # Build block under a hard timeout. CRITICAL: a `ThreadPoolExecutor`
    # spawns *non-daemon* workers, which keep the interpreter alive on
    # `atexit` even after `shutdown(wait=False)` — defeating the timeout
    # for downstream callers (Claude Code blocks waiting for the hook
    # subprocess to actually exit). Use a daemon thread instead so the
    # abandoned worker dies with the hook process. Codex 2026-05-05 HIGH.
    #
    # Retriever construction (embedder + qdrant cold-start) happens INSIDE
    # the worker so it's bounded by the same timeout. Otherwise a 2-second
    # embedder load would block the hook before the timer started, breaking
    # the latency contract on first-fire. Codex 2026-05-05 P2.
    import queue
    import threading

    timeout_s = max(0.05, config.auto_recall_timeout_ms / 1000.0)
    result_q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=1)
    error_q: "queue.Queue[BaseException]" = queue.Queue(maxsize=1)
    unavailable_q: "queue.Queue[BaseException]" = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            try:
                retriever = auto_recall._load_retriever()
            except Exception as load_exc:
                # ImportError / qdrant missing / cold-start crash — fail open
                unavailable_q.put(load_exc)
                return
            result_q.put(auto_recall.build_recall_block(
                prompt, retriever,
                k=config.auto_recall_k,
                budget_tokens=config.auto_recall_budget_tokens,
                min_score=config.auto_recall_min_score,
            ))
        except BaseException as exc:  # noqa: BLE001 — pass to main thread
            error_q.put(exc)

    t = threading.Thread(target=_worker, daemon=True, name="auto-recall")
    t.start()
    t.join(timeout=timeout_s)

    if t.is_alive():
        # Worker still running. It's a daemon thread, so it'll be killed
        # when this process exits. Don't wait.
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "timeout"},
        )
        return
    if not unavailable_q.empty():
        exc = unavailable_q.get_nowait()
        print(f"[runtime] auto-recall unavailable: {exc!r}", file=sys.stderr)
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "unavailable"},
        )
        return
    if not error_q.empty():
        exc = error_q.get_nowait()
        print(f"[runtime] auto-recall error: {exc!r}", file=sys.stderr)
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "error"},
        )
        return

    block, telemetry = result_q.get_nowait()
    if block:
        print(block)
    _append_auto_recall_event(config, session_id, extensions=telemetry)


def _append_auto_recall_event(config: RuntimeConfig, session_id: str,
                              *, extensions: dict[str, Any]) -> None:
    """Write a single AutoRecall EventRecord. AutoRecall is NOT in
    `_KNOWN_HOOK_EVENTS` (which whitelists Claude-Code-driven events);
    it's a runtime-emitted event with its own name. The events.py loader
    accepts arbitrary `event` strings — only the routing in `handle_hook`
    cares about the whitelist."""
    record = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=_now_ms(),
        event="AutoRecall",
        session_id=session_id,
        turn=0,
        extensions=extensions,
    )
    try:
        append_event(config.event_log_path, record)
    except Exception as e:  # pragma: no cover - defensive
        # Log but don't propagate — telemetry failure must not break the prompt
        print(f"[runtime] auto-recall telemetry write failed: {e!r}", file=sys.stderr)


def _build_reinjection_for_session(config) -> str:
    """Replay the event log to current state, then ask the composer to
    build a re-injection block. Returns empty string if nothing useful."""
    from runtime.adapters.claude_code.reinjection import (
        ReinjectionContext,
        build_reinjection_block,
        collect_user_intent_events,
    )
    from runtime.core.events import load_events
    from runtime.core.policy.defaults.lru import LRUPolicy
    from runtime.core.replay import ReplayConfig, replay

    if not config.event_log_path.exists():
        return ""
    events = load_events(config.event_log_path)
    if not events:
        return ""

    rcfg = ReplayConfig(
        budgets=dict(config.budgets),
        policy=LRUPolicy(),
        session_id="reinjection",
    )
    summary = replay(config.event_log_path, rcfg)
    if not summary.manifests:
        return ""
    manifest = summary.manifests[-1]

    # Intent events since the PREVIOUS UserPromptSubmit (the one before the
    # one we just wrote). The just-written UserPromptSubmit is the LAST in
    # the list; we want everything between the second-to-last and now.
    ups_timestamps = [ev.ts_ms for ev in events if ev.event == "UserPromptSubmit"]
    boundary_ts = ups_timestamps[-2] if len(ups_timestamps) >= 2 else 0
    user_added, user_evicted = collect_user_intent_events(events, since_ts_ms=boundary_ts)

    # Load content for added/pinned items from disk if available
    content_by_id: dict[str, str] = {}
    added_dir = config.log_dir / "added"
    if added_dir.exists():
        for it in user_added:
            f = added_dir / f"{it.id}.txt"
            if f.exists():
                content_by_id[it.id] = f.read_text(encoding="utf-8")
        for it in manifest.items:
            if it.pinned:
                f = added_dir / f"{it.id}.txt"
                if f.exists():
                    content_by_id[it.id] = f.read_text(encoding="utf-8")

    ctx = ReinjectionContext(
        manifest=manifest,
        user_added_items=user_added,
        user_evicted_ids=user_evicted,
        item_content_by_id=content_by_id,
        budget_tokens=config.reinjection_budget_tokens,
    )
    return build_reinjection_block(ctx)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint when invoked as `python -m ...claude_code.hooks <event>`."""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 0
    event = args[0]
    return handle_hook(event)


if __name__ == "__main__":
    sys.exit(main())
