"""Session-digest adapter.

Walks Claude/Codex transcripts and produces ONE structured digest per
session, written to two surfaces (episodic JSONL line for vector recall
and a markdown file with YAML front-matter for browse + git sync).

Architecture (matches the approved plan):

    1. Walk sessions (Claude `~/.claude/projects/<slug>/<uuid>.jsonl`
       and Codex `~/.codex/sessions/.../rollout-*.jsonl`).
    2. Dedup via content-SHA sidecar at
       memory/episodic/digests/_imported.jsonl. Re-runs are no-ops
       when nothing changed.
    3. Normalize (via _session_normalize) so the LLM doesn't see
       upstream-format quirks.
    4. Redact each turn's text via redact_jsonl.redact_string before
       it leaves the local process.
    5. Summarize via the resolved LLM provider, JSON-schema enforced.
       Small sessions: single call. Big sessions (>SINGLE_PASS_TOKEN_LIMIT):
       map-reduce — one chunk-summary per chunk, then a final merge.
    6. Write episodic line + markdown via _digest_render.write_dual.
    7. Record the session's content_sha256 in the sidecar.

One bad session must not break the run. Provider errors are caught
per-session, logged with the session id, and counted in the `failed`
stat. A truly broken provider (no auth) raises ProviderNotAvailable
before the loop starts so the user sees the fix-it text once, not 388
times.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl  # POSIX — present on macOS + Linux
except ImportError:  # pragma: no cover (Windows)
    fcntl = None  # type: ignore[assignment]

# Path setup so we can import sibling modules without packaging.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent))
sys.path.insert(0, str(_THIS.parent.parent / "memory"))

from _session_normalize import (  # type: ignore
    NormalizedSession,
    normalize_claude_session,
    normalize_codex_session,
)
import _digest_render as digest_render  # type: ignore
from llm_providers import LLMProvider, resolve_provider  # type: ignore
from llm_providers.base import LLMError, ProviderNotAvailable  # type: ignore

# Redaction
from redact import (  # type: ignore
    BUILTIN_PATTERNS,
    MULTILINE_PATTERNS,
    load_private_patterns,
)
from redact_jsonl import redact_string  # type: ignore


# Sessions up to this many tokens go single-pass; bigger sessions trigger
# map-reduce. Module-level so tests can monkeypatch a lower threshold.
SINGLE_PASS_TOKEN_LIMIT = 60_000

# Per-chunk target when splitting. Set to 120K so even multi-MB outlier
# sessions land in a small number of chunks. Originally 30K, which made
# the 14MB outlier need ~120 chunks → cumulative wall time hours.
# Haiku's context is 200K so 120K leaves headroom for system prompt +
# schema instructions.
CHUNK_TOKEN_TARGET = 120_000


# ---------------------------------------------------------------------------
# Schema the LLM must satisfy
# ---------------------------------------------------------------------------

DIGEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "domain_tags": {"type": "array", "items": {"type": "string"}},
        "what_user_did": {"type": "string"},
        "what_was_learned": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "outcome": {"type": "string"},
        "salience": {"type": "integer"},
    },
    "required": [
        "title", "domain_tags", "what_user_did", "what_was_learned",
        "decisions", "files_touched", "outcome", "salience",
    ],
}


# ---------------------------------------------------------------------------
# System prompt — framework-pure (zero org references)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You summarize a single coding-assistant session into a structured digest used for long-term recall.

Goals:
  - Capture WHAT the user did and WHAT was learned in a way that's still useful in 3 months when they ask "have I dealt with this before?"
  - Extract domain tags from the session content itself — do not invent tags from a fixed taxonomy.
  - Be concise; the digest is a recall hint, not a transcript.

Rules:
  - Return ONLY a JSON object matching the required schema. No prose, no markdown fences.
  - `title`: ≤ 60 chars, descriptive.
  - `domain_tags`: 1-5 tags, lowercase, hyphenated, derived from the session.
  - `what_user_did`: 1-3 sentences in plain prose.
  - `what_was_learned`: 1-3 sentences. Durable insights only — not session-specific events.
  - `decisions`: 0-5 items. Concrete decisions the user committed to.
  - `files_touched`: paths edited or written (deduped).
  - `outcome`: one of "completed", "abandoned", "blocked", "in-progress".
  - `salience`: integer 1-10, where 10 = "I will definitely want to recall this".
"""

