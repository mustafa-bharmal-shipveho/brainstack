"""`runtime/dream_status.json` — the dream cycle's machine-readable receipt (S5).

Today the only trace a dream cycle leaves is an untimestamped line in
`~/.agent/dream.log`. That is why a month of cycles failing with
`llm_errors=provider_unavailable=3` went unnoticed: nothing could tell
"ran an hour ago, clean" from "last ran in July".

`run_dream_cycle()` and `run()` now write a status file the health check
reads:

    {"schema_version": 1, "ts": "2026-09-04T07:00:12Z", "namespace": "default",
     "ok": true, "summary": "dream cycle: patterns=0 ...", "staged": 0,
     "kept": 43770, "archived": 25, "consolidate_claims": 0, "llm_calls": 3,
     "llm_errors": {"provider_unavailable": 3}, "error": null}

The counters are the cycle's own tally, handed to the status writer
directly — the printed line is a rendering of the same numbers, not the
source of them. Re-parsing the text is the fallback for a caller holding
nothing but `dream.log`, and that parser is pinned against
`recall.health._parse_llm_errors` so the two readings of that text agree.

Hermetic: tmp brain via BRAIN_ROOT plus patched module paths, never
`~/.agent`. HOME and XDG_CONFIG_HOME are redirected so the consolidator
cannot pick up the developer's real `extractors.toml` and call an LLM.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / "agent" / "memory"
HARNESS_DIR = REPO_ROOT / "agent" / "harness"

for _d in (MEMORY_DIR, HARNESS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

pytest.importorskip("fcntl")

STATUS_REL = Path("runtime") / "dream_status.json"


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def auto_dream():
    """The dream module lives in agent/memory/, which is not a package."""
    return __import__("auto_dream")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def make_brain(brain: Path) -> Path:
    """Minimal brain layout (shape from tests/test_dream_runner.py)."""
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "episodic" / "snapshots").mkdir()
    (brain / "memory" / "working").mkdir()
    (brain / "memory" / "candidates").mkdir()
    (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
    (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").touch()
    return brain


def _seed_episodic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _entry(idx: int) -> dict:
    return {
        "id": f"e-{idx}",
        "timestamp": _iso_days_ago(1),
        "action": f"ran step {idx}",
        "detail": f"detail for step {idx}",
        "result": "success",
    }


def _patch_dream_globals(monkeypatch, auto_dream, brain: Path) -> None:
    """Point `run_dream_cycle()`'s module-level paths at the tmp brain."""
    mem = brain / "memory"
    episodic = mem / "episodic" / "AGENT_LEARNINGS.jsonl"
    monkeypatch.setenv("BRAIN_ROOT", str(brain))
    monkeypatch.setattr(auto_dream, "ROOT", str(mem))
    monkeypatch.setattr(auto_dream, "EPISODIC", str(episodic))
    monkeypatch.setattr(auto_dream, "EPISODIC_LOCK", str(episodic) + ".lock")
    monkeypatch.setattr(auto_dream, "CANDIDATES", str(mem / "candidates"))
    monkeypatch.setattr(auto_dream, "SEMANTIC", str(mem / "semantic"))
    monkeypatch.setattr(
        auto_dream, "REVIEW_QUEUE", str(mem / "working" / "REVIEW_QUEUE.md")
    )


def _read_status(brain: Path) -> dict:
    path = brain / STATUS_REL
    assert path.is_file(), (
        f"{STATUS_REL} was not written; runtime dir holds "
        f"{[p.name for p in (brain / 'runtime').glob('*')] if (brain / 'runtime').is_dir() else 'nothing'}"
    )
    return json.loads(path.read_text())


