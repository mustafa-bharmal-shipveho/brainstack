"""Bounded incremental digests: per-run limit + wall-clock time budget.

Context: the hourly LaunchAgent (`sync_claude_extras.py`) used to run
`digest_cli.py incremental` under the same 600s timeout as the
near-instant session/misc mirror adapters. A single session digest costs
several minutes of real LLM time, so a busy hour with many new sessions
could never finish inside 600s and got SIGKILLed every tick — exit 1
forever, even though the session + misc mirrors succeeded, and the
digest backlog was invisible (progress is sidecar-idempotent, so nothing
was lost, but nothing SAID so either).

This file pins three contracts:

  1. `claude_session_digest_adapter.backfill()` accepts `limit` /
     `max_seconds` and stops CLEANLY (no partial LLM call, no kill)
     once either is exceeded — reporting `processed` / `pending` /
     `elapsed_s` / `budget_hit` instead of just stopping quietly.
  2. `digest_cli.py incremental` exposes `--limit` (default 3) /
     `--max-seconds` (default 1500) and prints the machine-readable
     `digests: processed=P pending=Q elapsed_s=E budget_hit=<bool>`
     line sync_claude_extras.py depends on.
  3. `sync_claude_extras.py` gives the digest step its OWN timeout
     (`BRAINSTACK_DIGEST_TIMEOUT_S`, default 1800s) separate from the
     600s adapter timeout, forwards `BRAINSTACK_DIGEST_LIMIT` /
     `BRAINSTACK_DIGEST_MAX_SECONDS` as `--limit`/`--max-seconds`, logs
     the summary line, writes `runtime/digest_status.json`, and exits 0
     when the digest step completed (even with pending > 0) — 1 only
     on a real failure (non-zero rc or a genuine timeout kill).

No real LLM is ever called: a fake in-process provider stands in for
digest_cli/adapter-level tests, and a fake `digest_cli.py` STUB script
(controlled entirely via env vars) stands in for sync_claude_extras'
subprocess boundary — the same "fake the sub-step" shape
`tests/test_new_adapters.py`'s `TestEndToEndCLI` and
`tests/test_session_digest.py`'s `TestCLI` already use.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter_mod():
    import claude_session_digest_adapter as adapter
    return adapter


@pytest.fixture
def cli_mod():
    import digest_cli
    return digest_cli


class _FakeProvider:
    """Deterministic in-memory provider. `sleep_s` simulates the several
    minutes of real LLM time a session digest costs, without an actual
    sleep of that length — tests use tiny values and tiny budgets so the
    ratio (not the absolute magnitude) drives the assertions."""
    name = "fake"
    default_model = "fake-1"

    def __init__(self, *, sleep_s: float = 0.0):
        self.calls = 0
        self._sleep_s = sleep_s
        self._response = {
            "title": "Synthetic digest title",
            "domain_tags": ["topic-a"],
            "what_user_did": "Did some work.",
            "what_was_learned": "Learned a thing.",
            "decisions": [],
            "files_touched": [],
            "outcome": "completed",
            "salience": 5,
        }

    def is_available(self):
        return (True, "")

    def invoke(self, system, prompt, *, model=None, json_schema=None,
               max_budget_usd=0.10, timeout_s=60):
        self.calls += 1
        if self._sleep_s:
            time.sleep(self._sleep_s)
        from llm_providers.base import LLMResult
        return LLMResult(
            text=json.dumps(self._response),
            parsed_json=self._response,
            tokens_in=10, tokens_out=10,
            provider=self.name, model=model or self.default_model,
            cost_usd=None,
        )


def _claude_session(tmp_path: Path, sid: str) -> Path:
    """A minimal, single-pass-sized synthetic Claude session transcript."""
    path = tmp_path / f"{sid}.jsonl"
    events = [
        {"type": "user", "uuid": "u0", "promptId": "p0",
         "timestamp": "2026-05-01T12:00:00Z", "sessionId": sid,
         "cwd": "/tmp/work", "gitBranch": "main",
         "message": {"role": "user", "content": "User message about topic"}},
        {"type": "assistant", "uuid": "a0", "parentUuid": "u0",
         "timestamp": "2026-05-01T12:00:30Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-haiku-4-5",
                     "content": [{"type": "text", "text": "Assistant reply"}],
                     "usage": {"input_tokens": 5, "output_tokens": 10}}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _make_pending_sessions(projects_root: Path, n: int) -> None:
    d = projects_root / "p"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        _claude_session(d, f"sess-{i}")


# ---------------------------------------------------------------------------
# 1. adapter.backfill() — limit + max_seconds
# ---------------------------------------------------------------------------

class TestBackfillBudget:
    def test_no_limit_no_max_seconds_processes_everything(self, adapter_mod,
                                                           tmp_path):
        """Backward compatibility: existing callers that pass neither
        knob (backfill's historical contract) must see unbounded
        behavior — every pending session processed in one call."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        _make_pending_sessions(projects, 3)
        provider = _FakeProvider()

        stats = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                      codex_root=None, provider=provider)

        assert stats["digests_written"] == 3
        assert stats["processed"] == 3
        assert stats["pending"] == 0
        assert stats["budget_hit"] is False
        assert stats["elapsed_s"] >= 0

    def test_limit_caps_processed_count(self, adapter_mod, tmp_path):
        """`limit=N` processes at most N pending sessions; the rest are
        reported as pending (not silently dropped, not force-run)."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        _make_pending_sessions(projects, 3)
        provider = _FakeProvider()

        stats = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                      codex_root=None, provider=provider,
                                      limit=2)

        assert provider.calls == 2, "only 2 sessions should have hit the LLM"
        assert stats["processed"] == 2
        assert stats["digests_written"] == 2
        assert stats["pending"] == 1
        assert stats["budget_hit"] is True

    def test_already_digested_sessions_never_count_against_limit(
            self, adapter_mod, tmp_path):
        """A session whose SHA already matches the sidecar is a free
        skip — it must not consume the `limit` budget meant for actual
        LLM work."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        _make_pending_sessions(projects, 2)
        provider = _FakeProvider()
        # First pass digests both (no limit).
        s1 = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                   codex_root=None, provider=provider)
        assert s1["digests_written"] == 2

        # Add ONE new pending session; re-run with limit=1. The 2
        # already-digested sessions must be free skips, and the 1 new
        # one must be the one that consumes the limit.
        _claude_session(projects / "p", "sess-new")
        s2 = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                   codex_root=None, provider=provider,
                                   limit=1)
        assert s2["skipped_idempotent"] == 2
        assert s2["processed"] == 1
        assert s2["digests_written"] == 1
        assert s2["pending"] == 0
        assert s2["budget_hit"] is False

    def test_max_seconds_stops_cleanly_before_next_session(self, adapter_mod,
                                                            tmp_path):
        """A time budget that's exceeded partway through must stop
        starting NEW sessions rather than kill one in progress. Uses a
        real (tiny) sleep per LLM call and a real (tinier) budget so the
        second/third session's pre-flight check sees the budget as
        already spent."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        _make_pending_sessions(projects, 3)
        provider = _FakeProvider(sleep_s=0.15)

        stats = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                      codex_root=None, provider=provider,
                                      max_seconds=0.02)

        assert provider.calls == 1, (
            "budget was nearly zero — only the first session (already "
            "past the pre-flight check when the clock started) should "
            "have run an LLM call"
        )
        assert stats["processed"] == 1
        assert stats["pending"] == 2
        assert stats["budget_hit"] is True
        assert stats["elapsed_s"] >= 0.15

    def test_limit_and_max_seconds_together_pending_reflects_both(
            self, adapter_mod, tmp_path):
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        _make_pending_sessions(projects, 5)
        provider = _FakeProvider()

        stats = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                      codex_root=None, provider=provider,
                                      limit=2, max_seconds=1500)

        assert stats["processed"] == 2
        assert stats["pending"] == 3
        assert stats["budget_hit"] is True


# ---------------------------------------------------------------------------
# 2. digest_cli.py incremental — CLI surface
# ---------------------------------------------------------------------------

class TestIncrementalCLI:
    def _patch_roots(self, monkeypatch, cli_mod, *, brain, projects, codex):
        monkeypatch.setattr(cli_mod, "_brain_root", lambda: brain)
        monkeypatch.setattr(cli_mod, "_projects_root", lambda: projects)
        monkeypatch.setattr(cli_mod, "_codex_root", lambda: codex)

    def test_summary_line_shape_and_default_limit_is_three(
            self, cli_mod, tmp_path, monkeypatch, capsys):
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        codex = tmp_path / "no-codex"
        _make_pending_sessions(projects, 4)
        self._patch_roots(monkeypatch, cli_mod, brain=brain,
                          projects=projects, codex=codex)
        provider = _FakeProvider()
        monkeypatch.setattr(cli_mod, "resolve_provider",
                            lambda *a, **k: provider)

        code = cli_mod.main(["incremental"])
        out = capsys.readouterr().out

        assert code == 0
        m = re.search(
            r"digests: processed=(\d+) pending=(\d+) "
            r"elapsed_s=([\d.]+) budget_hit=(True|False)",
            out,
        )
        assert m, f"summary line not found in output: {out!r}"
        assert int(m.group(1)) == 3, "default --limit must be 3"
        assert int(m.group(2)) == 1
        assert m.group(4) == "True"

    def test_explicit_limit_and_max_seconds_respected(
            self, cli_mod, tmp_path, monkeypatch, capsys):
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        codex = tmp_path / "no-codex"
        _make_pending_sessions(projects, 5)
        self._patch_roots(monkeypatch, cli_mod, brain=brain,
                          projects=projects, codex=codex)
        provider = _FakeProvider()
        monkeypatch.setattr(cli_mod, "resolve_provider",
                            lambda *a, **k: provider)

        code = cli_mod.main(["incremental", "--limit", "2",
                             "--max-seconds", "1500"])
        out = capsys.readouterr().out

        assert code == 0
        assert provider.calls == 2
        m = re.search(r"digests: processed=(\d+) pending=(\d+)", out)
        assert m and int(m.group(1)) == 2 and int(m.group(2)) == 3

    def test_second_run_only_digests_new_pending_sessions(
            self, cli_mod, tmp_path, monkeypatch, capsys):
        """Two consecutive `incremental` runs with limit=2 against 3
        pending sessions: first run digests 2, second run digests the
        remaining 1 (already-digested ones are free skips, not counted
        against the second run's limit) and reports pending=0."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        codex = tmp_path / "no-codex"
        _make_pending_sessions(projects, 3)
        self._patch_roots(monkeypatch, cli_mod, brain=brain,
                          projects=projects, codex=codex)
        provider = _FakeProvider()
        monkeypatch.setattr(cli_mod, "resolve_provider",
                            lambda *a, **k: provider)

        cli_mod.main(["incremental", "--limit", "2"])
        capsys.readouterr()
        cli_mod.main(["incremental", "--limit", "2"])
        out2 = capsys.readouterr().out

        m = re.search(
            r"digests: processed=(\d+) pending=(\d+) .*budget_hit=(\w+)",
            out2,
        )
        assert m
        assert int(m.group(1)) == 1
        assert int(m.group(2)) == 0
        assert m.group(3) == "False"


# ---------------------------------------------------------------------------
# 3. sync_claude_extras.py — separate timeout, env wiring, exit codes,
#    digest_status.json
# ---------------------------------------------------------------------------

_FAKE_ADAPTER = '''#!/usr/bin/env python3
import sys
print("fake adapter ok", *sys.argv[1:])
sys.exit(0)
'''

_FAKE_DIGEST_CLI = '''#!/usr/bin/env python3
import argparse
import os
import sys
import time

p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd")
si = sub.add_parser("incremental")
si.add_argument("--limit", type=int, default=3)
si.add_argument("--max-seconds", type=float, default=1500)
si.add_argument("--provider", default=None)
args = p.parse_args()

if args.cmd != "incremental":
    sys.exit(2)

print(f"received: limit={args.limit} max_seconds={args.max_seconds}")
sleep_s = float(os.environ.get("FAKE_DIGEST_SLEEP_S", "0"))
if sleep_s:
    time.sleep(sleep_s)
processed = os.environ.get("FAKE_DIGEST_PROCESSED", "0")
pending = os.environ.get("FAKE_DIGEST_PENDING", "0")
elapsed = os.environ.get("FAKE_DIGEST_ELAPSED", "0.0")
budget_hit = os.environ.get("FAKE_DIGEST_BUDGET_HIT", "False")
print(f"digests: processed={processed} pending={pending} "
      f"elapsed_s={elapsed} budget_hit={budget_hit}")
sys.exit(int(os.environ.get("FAKE_DIGEST_EXIT", "0")))
'''


def _write_stub_tools(tools_dir: Path) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "claude_session_adapter.py").write_text(_FAKE_ADAPTER)
    (tools_dir / "claude_misc_adapter.py").write_text(_FAKE_ADAPTER)
    (tools_dir / "digest_cli.py").write_text(_FAKE_DIGEST_CLI)


