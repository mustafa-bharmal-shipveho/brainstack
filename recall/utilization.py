"""`recall stats --utilization` — did anyone OPEN the injected docs?

`recall stats` can only report how many docs auto-recall injected. This
module answers the harder question: take every schema-1.2 AutoRecall
``hit`` (which carries ``x_paths``, the brain-relative paths it injected),
find the Claude Code transcript for that session, locate the injection
attachment near the event timestamp, and look at what the model did next.
A doc counts as USED when a later tool call in the same session reads it
(``Read``/``Grep``/``Glob`` on the path, or a ``Bash`` command that
mentions it).

Two outputs:
  (a) a mechanical percentage — injected docs later opened in-session;
  (b) a JSON sample of cases (prompt, injected docs, response) shaped
      exactly like the ad-hoc ``tools/sample_utilization.py`` script, so
      an LLM judge can grade relevance on the same input.

Legacy hits (schema 1.1, or 1.2 without ``x_paths``) cannot be joined —
they never logged which docs they injected — so they are counted and
reported separately rather than silently scored as unused.

The mechanical metric is a floor, not a verdict: a doc the model read in
its own context window and never re-opened leaves no trace here. Output
copy says "opened later in-session" for that reason.
"""
from __future__ import annotations

import bisect
import datetime
import json
import os
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Optional

from recall._coerce import as_int as _as_int
from recall._coerce import as_list as _as_list
from recall._coerce import format_window as _format_window
from recall.fsutil import atomic_write_text

# The hook's injection banner, as it appears in the transcript attachment.
# `docs?` because the header pluralizes the noun: "1 doc surfaced" for a
# single result, "2 docs surfaced" otherwise.
_HEADER_RE = re.compile(r"auto-recall: (\d+) docs? surfaced in (\d+)ms")
# One `## <path> (score X) …` section wrapped in start/end markers.
_DOC_RE = re.compile(
    r"## (\S+\.md) \(score ([0-9.]+)\).*?"
    r"\[recall-doc-\d+-start\]\n(.*?)\[recall-doc-\d+-end\]",
    re.S,
)
# `path.md:12` / `path.md:12:4` / `path.md:12,4` — editors and tool inputs
# append these; the brain path is everything before them.
_LINE_REF_RE = re.compile(r":\d+(?:[:,]\d+)*$")

# The tools whose inputs name a file we can compare against `x_paths`.
_PATH_INPUT = {"Read": "file_path", "Grep": "path", "Glob": "path"}

EXCERPT_MAX = 1200
PROMPT_MAX = 900
RESPONSE_MAX = 4500
PARENT_HOPS = 12
FORWARD_SCAN = 8
HUMAN_PROMPT_MIN_CHARS = 30
SAMPLE_PER_SESSION = 2
SAMPLE_PER_DOCSET = 3


@dataclass
class UtilizationReport:
    """Result of joining AutoRecall hits to Claude Code transcripts."""

    since_ts_ms: Optional[int] = None
    hit_events: int = 0
    joined_events: int = 0
    unjoined_events: int = 0
    legacy_hits_ignored: int = 0
    injected_docs: int = 0
    used_docs: int = 0
    used_pct: float = 0.0
    used_by_tool: dict[str, int] = field(default_factory=dict)
    used_docs_top: list[tuple[str, int]] = field(default_factory=list)
    sample_written: int = 0
    sample_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-session transcript index
# ---------------------------------------------------------------------------


@dataclass
class TranscriptIndex:
    """The three lookups the join needs, built once per transcript.

    Every hit in a session used to rescan the whole transcript three
    times — once to find its injection attachment, once to collect the
    tool calls after it, once to rebuild the `uuid` map that walks back
    to the human prompt. A 207-hour session with 248 injections paid that
    O(hits x lines) three times over. The scan is the same; only the
    number of passes changes.

    ``attachments`` and ``tool_refs`` are in line order, which is what
    the readers below rely on: the attachment search reproduces the
    original first-wins tie-break, and ``tool_ref_positions`` is
    non-decreasing so a suffix can be sliced with `bisect`.
    """

    attachments: list[tuple[int, int, str]] = field(default_factory=list)
    by_uuid: dict[object, dict] = field(default_factory=dict)
    tool_ref_positions: list[int] = field(default_factory=list)
    tool_refs: list[tuple[str, str]] = field(default_factory=list)