CHUNK_SYSTEM_PROMPT = """You summarize ONE CHUNK of a longer coding-assistant session. Other chunks are being summarized in parallel and a final pass will merge them.

Return ONLY a JSON object matching the required schema. Same shape and rules as a full-session digest, but scoped to THIS chunk only. Pick `salience` for THIS chunk's content."""

MERGE_SYSTEM_PROMPT = """You merge several per-chunk digests of the SAME session into ONE final digest. Inputs are JSON; output is JSON, same schema.

Rules:
  - Title: capture the session as a whole, not a single chunk.
  - domain_tags: union, dedup, keep at most 5.
  - what_user_did / what_was_learned: synthesize across chunks, not concatenate.
  - decisions, files_touched: union, dedup.
  - outcome: pick the chunk that best describes the session's end state.
  - salience: max of input saliences (the session is as important as its most-important moment).
Return JSON only — same required keys."""


# ---------------------------------------------------------------------------
# Walking sessions
# ---------------------------------------------------------------------------

def iter_claude_sessions(
    projects_root: Path,
) -> Iterator[NormalizedSession]:
    """Yield NormalizedSession for every <slug>/<uuid>.jsonl under
    `projects_root`. Skips files that yield None (no conversational
    turns) and any that can't be parsed at all. Output order is
    deterministic (sorted by slug + filename)."""
    if not projects_root.is_dir():
        return
    for slug_dir in sorted(projects_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for jsonl in sorted(slug_dir.glob("*.jsonl")):
            try:
                ns = normalize_claude_session(jsonl,
                                              project_slug=slug_dir.name)
            except Exception:
                continue
            if ns is None:
                continue
            yield ns


def iter_codex_sessions(
    codex_root: Path,
) -> Iterator[NormalizedSession]:
    """Yield NormalizedSession for every rollout-*.jsonl under
    `codex_root/sessions/`. Returns nothing if `codex_root` is missing."""
    sessions_dir = codex_root / "sessions"
    if not sessions_dir.is_dir():
        # Allow caller to pass either the parent or `sessions` directly.
        if codex_root.name == "sessions" and codex_root.is_dir():
            sessions_dir = codex_root
        else:
            return
    for path in sorted(sessions_dir.rglob("rollout-*.jsonl")):
        try:
            ns = normalize_codex_session(path)
        except Exception:
            continue
        if ns is None:
            continue
        # Carry the on-disk file path for sidecar SHA computation.
        ns.__dict__["_source_path"] = path
        yield ns


# ---------------------------------------------------------------------------
# Sidecar (content-SHA dedup)
# ---------------------------------------------------------------------------

def _sidecar_path(brain_root: Path) -> Path:
    return brain_root / "memory" / "episodic" / "digests" / "_imported.jsonl"


def _load_sidecar(brain_root: Path) -> dict[str, str]:
    """Return {session_id: content_sha256}. Tolerates partial/missing
    files."""
    out: dict[str, str] = {}
    sp = _sidecar_path(brain_root)
    if not sp.is_file():
        return out
    try:
        for line in sp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = e.get("session_id")
            sha = e.get("content_sha256")
            if isinstance(sid, str) and isinstance(sha, str):
                # Last write wins (per-session sidecar update).
                out[sid] = sha
    except OSError:
        pass
    return out


def _append_sidecar(brain_root: Path, entry: dict) -> None:
    sp = _sidecar_path(brain_root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    with open(sp, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _session_source_path(ns: NormalizedSession,
                          claude_root: Path | None) -> Path | None:
    """Recover the on-disk path for a session. iter_codex_sessions
    stashes it; for Claude we reconstruct from project_slug + sid."""
    p = ns.__dict__.get("_source_path")
    if isinstance(p, Path):
        return p
    if ns.source == "claude" and claude_root and ns.project_slug:
        # File name is "<sid>.jsonl" by convention.
        candidate = claude_root / ns.project_slug / f"{ns.session_id}.jsonl"
        return candidate if candidate.is_file() else None
    return None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_PATTERN_CACHE: dict[str, list] = {}


def _redact_text(text: str, brain_root: Path) -> str:
    """Single-call redaction wrapper. Uses the same patterns as the
    JSONL adapter PLUS an entropy sweep so opaque 32+ char tokens that
    don't match a named vendor pattern are still scrubbed.

    Patterns are cached per brain_root so a long backfill doesn't re-read
    `redact-private.txt` once per session. A load failure for the private
    patterns file is logged once to stderr — we keep going with builtin
    coverage rather than failing closed (which would skip every digest
    forever on a single typo in a user-provided regex)."""
    key = str(brain_root)
    patterns = _PATTERN_CACHE.get(key)
    if patterns is None:
        patterns = list(BUILTIN_PATTERNS) + list(MULTILINE_PATTERNS)
        try:
            patterns += list(load_private_patterns(brain_root))
        except Exception as e:
            sys.stderr.write(
                f"WARN: digest adapter: load_private_patterns failed "
                f"({type(e).__name__}: {e}); using builtin patterns only\n"
            )
        _PATTERN_CACHE[key] = patterns
    redacted, _hits = redact_string(text, patterns, entropy_threshold=4.5)
    return redacted


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

# Tool-call input keys whose VALUES are safe to include in the digest
# prompt. Limited to path-like fields so the LLM can populate
# `files_touched` accurately. Command bodies and arbitrary inputs stay
# out to avoid prompt-injection surface.
_SAFE_TOOL_INPUT_KEYS = {
    "file_path", "path", "notebook_path", "dir", "directory",
}


def _format_messages_for_prompt(messages: list, brain_root: Path) -> str:
    parts: list[str] = []
    for m in messages:
        role = getattr(m, "role", None) or "user"
        text = _redact_text(getattr(m, "text", "") or "", brain_root)
        parts.append(f"[{role}]\n{text}")
        tool_calls = getattr(m, "tool_calls", None) or []
        if tool_calls:
            tcs = []
            for tc in tool_calls:
                name = tc.get("name", "?")
                inp = tc.get("input")
                if isinstance(inp, dict):
                    # Include path-like values so files_touched can be
                    # accurate. Redact each value to scrub embedded
                    # secrets / tokens.
                    safe_parts = []
                    other_keys = []
                    for k, v in inp.items():
                        if k in _SAFE_TOOL_INPUT_KEYS and isinstance(v, str):
                            safe_parts.append(
                                f"{k}={_redact_text(v, brain_root)}"
                            )
                        else:
                            other_keys.append(k)
                    safe_str = ", ".join(safe_parts) if safe_parts else ""
                    other_str = ", ".join(sorted(other_keys)) if other_keys \
                                else ""
                    if safe_str and other_str:
                        tcs.append(f"  - {name}({safe_str}; +{other_str})")
                    elif safe_str:
                        tcs.append(f"  - {name}({safe_str})")
                    else:
                        tcs.append(f"  - {name}({other_str})")
                else:
                    tcs.append(f"  - {name}")
            parts.append("[tool_calls]\n" + "\n".join(tcs))
    return "\n\n".join(parts)


def _chunk_messages(messages: list, target_tokens: int) -> list[list]:
    """Split messages into chunks whose ~token estimates stay below
    `target_tokens`. Preserves order; never splits inside a message."""
    chunks: list[list] = [[]]
    used = 0
    for m in messages:
        t = (len(getattr(m, "text", "") or "") // 4) + 1
        if used + t > target_tokens and chunks[-1]:
            chunks.append([])
            used = 0
        chunks[-1].append(m)
        used += t
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def _summarize_single(
    ns: NormalizedSession, provider: LLMProvider, brain_root: Path,
) -> dict:
    transcript = _format_messages_for_prompt(ns.messages, brain_root)
    prompt = (
        f"Session id: {ns.session_id}\n"
        f"Source: {ns.source}\n"
        f"Started: {ns.started_at}\n"
        f"Model: {ns.model or ''}\n\n"
        f"TRANSCRIPT:\n{transcript}\n"
    )
    result = provider.invoke(
        SYSTEM_PROMPT, prompt,
        json_schema=DIGEST_SCHEMA,
        timeout_s=180,
    )
    if result.parsed_json is None:
        raise LLMError("provider returned no parsed digest")
    return result.parsed_json


def _summarize_chunks(
    ns: NormalizedSession, provider: LLMProvider, brain_root: Path,
) -> dict:
    # Chunk target scales with the single-pass limit so a session that
    # crossed the threshold actually splits into ≥2 chunks. Cap at
    # CHUNK_TOKEN_TARGET so production runs use the production target;
    # use the smaller of (production target, half the single-pass limit).
    chunk_target = min(CHUNK_TOKEN_TARGET,
                       max(1, SINGLE_PASS_TOKEN_LIMIT // 2))
    chunks = _chunk_messages(ns.messages, chunk_target)
    if len(chunks) < 2:
        # Threshold edge case — fall back to single-pass
        return _summarize_single(ns, provider, brain_root)

    chunk_digests: list[dict] = []
    for i, chunk in enumerate(chunks):
        transcript = _format_messages_for_prompt(chunk, brain_root)
        prompt = (
            f"Session id: {ns.session_id} (CHUNK {i+1}/{len(chunks)})\n"
            f"Source: {ns.source}\n\n"
            f"TRANSCRIPT (chunk {i+1}):\n{transcript}\n"
        )
        result = provider.invoke(
            CHUNK_SYSTEM_PROMPT, prompt,
            json_schema=DIGEST_SCHEMA, timeout_s=180,
        )
        if result.parsed_json is None:
            raise LLMError(f"chunk {i+1} returned no parsed digest")
        chunk_digests.append(result.parsed_json)

    # Final merge
    merge_prompt = (
        f"Session id: {ns.session_id}\n"
        f"Per-chunk digests to merge ({len(chunk_digests)} chunks):\n"
        + json.dumps(chunk_digests, indent=2)
    )
    merged = provider.invoke(
        MERGE_SYSTEM_PROMPT, merge_prompt,
        json_schema=DIGEST_SCHEMA, timeout_s=180,
    )
    if merged.parsed_json is None:
        raise LLMError("merge call returned no parsed digest")
    return merged.parsed_json


# ---------------------------------------------------------------------------
# Backfill orchestrator
# ---------------------------------------------------------------------------

def _session_meta(ns: NormalizedSession) -> dict:
    return {
        "session_id": ns.session_id,
        "source": ns.source,
        "started_at": ns.started_at,
        "ended_at": ns.ended_at,
        "cwd": ns.cwd,
        "git_branch": ns.git_branch,
        "project_slug": ns.project_slug,
        "model": ns.model,
    }


@contextlib.contextmanager
def _backfill_lock(brain_root: Path, log: Callable[[str], None]):
    """Hold an exclusive flock on a sentinel so two backfills don't race
    on the sidecar / episodic / markdown writes. Non-blocking acquire —
    a concurrent run just skips (returns control to caller's empty-body
    use). On non-POSIX platforms (no fcntl) we degrade to a best-effort
    no-op + warn the operator."""
    lock_path = brain_root / ".digest-backfill.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        log("WARN: fcntl not available; running without backfill lock")
        yield True
        return
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("digest backfill already in progress; skipping this run")
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def backfill(
    *,
    brain_root: Path,
    projects_root: Path | None,
    codex_root: Path | None,
    provider: LLMProvider | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> dict:
    """Walk all configured sources and produce digests. Returns stats.

    Concurrency: holds an exclusive lock on `<brain>/.digest-backfill.lock`
    so two concurrent runs don't race on the sidecar / episodic /
    markdown writes. A second run while one is in progress is a clean
    no-op (NOT an error — matches sync_claude_extras' lock semantics)."""
    brain_root = Path(brain_root)
    brain_root.mkdir(parents=True, exist_ok=True)
    if provider is None:
        provider = resolve_provider()
    # Surface unavailability EARLY so a misconfigured user sees the
    # fix-it text once, not 388 times.
    ok, reason = provider.is_available()
    if not ok:
        raise ProviderNotAvailable({provider.name: reason or "unavailable"})

    stats = {"discovered": 0, "digests_written": 0,
             "skipped_idempotent": 0, "failed": 0,
             "races_skipped": 0,
             "tokens_in_total": 0, "tokens_out_total": 0}

    ep_path = brain_root / "memory" / "episodic" / "digests" \
              / "AGENT_LEARNINGS.jsonl"
    md_dir = brain_root / "memory" / "semantic" / "digests"

    with _backfill_lock(brain_root, log) as acquired:
        if not acquired:
            return stats

        # Re-read sidecar AFTER acquiring the lock so a freshly-finished
        # backfill in the other process is reflected.
        seen = _load_sidecar(brain_root)

        def _process(ns: NormalizedSession,
                     source_path: Path | None) -> None:
            stats["discovered"] += 1
            sha_before = ""
            if source_path is not None:
                sha_before = _file_sha256(source_path)
            if sha_before and seen.get(ns.session_id) == sha_before:
                stats["skipped_idempotent"] += 1
                return
            try:
                if ns.raw_token_estimate <= SINGLE_PASS_TOKEN_LIMIT:
                    digest = _summarize_single(ns, provider, brain_root)
                else:
                    digest = _summarize_chunks(ns, provider, brain_root)
            except LLMError as e:
                stats["failed"] += 1
                log(f"[{ns.source}] {ns.session_id} LLM FAIL: {e}")
                return
            except Exception as e:
                # Broad catch is intentional: per-session isolation is
                # the contract. A malformed normalizer output, an
                # unexpected provider exception, an OOM in the prompt
                # builder — none of these should kill the loop.
                stats["failed"] += 1
                log(f"[{ns.source}] {ns.session_id} UNEXPECTED "
                    f"{type(e).__name__}: {e}")
                return

            # Race detector: if the transcript file grew while we were
            # summarizing, the SHA changed. We still write the digest
            # (it represents the state we summarized), but we DO NOT
            # update the sidecar with the after-SHA — that would mark
            # the now-larger file as "digested" when in fact the new
            # tail wasn't seen. Sidecar gets sha_before so the next run
            # sees the after-SHA as different and re-digests.
            sha_after = sha_before
            if source_path is not None:
                sha_after_check = _file_sha256(source_path)
                if sha_after_check and sha_after_check != sha_before:
                    stats["races_skipped"] += 1
                    log(f"[{ns.source}] {ns.session_id} grew during "
                        f"digest — wrote digest, sidecar holds old SHA "
                        f"so next run picks up the tail")

            meta = _session_meta(ns)
            try:
                digest_render.write_dual(
                    digest, meta,
                    episodic_path=ep_path, markdown_dir=md_dir,
                )
            except Exception as e:
                stats["failed"] += 1
                log(f"[{ns.source}] {ns.session_id} WRITE FAIL "
                    f"{type(e).__name__}: {e}")
                return

            try:
                _append_sidecar(brain_root, {
                    "session_id": ns.session_id,
                    "source": ns.source,
                    "content_sha256": sha_before,
                    "source_path": str(source_path) if source_path else None,
                    "digest_ts": ns.ended_at or ns.started_at or "",
                })
            except Exception as e:
                # Sidecar append failed AFTER digest was written. Worst
                # case: next run summarizes the same session again
                # (idempotent waste, not data loss). Log + continue.
                log(f"[{ns.source}] {ns.session_id} SIDECAR APPEND FAIL "
                    f"{type(e).__name__}: {e}")
            seen[ns.session_id] = sha_before
            stats["digests_written"] += 1
            title = digest.get("title", "")[:60]
            log(f"[{ns.source}] {ns.session_id} → {title!r}")

        if projects_root is not None:
            projects_root = Path(projects_root)
            for ns in iter_claude_sessions(projects_root):
                sp = _session_source_path(ns, claude_root=projects_root)
                _process(ns, sp)

        if codex_root is not None:
            codex_root = Path(codex_root)
            for ns in iter_codex_sessions(codex_root):
                sp = _session_source_path(ns, claude_root=None)
                _process(ns, sp)

    return stats