def _reload_sync(monkeypatch, brain: Path, **extra_env: str):
    """(Re)import sync_claude_extras with BRAIN_ROOT + digest-budget env
    vars set BEFORE import, so its module-level constants (BRAIN_ROOT,
    TOOLS_DIR, DIGEST_TIMEOUT_S, DIGEST_LIMIT, DIGEST_MAX_SECONDS) are
    derived from them — the same env/arg-injection shape the rest of
    the suite uses to fake sub-steps without touching real ~/.agent."""
    monkeypatch.setenv("BRAIN_ROOT", str(brain))
    monkeypatch.setenv("PYTHON", sys.executable)
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)
    import sync_claude_extras
    importlib.reload(sync_claude_extras)
    return sync_claude_extras


@pytest.fixture
def brain(tmp_path):
    b = tmp_path / "brain"
    b.mkdir()
    (b / ".digests-enabled").touch()
    _write_stub_tools(b / "tools")
    return b


class TestSyncClaudeExtrasDigestBudget:
    def test_digest_step_gets_its_own_timeout_not_600s(self, monkeypatch,
                                                       brain):
        """A digest step that runs longer than the SHARED 600s adapter
        timeout but within its own BRAINSTACK_DIGEST_TIMEOUT_S must not
        be killed. Uses a short custom timeout (not 600s) so the test
        stays fast, but the KEY assertion is that it's a DIFFERENT,
        independently-configurable value from the adapter timeout."""
        monkeypatch.setenv("FAKE_DIGEST_SLEEP_S", "0.2")
        monkeypatch.setenv("FAKE_DIGEST_PROCESSED", "1")
        monkeypatch.setenv("FAKE_DIGEST_PENDING", "0")
        mod = _reload_sync(monkeypatch, brain,
                           BRAINSTACK_DIGEST_TIMEOUT_S="5")
        assert mod.DIGEST_TIMEOUT_S == 5.0
        assert mod.ADAPTER_TIMEOUT == 600.0

        rc = mod.main()

        assert rc == 0
        log = (brain / "claude-extras.log").read_text()
        assert "TIMEOUT" not in log

    def test_digest_step_timeout_kills_cleanly_and_fails_the_run(
            self, monkeypatch, brain):
        """A digest step that overruns ITS OWN (short) timeout must be
        killed and reported as a real failure — separate from, and
        without disturbing, the two adapters' 600s ceiling."""
        monkeypatch.setenv("FAKE_DIGEST_SLEEP_S", "2")
        mod = _reload_sync(monkeypatch, brain,
                           BRAINSTACK_DIGEST_TIMEOUT_S="0.2")

        rc = mod.main()

        assert rc == 1
        log = (brain / "claude-extras.log").read_text()
        assert "[digest_cli_incremental] TIMEOUT after" in log
        # the other two adapters ran fine and are unaffected
        assert "[claude_session_adapter] done (exit 0)" in log
        assert "[claude_misc_adapter] done (exit 0)" in log

    def test_env_vars_forwarded_as_limit_and_max_seconds_args(
            self, monkeypatch, brain):
        mod = _reload_sync(monkeypatch, brain,
                           BRAINSTACK_DIGEST_LIMIT="7",
                           BRAINSTACK_DIGEST_MAX_SECONDS="42.5")
        assert mod.DIGEST_LIMIT == 7
        assert mod.DIGEST_MAX_SECONDS == 42.5

        rc = mod.main()

        assert rc == 0
        log = (brain / "claude-extras.log").read_text()
        assert "received: limit=7 max_seconds=42.5" in log

    def test_default_env_vars_match_spec(self, monkeypatch, brain):
        mod = _reload_sync(monkeypatch, brain)
        assert mod.DIGEST_LIMIT == 3
        assert mod.DIGEST_MAX_SECONDS == 1500.0
        assert mod.DIGEST_TIMEOUT_S == 1800.0

    def test_summary_line_logged_and_status_file_written(self, monkeypatch,
                                                          brain):
        monkeypatch.setenv("FAKE_DIGEST_PROCESSED", "2")
        monkeypatch.setenv("FAKE_DIGEST_PENDING", "5")
        monkeypatch.setenv("FAKE_DIGEST_ELAPSED", "12.3")
        monkeypatch.setenv("FAKE_DIGEST_BUDGET_HIT", "True")
        mod = _reload_sync(monkeypatch, brain)

        rc = mod.main()

        # Exit 0 even though pending > 0 — a bounded, completed run is
        # NOT a failure.
        assert rc == 0
        log = (brain / "claude-extras.log").read_text()
        assert ("[digest_cli_incremental] digests: processed=2 pending=5 "
                "elapsed_s=12.3 budget_hit=True") in log

        status_path = brain / "runtime" / "digest_status.json"
        assert status_path.is_file()
        payload = json.loads(status_path.read_text())
        assert payload["processed"] == 2
        assert payload["pending"] == 5
        assert payload["elapsed_s"] == 12.3
        assert payload["budget_hit"] is True
        assert "ts" in payload

    def test_real_digest_failure_exits_1(self, monkeypatch, brain):
        """A genuine failure (e.g. provider unavailable, rc=2) must
        fail the overall run — exit-0-despite-pending only applies to
        a CLEANLY COMPLETED bounded run, not an error exit."""
        monkeypatch.setenv("FAKE_DIGEST_EXIT", "2")
        mod = _reload_sync(monkeypatch, brain)

        rc = mod.main()

        assert rc == 1

    def test_digest_layer_disabled_without_marker_file_skips_cleanly(
            self, monkeypatch, tmp_path):
        brain = tmp_path / "brain2"
        brain.mkdir()
        _write_stub_tools(brain / "tools")
        # deliberately no `.digests-enabled` marker
        mod = _reload_sync(monkeypatch, brain)

        rc = mod.main()

        assert rc == 0
        log = (brain / "claude-extras.log").read_text()
        assert "digest layer not enabled" in log
        assert not (brain / "runtime" / "digest_status.json").exists()