def build_transcript_index(lines: list[dict]) -> TranscriptIndex:
    """One pass over `lines` producing every lookup the join needs."""
    index = TranscriptIndex()
    for i, rec in enumerate(lines):
        if not isinstance(rec, dict):
            continue
        uuid = rec.get("uuid")
        if uuid:
            # Last write wins on a duplicate uuid, as the dict
            # comprehension this replaced did.
            index.by_uuid[uuid] = rec
        content = _attachment_content(rec)
        if content is not None:
            ts = _iso_to_ms(rec.get("timestamp"))
            # An attachment with no parseable timestamp can't be joined
            # to an event, so it never enters the search.
            if ts is not None:
                index.attachments.append((i, ts, content))
        message = rec.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            ref = _tool_ref(block)
            if ref is not None:
                index.tool_ref_positions.append(i)
                index.tool_refs.append(ref)
    return index


def _tool_ref(block: object) -> tuple[str, str] | None:
    """`(tool_name, path_or_command)` for a tool_use block that names a
    file we can compare against `x_paths`, else None.

    A `Grep` without a path searches the cwd: no evidence about any
    particular doc, and not a reason to crash the join.
    """
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return None
    name = block.get("name")
    inputs = block.get("input")
    if not isinstance(name, str) or not isinstance(inputs, dict):
        return None
    if name in _PATH_INPUT:
        value = inputs.get(_PATH_INPUT[name])
    elif name == "Bash":
        value = inputs.get("command")
    else:
        return None
    return (name, value) if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Join algorithm
# ---------------------------------------------------------------------------


def compute_utilization(
    log_path: Path | str,
    transcripts_dir: Path | str,
    *,
    brain_root: Path,
    since_ts_ms: int | None = None,
    sample_n: int = 24,
    sample_out: Path | None = None,
    seed: int = 20260904,
) -> UtilizationReport:
    """Join schema-1.2 AutoRecall hits to their transcripts and compute
    the mechanical usage metric (+ optionally write an LLM-judge sample).

    Only sessions that actually had a hit are opened, so the cost scales
    with retrieval traffic rather than with the size of ~/.claude.
    """
    from recall.stats import is_v12, iter_auto_recall_records

    brain = Path(brain_root).expanduser()
    transcripts = Path(transcripts_dir)

    hits: list[dict] = []
    legacy_hits_ignored = 0
    for rec in iter_auto_recall_records(log_path, since_ts_ms=since_ts_ms):
        if rec.get("x_outcome") != "hit":
            continue
        if since_ts_ms is not None and _as_int(rec.get("ts_ms")) < since_ts_ms:
            continue
        if not is_v12(rec):
            # No `x_paths` means nothing to look for in the transcript.
            # Counted, never scored as unused.
            legacy_hits_ignored += 1
            continue
        hits.append(rec)

    by_session: dict[str, list[dict]] = {}
    for rec in sorted(hits, key=lambda r: _as_int(r.get("ts_ms"))):
        by_session.setdefault(str(rec.get("session_id") or ""), []).append(rec)

    joined = 0
    unjoined = 0
    injected_docs = 0
    used_docs = 0
    used_by_tool: Counter[str] = Counter()
    used_docs_top: Counter[str] = Counter()
    cases: list[dict] = []

    for session_id, recs in by_session.items():
        transcript = find_transcript(transcripts, session_id) if session_id else None
        if transcript is None:
            unjoined += len(recs)
            continue
        lines = load_transcript(transcript)
        # Built once, reused by every hit in this session. See
        # `TranscriptIndex` — the per-hit rescans were the cost that made
        # a long session quadratic.
        index = build_transcript_index(lines)
        for rec in recs:
            doc_paths = [str(p) for p in _as_list(rec.get("x_paths"))]
            idx = find_injection_attachment(
                lines, _as_int(rec.get("ts_ms")),
                prefer_paths={norm_brain_path(p, brain) for p in doc_paths},
                brain_root=brain,
                index=index,
            )
            if idx is None:
                unjoined += 1
                continue
            joined += 1
            injected_docs += len(doc_paths)
            refs = tool_refs_after(lines, idx, index=index)
            for doc in doc_paths:
                tool = doc_use(doc, refs, brain)
                if tool:
                    used_docs += 1
                    used_by_tool[tool] += 1
                    used_docs_top[doc] += 1
            case = _build_case(lines, idx, session_id, transcript, index=index)
            if case is not None:
                cases.append(case)

    sample_written = 0
    sample_path: str | None = None
    if sample_out is not None:
        sample_path = str(sample_out)
        sample_written = write_sample(
            select_sample(cases, sample_n, seed), Path(sample_out)
        )

    return UtilizationReport(
        since_ts_ms=since_ts_ms,
        hit_events=len(hits),
        joined_events=joined,
        unjoined_events=unjoined,
        legacy_hits_ignored=legacy_hits_ignored,
        injected_docs=injected_docs,
        used_docs=used_docs,
        used_pct=100.0 * used_docs / injected_docs if injected_docs else 0.0,
        used_by_tool=dict(used_by_tool.most_common()),
        used_docs_top=used_docs_top.most_common(10),
        sample_written=sample_written,
        sample_path=sample_path,
    )


