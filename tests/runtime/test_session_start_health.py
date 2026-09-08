"""SessionStart surfaces the cached health report in the Claude Code banner.

The hourly sync LaunchAgent writes `<brain>/runtime/health.json`; the
SessionStart hook reads it from `recall.config.brain_root() / "runtime" /
"health.json"` and prints one line per FAIL. The brain root — NOT
`log_dir.parent` — is what locates it: `log_dir` is user-configurable and
the demo sets it elsewhere, which used to disable the banner outright.
This is the only place a health regression reaches the user without them
running a command, so the rules are strict: never raise, never block,
always return 0, and stay silent when there is nothing wrong.

Kept separate from tests/runtime/test_adapter_hooks.py so the health work
never edits that file.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest

from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.adapters.claude_code.hooks import handle_hook
from runtime.core.events import load_events


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    """A HOME and BRAIN_ROOT with no runtime config, so the live auto-recall
    probe resolves to SKIP unless a test deliberately opts in."""
    home = tmp_path / "home"
    brain = home / ".agent"
    brain.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRAIN_ROOT", str(brain))
    monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)
    return home


@pytest.fixture
def brain(isolated_home: Path) -> Path:
    """The `$BRAIN_ROOT` every test writes health.json under."""
    return isolated_home / ".agent"


@pytest.fixture
def tmp_config(tmp_path: Path) -> RuntimeConfig:
    """A DELIBERATELY custom `log_dir`, outside the brain root. The banner
    has to find health.json anyway; resolving it from `log_dir.parent`
    silently disabled the banner for anyone who moved their logs."""
    return RuntimeConfig(log_dir=tmp_path / "custom-logs")


@pytest.fixture
def stdin_with(monkeypatch):
    def _set(payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(sys, "stdin", StringIO(text))
    return _set


@pytest.fixture
def session_cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    return cwd


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_health(
    brain: Path,
    checks: list[dict],
    *,
    hours_old: float = 0.5,
    raw: str | None = None,
) -> Path:
    """Drop a health.json where the hook looks for it: under the brain
    root, exactly where `sync.sh`'s `_write_health` puts it."""
    path = brain / "runtime" / "health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    generated = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    counts: dict[str, int] = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    status = "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS")
    path.write_text(json.dumps({
        "schema_version": 1,
        "generated_at": _iso(generated),
        "brain_root": str(brain),
        "cwd": str(brain),
        "status": status,
        "counts": counts,
        "checks": checks,
    }), encoding="utf-8")
    return path


def _check(id_: str, status: str, evidence: str, fix: str = "") -> dict:
    return {"id": id_, "status": status, "evidence": evidence, "fix": fix}


# ---------- printing ----------------------------------------------------


