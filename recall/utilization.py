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

Scaffold module: dataclass + constants are real; every function below is
a stub (``raise NotImplementedError("scaffold")``). See
tests/recall/test_stats_utilization.py for the pinned contract this
module must eventually satisfy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    """
    raise NotImplementedError("scaffold")


def find_transcript(transcripts_dir: Path, session_id: str) -> Path | None:
    """First `<slug>/<session_id>.jsonl` under `transcripts_dir` whose
    path parts exclude "subagents" — a subagent's copy of the transcript
    is not the session the hook fired in."""
    raise NotImplementedError("scaffold")


def load_transcript(path: Path) -> list[dict]:
    """Parse one transcript file into a list of JSON records, skipping
    malformed lines."""
    raise NotImplementedError("scaffold")


def find_injection_attachment(
    lines: list[dict], ts_ms: int, *, window_ms: int = 120_000
) -> int | None:
    """Index of the `UserPromptSubmit` attachment record nearest `ts_ms`
    within `window_ms`, or None if none qualifies."""
    raise NotImplementedError("scaffold")


def parse_injection_block(content: str) -> list[dict]:
    """Parse an auto-recall injection block into
    `[{"path": str, "score": float, "excerpt": str}, ...]`."""
    raise NotImplementedError("scaffold")


def tool_refs_after(lines: list[dict], start_idx: int) -> list[tuple[str, str]]:
    """Every `(tool_name, path_or_command)` tool_use reference after
    `start_idx`, including sidechain (subagent) records."""
    raise NotImplementedError("scaffold")


def doc_use(doc_rel: str, refs: list[tuple[str, str]], brain_root: Path) -> str | None:
    """First tool name in `refs` that counts as evidence `doc_rel` was
    opened, or None."""
    raise NotImplementedError("scaffold")


def extract_prompt(lines: list[dict], idx: int) -> str | None:
    """Walk `parentUuid` back from `idx` to the human prompt that
    triggered this injection (forward-scan fallback when the chain is
    broken); None for slash/hash commands or when nothing qualifies."""
    raise NotImplementedError("scaffold")


def extract_response(lines: list[dict], idx: int, prompt: str) -> str:
    """Render the assistant's response after `idx` up to (not including)
    the next human prompt, as `[text] ...` / `[tool_use NAME] ...` lines."""
    raise NotImplementedError("scaffold")


def norm_brain_path(p: str, brain_root: Path) -> str:
    """Normalize a transcript-side path for comparison against a
    brain-relative `x_paths` entry: expanduser, strip a trailing line
    reference, strip `brain_root` prefix when present."""
    raise NotImplementedError("scaffold")


# ---------------------------------------------------------------------------
# LLM-judge sample
# ---------------------------------------------------------------------------


def select_sample(cases: list[dict], n: int, seed: int) -> list[dict]:
    """Seeded selection: one case per (session_id, docset), at most 2 per
    session and 3 per docset, capped at `n`."""
    raise NotImplementedError("scaffold")


def write_sample(cases: list[dict], path: Path) -> int:
    """Write `cases` to `path` as indented JSON, atomically (temp file +
    replace). Returns the number of cases written."""
    raise NotImplementedError("scaffold")


# ---------------------------------------------------------------------------
# Human renderer
# ---------------------------------------------------------------------------


def render_utilization(r: UtilizationReport) -> str:
    """Format `r` as the user-facing `recall stats --utilization` block."""
    raise NotImplementedError("scaffold")