def find_transcript(transcripts_dir: Path, session_id: str) -> Path | None:
    """First `<slug>/<session_id>.jsonl` under `transcripts_dir` whose
    path parts exclude "subagents" — a subagent's copy of the transcript
    is not the session the hook fired in."""
    root = Path(transcripts_dir)
    if not root.is_dir():
        return None
    try:
        candidates = sorted(root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    for path in candidates:
        if "subagents" in path.parts:
            continue
        return path
    return None


def load_transcript(path: Path) -> list[dict]:
    """Parse one transcript file into a list of JSON records, skipping
    malformed lines (real transcripts carry truncated writes)."""
    out: list[dict] = []
    try:
        handle = Path(path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def find_injection_attachment(
    lines: list[dict],
    ts_ms: int,
    *,
    window_ms: int = 120_000,
    prefer_paths: set[str] | None = None,
    brain_root: Path | None = None,
    index: TranscriptIndex | None = None,
) -> int | None:
    """Index of the `UserPromptSubmit` attachment record nearest `ts_ms`
    within `window_ms`, or None if none qualifies.

    A chatty session can hold dozens of injections, so nearest-in-time is
    the join key. `prefer_paths` breaks an exact tie in favour of the
    attachment that actually rendered the event's docs.

    Pass `index` to reuse a `TranscriptIndex` across the hits of one
    session; without it, one is built for this call alone.
    """
    if index is None:
        index = build_transcript_index(lines)
    best: int | None = None
    best_delta: int | None = None
    best_content = ""
    for i, ts, content in index.attachments:
        delta = abs(ts - ts_ms)
        if delta > window_ms:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta, best_content = i, delta, content
        elif delta == best_delta and prefer_paths and brain_root is not None:
            if (_covers(content, prefer_paths, brain_root)
                    and not _covers(best_content, prefer_paths, brain_root)):
                best, best_content = i, content
    return best


def _attachment_content(rec: dict) -> str | None:
    """The auto-recall injection text of `rec`, or None if it isn't one."""
    if not isinstance(rec, dict) or rec.get("type") != "attachment":
        return None
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("hookName") != "UserPromptSubmit":
        return None
    content = att.get("content") or att.get("stdout") or ""
    if not isinstance(content, str) or not _HEADER_RE.search(content):
        return None
    return content


def _covers(content: str, wanted: set[str], brain_root: Path) -> bool:
    rendered = {norm_brain_path(d["path"], brain_root)
                for d in parse_injection_block(content)}
    return wanted.issubset(rendered)


def parse_injection_block(content: str) -> list[dict]:
    """Parse an auto-recall injection block into
    `[{"path": str, "score": float, "excerpt": str}, ...]`.

    Paths are kept verbatim (absolute, as the block renders them) so the
    judge sample shows the model what the model saw; normalization
    happens only where the block is compared to `x_paths`.
    """
    docs: list[dict] = []
    for m in _DOC_RE.finditer(content or ""):
        try:
            score = float(m.group(2))
        except ValueError:
            continue
        docs.append({
            "path": m.group(1),
            "score": score,
            "excerpt": m.group(3).strip()[:EXCERPT_MAX],
        })
    return docs


def tool_refs_after(lines: list[dict], start_idx: int, *,
                    index: TranscriptIndex | None = None
                    ) -> list[tuple[str, str]]:
    """Every `(tool_name, path_or_command)` tool_use reference after
    `start_idx`, in line order, including sidechain (subagent) records —
    a subagent reading the injected doc is still the injection paying off.

    With an `index` this is a suffix slice: the positions are
    non-decreasing, so `bisect_right` lands past every ref on
    `start_idx` itself, which the scan it replaced also excluded.
    """
    if index is None:
        index = build_transcript_index(lines)
    start = bisect.bisect_right(index.tool_ref_positions, start_idx)
    return index.tool_refs[start:]


def doc_use(doc_rel: str, refs: list[tuple[str, str]], brain_root: Path) -> str | None:
    """First tool name in `refs` that counts as evidence `doc_rel` was
    opened, or None. First match wins, so a doc read then grepped counts
    once, under the tool that opened it."""
    absolute = str(Path(brain_root) / doc_rel)
    for tool, value in refs:
        if tool == "Bash":
            if doc_rel in value or absolute in value:
                return tool
        elif norm_brain_path(value, brain_root) == doc_rel:
            return tool
    return None


def extract_prompt(lines: list[dict], idx: int, *,
                   index: TranscriptIndex | None = None) -> str | None:
    """Walk `parentUuid` back from `idx` to the human prompt that
    triggered this injection (forward-scan fallback when the chain is
    broken); None for slash/hash commands or when nothing qualifies.

    Pass `index` to reuse a session's uuid map instead of rebuilding it
    for every hit."""
    if index is None:
        index = build_transcript_index(lines)
    by_uuid = index.by_uuid
    prompt: str | None = None
    current: dict | None = lines[idx]
    hops = 0
    while current is not None and hops < PARENT_HOPS:
        parent = by_uuid.get(current.get("parentUuid"))
        hops += 1
        if parent is not None and _is_human(parent):
            prompt = _text_of(parent.get("message")).strip()
            break
        current = parent
    if not prompt:
        # Compaction drops `parentUuid`; the human turn is usually the
        # next thing on the wire.
        for j in range(idx + 1, min(len(lines), idx + 1 + FORWARD_SCAN)):
            if _is_human(lines[j]):
                prompt = _text_of(lines[j].get("message")).strip()
                break
    if not prompt:
        return None
    # `/agent-team …` and `# note` turns are commands, not questions —
    # grading retrieval relevance against them is noise.
    if prompt.startswith("/") or prompt.startswith("#"):
        return None
    return prompt


def extract_response(lines: list[dict], idx: int, prompt: str) -> str:
    """Render the assistant's response after `idx` up to (not including)
    the next human prompt, as `[text] ...` / `[tool_use NAME] ...` lines."""
    parts: list[str] = []
    total = 0
    for rec in lines[idx + 1:]:
        if _is_human(rec) and _text_of(rec.get("message")).strip() != prompt:
            break
        if rec.get("type") == "assistant" and not rec.get("isSidechain"):
            message = rec.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    rendered = "[text] " + str(block["text"]).strip()
                elif block.get("type") == "tool_use":
                    rendered = (f"[tool_use {block.get('name')}] "
                                + json.dumps(block.get("input"))[:300])
                else:
                    continue
                parts.append(rendered)
                total += len(rendered)
        if total > RESPONSE_MAX:
            break
    return "\n".join(parts)[:RESPONSE_MAX]


def norm_brain_path(p: str, brain_root: Path) -> str:
    """Normalize a transcript-side path for comparison against a
    brain-relative `x_paths` entry: expanduser, strip a trailing line
    reference, strip `brain_root` prefix when present.

    Both sides run through this, so an absolute tool input and the
    brain-relative path the hook logged collapse to the same string. A
    doc outside the brain root keeps its absolute path (that is what
    `x_paths` logs for it too)."""
    text = _LINE_REF_RE.sub("", str(p).strip())
    text = os.path.expanduser(text)
    root = str(Path(brain_root).expanduser())
    if text.startswith(root + os.sep):
        text = text[len(root) + 1:]
    return PurePath(text).as_posix()


def _build_case(lines: list[dict], idx: int, session_id: str,
                transcript: Path, *,
                index: TranscriptIndex | None = None) -> dict | None:
    """One LLM-judge case, in the exact shape of the ad-hoc
    `tools/sample_utilization.py` script. None when the turn can't be
    judged (no docs rendered, no human prompt, or no response)."""
    record = lines[idx]
    content = _attachment_content(record) or ""
    docs = parse_injection_block(content)
    if not docs:
        return None
    prompt = extract_prompt(lines, idx, index=index)
    if not prompt:
        return None
    response = extract_response(lines, idx, prompt)
    if not response:
        return None
    attachment = record.get("attachment") or {}
    return {
        "session_id": session_id,
        "project_dir": transcript.parent.name,
        "ts": record.get("timestamp"),
        "hook_ms": attachment.get("durationMs"),
        "docset": "|".join(sorted(d["path"] for d in docs)),
        "docs": docs,
        "prompt": prompt[:PROMPT_MAX],
        "response": response,
    }


# ---------------------------------------------------------------------------
# Transcript record predicates — shared with tools/sample_utilization.py
# ---------------------------------------------------------------------------


def _text_of(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_human(rec: object) -> bool:
    """A real user turn, not a tool result or a system-injected block."""
    if not isinstance(rec, dict):
        return False
    if rec.get("type") != "user" or rec.get("isSidechain"):
        return False
    message = rec.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return False
    text = _text_of(message).strip()
    return (bool(text)
            and not text.startswith("<")
            and not text.startswith("[Request interrupted")
            and len(text) >= HUMAN_PROMPT_MIN_CHARS)


def _iso_to_ms(iso: object) -> int | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# LLM-judge sample
# ---------------------------------------------------------------------------


def select_sample(cases: list[dict], n: int, seed: int) -> list[dict]:
    """Seeded selection: one case per (session_id, docset), at most 2 per
    session and 3 per docset, capped at `n`.

    Without the caps one chatty session (or one doc that matches
    everything) fills the judge set and the grade describes that session
    rather than retrieval.
    """
    pool = list(cases)
    random.Random(seed).shuffle(pool)
    seen: set[tuple[str, str]] = set()
    per_session: Counter[str] = Counter()
    per_docset: Counter[str] = Counter()
    picked: list[dict] = []
    for case in pool:
        session = str(case.get("session_id", ""))
        docset = str(case.get("docset", ""))
        key = (session, docset)
        if key in seen:
            continue
        if per_session[session] >= SAMPLE_PER_SESSION:
            continue
        if per_docset[docset] >= SAMPLE_PER_DOCSET:
            continue
        seen.add(key)
        per_session[session] += 1
        per_docset[docset] += 1
        picked.append(case)
    return picked[:n] if n >= 0 else picked


def write_sample(cases: list[dict], path: Path) -> int:
    """Write `cases` to `path` as indented JSON, atomically (temp file +
    replace). Returns the number of cases written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(cases, indent=1))
    return len(cases)


# ---------------------------------------------------------------------------
# Human renderer
# ---------------------------------------------------------------------------

_LABEL_WIDTH = 16


def render_utilization(r: UtilizationReport) -> str:
    """Format `r` as the user-facing `recall stats --utilization` block."""
    lines = [f"brainstack: auto-recall utilization{_format_window(r.since_ts_ms)}"]
    lines.append(_row(
        "Hits joined",
        f"{r.joined_events} / {r.hit_events} schema-1.2 hits had a transcript"
        f" ({r.unjoined_events} unjoined)"))
    docs = (f"{r.injected_docs} · opened later in-session:"
            f" {r.used_docs} ({r.used_pct:.1f}%)")
    if r.used_by_tool:
        docs += " — " + ", ".join(
            f"{tool} {n}" for tool, n in
            sorted(r.used_by_tool.items(), key=lambda kv: -kv[1]))
    lines.append(_row("Docs injected", docs))
    if r.used_docs_top:
        lines.append(_row("Most opened", ", ".join(
            f"{path} ({n})" for path, n in r.used_docs_top[:10])))
    if r.legacy_hits_ignored:
        lines.append(f"  Legacy hits ignored (no x_paths): {r.legacy_hits_ignored}")
    if r.sample_written and r.sample_path:
        lines.append(_row(
            "Sample",
            f"{r.sample_written} cases → {r.sample_path} (LLM-judge input)"))
    return "\n".join(lines)


def _row(label: str, value: str) -> str:
    return f"  {label + ':':<{_LABEL_WIDTH}}{value}"
