"""Unified session-transcript normalizer.

Reads either:
  - `~/.claude/projects/<slug>/<uuid>.jsonl` (Claude Code sessions)
  - `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl` (Codex CLI sessions)

…and returns a `NormalizedSession` so the digest adapter doesn't care
about upstream format quirks. The contract is pinned by
`tests/test_session_normalize.py`.

Tolerance: malformed JSON lines are silently skipped (real sessions can
have a half-written tail line during active editing). A session that
contains no actual conversation turns (metadata only, all-system events)
returns None — caller treats that as "skip this session, nothing to
digest".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal


@dataclass
class NormalizedMessage:
    role: Literal["user", "assistant"]
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class NormalizedSession:
    session_id: str
    source: Literal["claude", "codex"]
    started_at: str
    ended_at: str
    cwd: str | None
    git_branch: str | None
    project_slug: str | None
    model: str | None
    messages: list[NormalizedMessage]
    raw_token_estimate: int


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects, skipping malformed lines silently.
    Tolerates active sessions where the last line may be partial."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _token_estimate(*texts: str) -> int:
    """4-char rule, deterministic. Used by the adapter to decide whether
    a session needs map-reduce summarization."""
    total = sum(len(t) for t in texts if t)
    return total // 4


# ---------------------------------------------------------------------------
# Claude normalization
# ---------------------------------------------------------------------------

_CLAUDE_SKIP_TYPES = {
    "file-history-snapshot",
    "permission-mode",
    "attachment",
    "queue-operation",
    "last-prompt",
    "ai-title",
    "pr-link",
    "agent-name",
    "system",
}


def _claude_user_text(message: dict) -> tuple[str, list[dict]]:
    """Pull text out of a Claude `user` event's content. Returns
    (text, tool_results). Content can be a plain string (typed prompt)
    or an array of blocks (tool_result feedback). Both shapes must
    survive — tool outputs are where the actionable findings often are."""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    tool_results: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for ib in inner:
                    if isinstance(ib, dict) and ib.get("type") == "text":
                        parts.append(str(ib.get("text", "")))
            tool_results.append({
                "tool_use_id": block.get("tool_use_id"),
                "is_error": block.get("is_error", False),
            })
        elif btype == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(p for p in parts if p), tool_results


def _claude_assistant_text(message: dict) -> tuple[str, list[dict]]:
    """Pull text + tool_use blocks out of a Claude `assistant` event."""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "thinking":
            # Thinking blocks carry the assistant's reasoning; we keep
            # them so the summarizer can see WHY a decision was made.
            parts.append(str(block.get("thinking", "")))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input"),
            })
    return "\n".join(p for p in parts if p), tool_calls


def normalize_claude_session(
    path: Path | str, *, project_slug: str | None,
) -> NormalizedSession | None:
    """Parse a single Claude session jsonl into NormalizedSession. Returns
    None when the file has no user/assistant turns."""
    p = Path(path)
    messages: list[NormalizedMessage] = []
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    model: str | None = None
    timestamps: list[str] = []
    text_bytes_for_estimate: list[str] = []

    for obj in _iter_jsonl(p):
        etype = obj.get("type")
        if etype in _CLAUDE_SKIP_TYPES:
            continue
        ts = obj.get("timestamp")
        if isinstance(ts, str):
            timestamps.append(ts)
        # Capture session metadata from the first event that has it.
        if session_id is None and isinstance(obj.get("sessionId"), str):
            session_id = obj["sessionId"]
        if cwd is None and isinstance(obj.get("cwd"), str):
            cwd = obj["cwd"]
        if git_branch is None and isinstance(obj.get("gitBranch"), str):
            git_branch = obj["gitBranch"]
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        if model is None and isinstance(msg.get("model"), str):
            model = msg["model"]

        if etype == "user":
            text, _trs = _claude_user_text(msg)
            if text.strip():
                messages.append(NormalizedMessage(
                    role="user", text=text, timestamp=ts or "",
                ))
                text_bytes_for_estimate.append(text)
        elif etype == "assistant":
            text, tcs = _claude_assistant_text(msg)
            if text.strip() or tcs:
                messages.append(NormalizedMessage(
                    role="assistant", text=text, tool_calls=tcs,
                    timestamp=ts or "",
                ))
                text_bytes_for_estimate.append(text)

    if not messages:
        return None

    return NormalizedSession(
        session_id=session_id or p.stem,
        source="claude",
        started_at=min(timestamps) if timestamps else "",
        ended_at=max(timestamps) if timestamps else "",
        cwd=cwd,
        git_branch=git_branch,
        project_slug=project_slug,
        model=model,
        messages=messages,
        raw_token_estimate=_token_estimate(*text_bytes_for_estimate),
    )


# ---------------------------------------------------------------------------
# Codex normalization
# ---------------------------------------------------------------------------

def _codex_message_text(payload: dict) -> str:
    """Extract text from a Codex response_item.message payload. Content
    is a list of {type, text} blocks. Both `input_text` (user) and
    `output_text` (assistant) — and any other text-bearing variant —
    are concatenated."""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
    return "\n".join(parts)


def normalize_codex_session(path: Path | str) -> NormalizedSession | None:
    """Parse a single Codex rollout jsonl. Returns None when there are
    no actual response_item.message events (metadata-only sessions
    happen — Codex writes a session_meta + nothing else if the user
    aborts immediately)."""
    p = Path(path)
    messages: list[NormalizedMessage] = []
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    model: str | None = None
    timestamps: list[str] = []
    text_bytes_for_estimate: list[str] = []

    for obj in _iter_jsonl(p):
        etype = obj.get("type")
        ts = obj.get("timestamp")
        if isinstance(ts, str):
            timestamps.append(ts)
        payload = obj.get("payload") or {}

        if etype == "session_meta" and isinstance(payload, dict):
            if session_id is None:
                sid = payload.get("id")
                if isinstance(sid, str):
                    session_id = sid
            if cwd is None and isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            git = payload.get("git")
            if git_branch is None and isinstance(git, dict) \
                    and isinstance(git.get("branch"), str):
                git_branch = git["branch"]
            continue

        if etype == "turn_context" and isinstance(payload, dict):
            if model is None and isinstance(payload.get("model"), str):
                model = payload["model"]
            continue

        if etype == "response_item" and isinstance(payload, dict):
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _codex_message_text(payload)
            if text.strip():
                messages.append(NormalizedMessage(
                    role=role, text=text, timestamp=ts or "",
                ))
                text_bytes_for_estimate.append(text)

    if not messages:
        return None

    return NormalizedSession(
        session_id=session_id or p.stem,
        source="codex",
        started_at=min(timestamps) if timestamps else "",
        ended_at=max(timestamps) if timestamps else "",
        cwd=cwd,
        git_branch=git_branch,
        project_slug=None,  # Codex doesn't have a project_slug concept
        model=model,
        messages=messages,
        raw_token_estimate=_token_estimate(*text_bytes_for_estimate),
    )