def _parse_ts(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


def test_run_dream_cycle_writes_dream_status_json(tmp_path, isolated_home,
                                                  auto_dream, monkeypatch):
    """A completed cycle leaves the full schema, with counters that agree
    with the summary line it printed."""
    brain = make_brain(tmp_path / ".agent")
    _patch_dream_globals(monkeypatch, auto_dream, brain)
    _seed_episodic(brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl",
                   [_entry(0), _entry(1)])

    before = datetime.now(timezone.utc)
    auto_dream.run_dream_cycle()

    status = _read_status(brain)
    assert set(status) >= {
        "schema_version", "ts", "namespace", "ok", "summary", "staged",
        "kept", "archived", "consolidate_claims", "llm_calls", "llm_errors",
        "error",
    }, f"missing schema keys: {sorted(status)}"
    assert status["schema_version"] == 1
    assert status["namespace"] == "default"
    assert status["ok"] is True
    assert status["error"] is None
    assert status["summary"].startswith("dream cycle:")

    assert _parse_ts(status["ts"]) >= before - timedelta(seconds=5)

    # Both entries are one day old, so decay keeps them.
    assert status["kept"] == 2
    assert status["archived"] == 0
    assert isinstance(status["staged"], int)
    assert isinstance(status["consolidate_claims"], int)
    assert isinstance(status["llm_calls"], int)
    assert status["llm_errors"] == {}, "a clean run must record no LLM errors"


def test_dream_status_written_on_no_entries_path(tmp_path, isolated_home,
                                                 auto_dream, monkeypatch):
    """The early return on an empty episodic stream is the path that runs
    on a fresh install — it must still prove the cycle fired, or the health
    check reads it as 'dream never ran'."""
    brain = make_brain(tmp_path / ".agent")
    _patch_dream_globals(monkeypatch, auto_dream, brain)

    auto_dream.run_dream_cycle()

    status = _read_status(brain)
    assert status["schema_version"] == 1
    assert status["namespace"] == "default"
    assert status["ok"] is True
    assert status["error"] is None
    assert "no entries" in status["summary"]
    assert status["kept"] == 0
    assert status["staged"] == 0
    assert status["archived"] == 0
    assert status["consolidate_claims"] == 0
    assert status["llm_errors"] == {}


def test_dream_status_parses_llm_errors_from_summary(tmp_path, auto_dream):
    """`llm_calls=N llm_errors=tag=count,tag=count` is what the extractors
    append to the summary; the health check needs it as structured data."""
    brain = tmp_path / ".agent"
    summary = (
        "dream cycle: patterns=4 staged=2 prefiltered_out=1 pending_review=3 "
        "archived=25 kept=43770 burst_skipped=0 activity_log_skipped=0 "
        "activity_log_swept=0 consolidate_events=12 consolidate_claims=7 "
        "consolidate_supersedes=0 consolidate_retracts=0 projected=1 "
        "llm_calls=3 llm_errors=provider_unavailable=3,rate_limited=1"
    )

    auto_dream._write_cycle_status(str(brain), "default", summary)

    status = _read_status(brain)
    assert status["summary"] == summary
    assert status["staged"] == 2
    assert status["kept"] == 43770
    assert status["archived"] == 25
    assert status["consolidate_claims"] == 7
    assert status["llm_calls"] == 3
    assert status["llm_errors"] == {"provider_unavailable": 3, "rate_limited": 1}
    assert status["ok"] is True
    assert status["error"] is None


def test_dream_status_counters_come_from_the_cycle_not_the_line(
        tmp_path, isolated_home, auto_dream, monkeypatch):
    """The cycle hands its own tally to the status writer. Proof: a
    summary line whose text says something else entirely still yields the
    real numbers, because nothing re-reads the line."""
    brain = make_brain(tmp_path / ".agent")
    _patch_dream_globals(monkeypatch, auto_dream, brain)

    auto_dream._write_cycle_status(
        str(brain), "default",
        "dream cycle: staged=999 kept=999 archived=999 "
        "consolidate_claims=999 llm_calls=999 llm_errors=bogus=999",
        counters={"staged": 2, "kept": 7, "archived": 1,
                  "consolidate_claims": 3, "llm_calls": 4,
                  "llm_errors": {"timeout": 1}},
    )

    status = _read_status(brain)
    assert status["staged"] == 2
    assert status["kept"] == 7
    assert status["archived"] == 1
    assert status["consolidate_claims"] == 3
    assert status["llm_calls"] == 4
    assert status["llm_errors"] == {"timeout": 1}


# The shapes `LLMExtractor.error_summary()` actually emits, plus a clean
# line that carries no `llm_errors=` token at all.
_LLM_ERROR_LINES = (
    "dream cycle: patterns=4 staged=2 prefiltered_out=1 pending_review=3 "
    "archived=25 kept=43770 consolidate_claims=7 "
    "llm_calls=3 llm_errors=provider_unavailable=3,rate_limited=1",
    "dream cycle: staged=1 kept=9 archived=0 "
    "llm_calls=1 llm_errors=timeout=1 lint_marked=0",
    "dream cycle: staged=0 kept=0 archived=0 consolidate_claims=0",
)


@pytest.mark.parametrize("line", _LLM_ERROR_LINES)
def test_llm_error_parsers_agree(auto_dream, line):
    """`auto_dream._parse_summary_counters` and
    `recall.health._parse_llm_errors` both read `dream.log` text when no
    status file is available. Pin them on one fixture so they can never
    disagree about what the last cycle failed with."""
    from recall.health import _parse_llm_errors

    assert (
        auto_dream._parse_summary_counters(line)["llm_errors"]
        == _parse_llm_errors(line)
    )


def test_dream_status_records_error_when_cycle_fails(tmp_path, auto_dream):
    """`ok=false` plus the error text is how a crashed cycle stays visible
    instead of looking like a stale-but-healthy run."""
    brain = tmp_path / ".agent"

    auto_dream._write_cycle_status(
        str(brain), "default", "", ok=False, error="OSError('disk full')",
    )

    status = _read_status(brain)
    assert status["ok"] is False
    assert "disk full" in status["error"]
    assert status["schema_version"] == 1


def test_namespaced_run_writes_status_with_namespace(tmp_path, isolated_home,
                                                     auto_dream):
    """`run(namespace=...)` stamps the namespace it processed, so a status
    written by the inbox cycle is not read as the default one."""
    brain = make_brain(tmp_path / ".agent")
    _seed_episodic(
        brain / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl",
        [_entry(0), _entry(1)],
    )

    auto_dream.run(brain_root=str(brain), namespace="inbox", dry_run=False)

    status = _read_status(brain)
    assert status["schema_version"] == 1
    assert status["namespace"] == "inbox"
    assert status["ok"] is True
    assert status["error"] is None
