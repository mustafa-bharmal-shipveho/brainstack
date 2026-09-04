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
import os
import sys
import time
from collections.abc import Callable
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

# Daemon failure reasons that mean the daemon is DOWN, so nothing holds the
# embedded-Qdrant process lock and the in-process fallback can actually run.
#
# The complement (`timeout`, `server_error`, `protocol_error`) means the
# daemon is ALIVE and owns the store: an in-process fallback would only
# block on fcntl until the hook's own deadline fires, turning an 800 ms
# degradation into a 1500 ms one and still returning nothing. Those report
# `unavailable` instead. Documented deviation from SPEC's "always fall
# back" — see plans/hook-path.md, "Client budget (hook)".
_DAEMON_DOWN_REASONS = frozenset({
    "no_socket", "connection_refused", "import_error",
})

# `x_daemon_error` is a diagnostic, not a payload. events.py caps every x_*
# value at 1024 bytes and drops the WHOLE record on breach.
_DAEMON_ERROR_MAX_CHARS = 200

# One health FAIL must not be able to flood the SessionStart banner.
_HEALTH_EVIDENCE_MAX_CHARS = 220
_HEALTH_STALE_HOURS_DEFAULT = 26.0
_HEALTH_FOOTER = "brainstack health: run 'recall health' for details and fixes"


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

    # SessionStart is the only surface where a health regression reaches the
    # user without them running a command. Strictly best-effort: the banner
    # is printed after the event is written, so a broken report costs a
    # banner, never telemetry.
    if event_name == "SessionStart":
        _print_health_banner(config, payload)

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
    entrypoint.

    Retrieval is DAEMON-FIRST. The hook is a fresh Python subprocess on
    every prompt, and loading qdrant + an embedder there costs ~1.5 s cold,
    which is why auto-recall used to time out on real machines. The warm
    daemon answers over a Unix socket in ~60-130 ms.

    Whether a failed daemon call may fall back in-process depends on WHY it
    failed — see `_DAEMON_DOWN_REASONS`.

    Every outcome (skip / hit / miss / dedup / timeout / unavailable /
    error) carries `x_latency_ms`, so the latency distribution is computed
    over all fires rather than over the survivors.
    """
    from runtime.adapters.claude_code import auto_recall

    started = time.perf_counter()
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
            extensions={
                "x_outcome": "skip",
                "x_skip_reason": reason or "unknown",
                "x_latency_ms": _elapsed_ms(started),
            },
        )
        return

    dedup_store = _build_dedup_store(config, session_id)
    socket_path = _resolve_daemon_socket(config)
    # The socket budget can never exceed the worker's own deadline: an
    # 800 ms budget under a 300 ms timeout would guarantee the thread is
    # abandoned mid-recv and every fire logged as `timeout` instead of the
    # honest reason.
    budget_ms = min(config.auto_recall_daemon_budget_ms,
                    config.auto_recall_timeout_ms)
    brain_root = _brain_root()

    # Build block under a hard timeout. CRITICAL: a `ThreadPoolExecutor`
    # spawns *non-daemon* workers, which keep the interpreter alive on
    # `atexit` even after `shutdown(wait=False)` — defeating the timeout
    # for downstream callers (Claude Code blocks waiting for the hook
    # subprocess to actually exit). Use a daemon thread instead so the
    # abandoned worker dies with the hook process. Codex 2026-05-05 HIGH.
    #
    # Both the socket call and the fallback's retriever construction happen
    # INSIDE the worker so they're bounded by the same timeout. Otherwise a
    # 2-second embedder load would block the hook before the timer started,
    # breaking the latency contract on first-fire. Codex 2026-05-05 P2.
    import queue
    import threading

    timeout_s = max(0.05, config.auto_recall_timeout_ms / 1000.0)
    out_q: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)
    # Written by the worker, read by this thread after the join. On a
    # timeout it names the phase that was in flight, which is the only way
    # to tell "the daemon never answered" from "the embedder never loaded".
    state: dict[str, Any] = {"path": "daemon", "daemon_error": None}

    def _worker() -> None:
        try:
            response, fail_reason = _daemon_query(
                prompt,
                k=config.auto_recall_k,
                session_id=session_id,
                socket_path=socket_path,
                budget_ms=budget_ms,
            )
            if response is None:
                state["daemon_error"] = fail_reason or "unknown"
                if _reason_tag(fail_reason) not in _DAEMON_DOWN_REASONS:
                    # The daemon is alive and owns the Qdrant store lock.
                    out_q.put(("unavailable", None))
                    return
                state["path"] = "inproc"
                try:
                    retriever: Any = auto_recall._load_retriever()
                except BaseException as load_exc:  # noqa: BLE001
                    # ImportError / qdrant missing / cold-start crash.
                    out_q.put(("unavailable", load_exc))
                    return
            else:
                retriever = auto_recall.DaemonResults(response)

            out_q.put(("ok", auto_recall.build_recall_block(
                prompt, retriever,
                k=config.auto_recall_k,
                budget_tokens=config.auto_recall_budget_tokens,
                min_score=config.auto_recall_min_score,
                min_rerank=config.auto_recall_min_rerank,
                dedup_store=dedup_store,
                brain_root=brain_root,
            )))
        except BaseException as exc:  # noqa: BLE001 — pass to main thread
            out_q.put(("error", exc))

    t = threading.Thread(target=_worker, daemon=True, name="auto-recall")
    t.start()
    t.join(timeout=timeout_s)

    # Hook-level fields, merged onto whatever outcome we end up recording.
    # A 900 ms p50 means one thing on "daemon" and another on "inproc", so
    # neither number is readable without the other.
    hook_ext: dict[str, Any] = {"x_path": state["path"]}
    if state["daemon_error"]:
        hook_ext["x_daemon_error"] = (
            str(state["daemon_error"])[:_DAEMON_ERROR_MAX_CHARS]
        )

    if t.is_alive():
        # Worker still running. It's a daemon thread, so it'll be killed
        # when this process exits. Don't wait.
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "timeout",
                        "x_latency_ms": _elapsed_ms(started), **hook_ext},
        )
        return

    try:
        kind, value = out_q.get_nowait()
    except Exception:  # pragma: no cover - the worker always puts exactly one
        kind, value = "error", RuntimeError("auto-recall worker produced nothing")

    if kind == "unavailable":
        if value is not None:
            print(f"[runtime] auto-recall unavailable: {value!r}", file=sys.stderr)
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "unavailable",
                        "x_latency_ms": _elapsed_ms(started), **hook_ext},
        )
        return
    if kind == "error":
        print(f"[runtime] auto-recall error: {value!r}", file=sys.stderr)
        _append_auto_recall_event(
            config, session_id,
            extensions={"x_outcome": "error",
                        "x_latency_ms": _elapsed_ms(started), **hook_ext},
        )
        return

    block, telemetry, injected = value
    if block:
        print(block)
        # COMMIT POINT. `record` marks these docs "already shown this
        # session", which suppresses them on every later prompt — so it
        # may only run once the block has actually reached the user. The
        # worker thread cannot know that: it is abandoned on timeout, and
        # recording there marked docs as shown for a block nobody saw.
        if dedup_store is not None and injected:
            try:
                dedup_store.record(injected)
            except Exception as e:  # pragma: no cover - defensive
                # Fail-open: a dedup we do not get, not a prompt lost.
                print(f"[runtime] auto-recall dedup record failed: {e!r}",
                      file=sys.stderr)

    extensions = dict(telemetry)
    extensions.update(hook_ext)
    # Full worker wall. Floored by the retrieval time the backend reported
    # so the invariant `x_latency_ms >= x_query_ms` holds even when the
    # daemon's own clock ran ahead of ours; in production the socket call
    # is a component of the wall, so the floor never binds.
    extensions["x_latency_ms"] = max(
        _elapsed_ms(started), int(extensions.get("x_query_ms") or 0),
    )
    _append_auto_recall_event(config, session_id, extensions=extensions)

    # Nothing else ever cleans the injected dir, so every fire pays this
    # one cheap pass. After the `record` above, so the store we just wrote
    # survives (its mtime is fresh, well inside the prune window).
    if dedup_store is not None:
        try:
            type(dedup_store).prune(config.injected_dir)
        except Exception as e:  # pragma: no cover - defensive
            print(f"[runtime] auto-recall dedup prune failed: {e!r}",
                  file=sys.stderr)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _reason_tag(reason: "str | None") -> str:
    """The bare reason from a `DaemonUnavailable`-style string, which may
    carry a `"<reason>: <detail>"` message."""
    return str(reason or "unknown").split(":", 1)[0].strip()


def _build_dedup_store(config: RuntimeConfig, session_id: str) -> Any:
    """The per-session injected-doc store, or None when disabled.

    `auto_recall_dedup = false` is the kill switch: no store is
    constructed, so nothing is written and every fire re-injects.
    """
    if not getattr(config, "auto_recall_dedup", True):
        return None
    try:
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        return SessionDedupStore(config.injected_dir, session_id)
    except Exception as e:  # pragma: no cover - defensive
        print(f"[runtime] auto-recall dedup disabled: {e!r}", file=sys.stderr)
        return None


def _brain_root_or(fallback: "Callable[[], Path | None]") -> "Path | None":
    """`recall.config.brain_root()` as a `Path`, or `fallback()`.

    This module must stay importable and usable without `recall` on the
    path, so every brain-root consumer needs this exact guard — and never
    raises, because a path helper that is missing or unimplemented must not
    cost the user their prompt. They differ only in what they fall back to.
    """
    try:
        from recall.config import brain_root

        return Path(brain_root())
    except Exception:
        return fallback()


def _brain_root() -> "Path | None":
    """The brain root used to relativize `x_paths`, or None.

    None means telemetry keeps absolute paths — honest but machine-bound.
    """
    def _from_env() -> "Path | None":
        env = os.environ.get("BRAIN_ROOT")
        return Path(env).expanduser() if env else None

    return _brain_root_or(_from_env)


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


def _resolve_daemon_socket(config: RuntimeConfig) -> Path:
    """Resolve the warm recall daemon's socket path for this hook fire.

    Precedence is `$RECALL_DAEMON_SOCKET` > the configured path > the
    brain-root default. The env var is an OVERRIDE and therefore outranks
    an explicitly configured path: the test suite's root conftest sets it
    for every test so nothing can reach the developer's live socket at
    `~/.agent/runtime/recall.sock`, and a config value that could outrank
    it would defeat that guard. Orchestrator decision, 2026-09-04.

    `recall.config.daemon_socket_path` (reached through the config
    property) is the single source of truth, and this function is a bare
    pass-through to it. It must stay that way: the hook, the CLI and the
    daemon all have to agree on one path, and a second copy of the
    precedence here is how they end up on different sockets. The
    `$`-literal guard for an unexpandable placeholder lives there too.
    """
    return Path(config.daemon_socket_path)


def _daemon_query(
    prompt: str,
    *,
    k: int,
    session_id: str,
    socket_path: "Path",
    budget_ms: int,
) -> "tuple[dict | None, str | None]":
    """Query the warm recall daemon over its Unix socket.

    Returns `(response, None)` on success or `(None, reason)` on failure,
    where `reason` is one of the `recall.daemon_client.DaemonUnavailable`
    reasons (`no_socket`, `connection_refused`, `timeout`,
    `protocol_error`, `server_error`) or `import_error` when
    `recall.daemon_client` itself is unavailable (an older install or a
    partial upgrade must degrade to the in-process path, not disable
    auto-recall).

    The import is lazy and the client is stdlib-only by design — the hook
    must never pull `recall.core`/qdrant just to ask a question over a
    socket.
    """
    def _fail(reason: str, exc: BaseException) -> "tuple[None, str]":
        return (None, f"{reason}: {exc}"[:_DAEMON_ERROR_MAX_CHARS])

    try:
        from recall import daemon_client
    except Exception as exc:
        return _fail("import_error", exc)

    try:
        response = daemon_client.query(
            prompt, k=k, socket_path=socket_path,
            budget_ms=budget_ms, session_id=session_id,
        )
    except (NotImplementedError, AttributeError, ImportError) as exc:
        # The client exists but cannot perform the call — an unfinished or
        # partially upgraded install. No daemon is holding anything on our
        # behalf, so this is the same situation as the module being
        # missing: fall back rather than go silent.
        return _fail("import_error", exc)
    except Exception as exc:
        # `DaemonUnavailable` carries the reason the fallback policy keys
        # on. Anything else is an unexpected client fault while a daemon
        # may well be alive and holding the store lock, so it takes the
        # conservative no-fallback branch.
        return _fail(str(getattr(exc, "reason", "") or "server_error"), exc)
    return (response, None)


def _print_health_banner(config: RuntimeConfig, payload: dict[str, Any]) -> None:
    """Print one line per FAIL check from the cached `runtime/health.json`
    report, plus a live re-check of THIS session's auto-recall config.

    The live re-check exists because the cached report is written by the
    hourly sync agent with the brain as cwd, so it can never see that the
    directory Claude Code opened shadows the global config and silently
    disables auto-recall.

    Rules, in order of importance: never raise, never block, always leave
    the exit code at 0, and stay silent when there is nothing wrong. A
    missing report is silence too — nagging about a file the user has
    never heard of, before the first sync tick has written it, is worse
    than saying nothing.
    """
    try:
        lines = _health_banner_lines(config, payload)
    except Exception:
        # Deliberately silent, including on a bug in the code above: a
        # broken banner must never cost the user a session.
        return
    if not lines:
        return
    for line in lines:
        print(line)
    print(_HEALTH_FOOTER)


def _health_banner_lines(config: RuntimeConfig,
                         payload: dict[str, Any]) -> list[str]:
    """The banner body, without the footer. Split out so the printing
    wrapper can stay a bare try/except."""
    lines: list[str] = []
    try:
        from recall import health as _health

        path = _health_report_path(config)
        # Missing/corrupt and stale want two different banners: silence for
        # the first (a fresh install has never had a report, and nagging
        # about a file the user has never heard of is worse than saying
        # nothing) and a warning for the second. `read_report` returns both
        # answers off ONE parse; `load_report` collapses them into a single
        # None, which used to cost this session-open path two reads of the
        # same file. Contract from the health slice owner, 2026-09-04.
        report, stale = _health.read_report(path)
        if report is None:
            return []
        if stale:
            # The checks inside a stale report are no longer evidence of
            # anything, so they are not reported as if they were.
            stale_hours = float(
                getattr(_health, "HEALTH_STALE_HOURS", _HEALTH_STALE_HOURS_DEFAULT)
            )
            lines.append(
                f"brainstack health: last report "
                f"{getattr(report, 'generated_at', '?')} is older than "
                f"{int(stale_hours)}h; the hourly sync LaunchAgent may be "
                f"dead. run 'recall health'"
            )
        else:
            lines.extend(
                _health_fail_line(c.id, c.evidence) for c in report.failures()
            )
    except Exception:
        # Missing module, unimplemented helper, unreadable file — all mean
        # the same thing to the user: no banner.
        return []

    live = _live_auto_recall_check(payload, config)
    if live is not None:
        lines.append(_health_fail_line(*live))
    return lines


def _health_report_path(config: RuntimeConfig) -> Path:
    """Where `sync.sh` writes the cached health report.

    `<brain>/runtime/health.json`, resolved from `recall.config.brain_root()`
    — NOT from `config.log_dir.parent`. `log_dir` is user-configurable (the
    demo points it elsewhere), and deriving the report path from it made the
    banner silently unreachable for anyone who had moved their logs, which
    is exactly the population most likely to have a health problem.

    The lookup is guarded (see `_brain_root_or`) because this module must
    stay importable without `recall` installed; the `log_dir.parent` guess
    is kept only as the no-`recall` fallback.
    """
    root = _brain_root_or(lambda: None)
    if root is None:
        return config.log_dir.parent / "health.json"
    return root / "runtime" / "health.json"


def _health_fail_line(check_id: str, evidence: str) -> str:
    """One FAIL, one line — always.

    Evidence is UNTRUSTED: it carries verbatim `remote: error:` git output
    and `check_freshness` summaries, which contain newlines and ANSI
    colour. A raw newline here would let a single check forge extra
    `brainstack health FAIL:` lines in the banner or scroll the real ones
    out of view, so it is flattened before the cap.
    """
    try:
        from recall.sanitize import sanitize_untrusted

        text = sanitize_untrusted(
            str(evidence),
            max_len=_HEALTH_EVIDENCE_MAX_CHARS,
            keep_newlines=False,
        )
    except Exception:
        # No `recall` on the path. Still never emit a second line.
        text = " ".join(str(evidence).split())[:_HEALTH_EVIDENCE_MAX_CHARS]
    return f"brainstack health FAIL: {check_id} — {text}"


def _live_auto_recall_check(
    payload: dict[str, Any], config: "RuntimeConfig | None" = None,
) -> "tuple[str, str] | None":
    """Re-run the auto-recall config check against THIS session's cwd.

    Returns `(check_id, evidence)` on FAIL, else None. Silent on any
    error, including the health module not being importable.

    `config` is the config the hook already loaded, from the process cwd.
    When the session's cwd IS that directory — the normal case, since
    Claude Code runs hooks in the project directory — reuse it instead of
    making the check parse the same TOML layers again. Any other cwd falls
    back to the chdir-guarded load, which is the only correct answer there.
    """
    try:
        from recall.health import build_env, check_auto_recall_config

        cwd = Path(str(payload.get("cwd") or os.getcwd()))
        reuse = config if _is_process_cwd(cwd) else None
        result = check_auto_recall_config(build_env(cwd=cwd), config=reuse)
    except Exception:
        return None
    if getattr(result, "status", "") != "FAIL":
        return None
    return (getattr(result, "id", "auto_recall_config"),
            getattr(result, "evidence", ""))


def _is_process_cwd(path: Path) -> bool:
    """True iff `path` is the directory this process is running in.

    Compared through `realpath` so a symlinked worktree or a /private
    prefix on macOS does not read as a different directory and cost a
    redundant config load. False on any error: the caller's fallback is
    only slower, never wrong.
    """
    try:
        return os.path.realpath(path) == os.path.realpath(os.getcwd())
    except OSError:
        return False


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