def test_session_start_prints_one_line_per_fail(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    long_evidence = "x" * 400
    _write_health(brain, [
        _check("imports_freshness", "FAIL", "never mirrored",
               "./install.sh --setup-claude-extras"),
        _check("brain_push", "FAIL", long_evidence, "sync.sh"),
        _check("log_sizes", "WARN", "events.log.jsonl 66.4 MB exceed 20 MB"),
        _check("drift", "PASS", "in sync"),
    ])
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    fail_lines = [ln for ln in out.splitlines()
                  if ln.startswith("brainstack health FAIL:")]
    assert len(fail_lines) == 2
    assert ("brainstack health FAIL: imports_freshness — never mirrored"
            in fail_lines)
    # Evidence is truncated so one broken check cannot flood the banner.
    long_line = [ln for ln in fail_lines if "x" * 50 in ln][0]
    assert len(long_line) < 300
    # WARN and PASS checks stay out of the banner entirely.
    assert "log_sizes" not in out
    assert "drift" not in out
    assert out.rstrip().endswith(
        "brainstack health: run 'recall health' for details and fixes"
    )


def test_session_start_silent_on_all_pass(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    _write_health(brain, [
        _check("drift", "PASS", "in sync"),
        _check("daemon", "SKIP", "not configured"),
    ])
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_session_start_silent_when_report_missing(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """Fresh install, before the first sync tick. Nagging about a file the
    user has never heard of is worse than saying nothing."""
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)

    assert rc == 0
    assert capsys.readouterr().out == ""
    assert not (brain / "runtime" / "health.json").exists()


def test_session_start_missing_report_with_a_stale_sync_log_is_flagged(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """No health.json is silence ONLY on a brain that has never synced. A
    sync.log that stopped growing more than 26 h ago while no report exists
    means the hourly agent stopped before it ever wrote one — the 2026-09-08
    live brain, where a sandbox uninstall had unloaded the LaunchAgents and
    nothing said so for three days."""
    import os, time
    log = brain / "sync.log"
    log.write_text("2026-09-05T00:33:30Z sync: commit succeeded but push failed\n")
    old = time.time() - 40 * 3600
    os.utime(log, (old, old))
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    assert "brainstack health:" in out
    assert "sync.log" in out and "26h" in out
    assert "LaunchAgent" in out
    assert "recall health" in out


def test_session_start_missing_report_with_a_fresh_sync_log_stays_silent(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """The first tick after install: sync.log is minutes old and the report
    is simply not written yet. Nothing to say."""
    (brain / "sync.log").write_text("2026-09-08T13:39:06Z sync: no changes\n")
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_session_start_flags_stale_report(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """A report older than 26 h means the hourly agent stopped running. The
    checks inside it are no longer evidence of anything."""
    _write_health(
        brain,
        [_check("drift", "PASS", "in sync")],
        hours_old=40,
    )
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    assert "brainstack health:" in out
    assert "older than 26h" in out
    assert "recall health" in out
    assert "brainstack health FAIL:" not in out


def test_session_start_never_raises_on_corrupt_json(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    _write_health(brain, [], raw='{"schema_version": 1, "checks": [')
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)

    assert rc == 0
    assert capsys.readouterr().out == ""
    # The event still lands: a broken banner must not cost us telemetry.
    assert len(load_events(tmp_config.event_log_path)) == 1


def test_session_start_live_cwd_auto_recall_fail_line(
    brain: Path, tmp_config, stdin_with, session_cwd, isolated_home, capsys
):
    """The cached report was written with the brain as cwd, so it can never
    see that THIS session's directory shadows the global config and silently
    disables auto-recall. The hook re-runs that one check live."""
    global_cfg = isolated_home / ".agent" / "runtime" / "pyproject.toml"
    global_cfg.parent.mkdir(parents=True, exist_ok=True)
    global_cfg.write_text(
        "[tool.recall.runtime]\nenable_auto_recall = true\n", encoding="utf-8"
    )
    (session_cwd / "pyproject.toml").write_text(
        "[tool.recall.runtime]\nenable_auto_recall = false\n", encoding="utf-8"
    )
    _write_health(brain, [_check("drift", "PASS", "in sync")])
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    assert "brainstack health FAIL: auto_recall_config" in out
    assert str(session_cwd) in out
    assert "recall health" in out


def test_session_start_returns_zero_always(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """Whatever the report says, the hook is telemetry: a non-zero exit would
    surface as a hook error in the user's session."""
    _write_health(brain, [
        _check("brain_push", "FAIL", "67 commits ahead of origin/main", "sync.sh"),
        _check("imports_freshness", "FAIL", "never mirrored"),
    ])
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)

    assert rc == 0
    capsys.readouterr()
    events = load_events(tmp_config.event_log_path)
    assert [e.event for e in events] == ["SessionStart"]


# ---------- where health.json is looked up ------------------------------


def test_report_is_read_from_the_brain_root_not_log_dir_parent(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """`log_dir` is user-configurable (the demo sets its own). Deriving the
    report path from `log_dir.parent` therefore pointed at a directory
    `sync.sh` never writes, and the banner went silent for exactly the
    users who had customised anything. A decoy at the old location must
    not win."""
    _write_health(brain, [
        _check("brain_push", "FAIL", "5 commits ahead of origin/main", "sync.sh"),
    ])
    decoy = tmp_config.log_dir.parent / "health.json"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text(json.dumps({
        "schema_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "brain_root": str(brain), "cwd": str(brain), "status": "FAIL",
        "counts": {"PASS": 0, "WARN": 0, "FAIL": 1, "SKIP": 0},
        "checks": [_check("decoy_check", "FAIL", "read from log_dir.parent")],
    }), encoding="utf-8")
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    assert "brainstack health FAIL: brain_push" in out, (
        f"the brain-root report was not read; banner said:\n{out}"
    )
    assert "decoy_check" not in out


# ---------- FAIL lines stay one line each -------------------------------


def test_fail_evidence_is_flattened_to_one_line(
    brain: Path, tmp_config, stdin_with, session_cwd, capsys
):
    """Evidence carries verbatim git output (`remote: error: ...`) and
    `check_freshness` summaries, i.e. untrusted multi-line text with ANSI
    colour in it. One FAIL is one banner line, always: a newline here
    would let a single check forge extra `brainstack health FAIL:` lines
    or scroll the real ones away."""
    evidence = (
        "remote: error: File big.jsonl is 107 MB\n"
        "\x1b[31mfatal:\x1b[0m failed to push some refs\n"
        "brainstack health FAIL: forged — not a real check"
    )
    _write_health(brain, [_check("brain_push", "FAIL", evidence, "sync.sh")])
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    fail_lines = [ln for ln in out.splitlines()
                  if ln.startswith("brainstack health FAIL:")]
    assert len(fail_lines) == 1, f"one FAIL produced {len(fail_lines)} lines:\n{out}"
    assert "remote: error: File big.jsonl is 107 MB" in fail_lines[0]
    assert "failed to push some refs" in fail_lines[0]
    assert "\x1b" not in out and "[31m" not in out


# ---------- the banner reads each source once ---------------------------


def test_live_check_reuses_the_hooks_config_when_cwd_matches(
    brain: Path, stdin_with, session_cwd, isolated_home, monkeypatch, capsys
):
    """SessionStart already loads `RuntimeConfig` for the process cwd, and
    `RuntimeConfig.load()` parses up to three TOML files. Re-running the
    live auto-recall check against that same directory used to load it all
    over again — two full parses on the session-open path for one answer.
    The banner must be identical either way."""
    global_cfg = isolated_home / ".agent" / "runtime" / "pyproject.toml"
    global_cfg.parent.mkdir(parents=True, exist_ok=True)
    global_cfg.write_text(
        "[tool.recall.runtime]\nenable_auto_recall = true\n", encoding="utf-8"
    )
    (session_cwd / "pyproject.toml").write_text(
        "[tool.recall.runtime]\nenable_auto_recall = false\n", encoding="utf-8"
    )
    _write_health(brain, [_check("drift", "PASS", "in sync")])
    monkeypatch.chdir(session_cwd)
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    loads: list[dict] = []
    real_load = RuntimeConfig.load

    def counting_load(**kwargs):
        loads.append(kwargs)
        return real_load(**kwargs)

    monkeypatch.setattr(RuntimeConfig, "load", staticmethod(counting_load))

    rc = handle_hook("SessionStart")
    out = capsys.readouterr().out

    assert rc == 0
    assert "brainstack health FAIL: auto_recall_config" in out
    assert str(session_cwd) in out
    assert len(loads) == 1, f"config loaded {len(loads)}x for one cwd: {loads}"


def test_live_check_still_loads_for_a_different_cwd(
    brain: Path, tmp_config, stdin_with, session_cwd, isolated_home,
    monkeypatch, capsys, tmp_path,
):
    """Reuse is only valid when the session's cwd IS the process cwd. A
    session opened elsewhere must still get the chdir-guarded load, or the
    check reports on the wrong directory's config."""
    global_cfg = isolated_home / ".agent" / "runtime" / "pyproject.toml"
    global_cfg.parent.mkdir(parents=True, exist_ok=True)
    global_cfg.write_text(
        "[tool.recall.runtime]\nenable_auto_recall = true\n", encoding="utf-8"
    )
    (session_cwd / "pyproject.toml").write_text(
        "[tool.recall.runtime]\nenable_auto_recall = false\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _write_health(brain, [_check("drift", "PASS", "in sync")])
    stdin_with({"session_id": "s-1", "cwd": str(session_cwd)})

    loads: list[dict] = []
    real_load = RuntimeConfig.load

    def counting_load(**kwargs):
        loads.append(kwargs)
        return real_load(**kwargs)

    monkeypatch.setattr(RuntimeConfig, "load", staticmethod(counting_load))

    # `config=` means the hook itself does not load, so every load counted
    # here belongs to the live check.
    rc = handle_hook("SessionStart", config=tmp_config)
    out = capsys.readouterr().out

    assert rc == 0
    assert "brainstack health FAIL: auto_recall_config" in out
    assert str(session_cwd) in out
    assert len(loads) == 1, (
        "the live check reused a config resolved from the wrong directory")
