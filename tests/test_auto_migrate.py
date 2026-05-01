"""Tests for auto-migrate setup wizard + LaunchAgent install + the
dispatcher's `auto-migrate-all` subcommand.

PR-D — turns the multi-tool migrate (PR-A/B/C) into a "set it once,
forget it" pipeline. ONE LaunchAgent reads `$BRAIN_ROOT/auto-migrate.json`
and runs each enabled tool's migrate sequentially every hour.

Tests mock `launchctl` via a `FakeLaunchctl` helper that records all
calls. Real launchd integration is opt-in via `BRAINSTACK_RUN_LAUNCHD=1`
(not used by these unit tests).
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

from auto_migrate_install import (  # noqa: E402
    LABEL,
    DEFAULT_INTERVAL,
    SCHEMA_VERSION,
    discover_enabled_candidates,
    generate_plist,
    install_plist,
    main as auto_install_main,
    read_config,
    remove_plist,
    write_config,
)
from migrate_dispatcher import (  # noqa: E402
    auto_migrate_all,
)


# --- Fake launchctl ---


class FakeLaunchctl:
    """Records every launchctl invocation. Drop-in for `launchctl_bin` arg.

    Tests assert against `.calls` (a list of argv tuples). Behaves like
    a real launchctl binary that always succeeds — override `.return_codes`
    if a specific call needs to fail.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.loaded_labels: set[str] = set()
        # Map "subcommand" → return code. Default 0 (success). Tests
        # override e.g. `{"bootout": 36}` to simulate "service not loaded".
        self.return_codes: dict[str, int] = {}

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(tuple(argv))
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "bootstrap" and len(argv) > 3:
            # 4th argv is plist path; extract label from filename
            self.loaded_labels.add(Path(argv[3]).stem)
        if sub == "bootout":
            # `bootout gui/UID label` — drop label
            target = argv[-1].split("/")[-1]
            self.loaded_labels.discard(target)
        if sub == "list":
            # Show what's loaded for inspection
            return subprocess.CompletedProcess(
                argv, 0, stdout="\n".join(self.loaded_labels), stderr=""
            )
        rc = self.return_codes.get(sub, 0)
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")


def _stage_brain(tmp_path: Path) -> Path:
    """Mirror existing test pattern from test_cursor_adapter.py."""
    brain = tmp_path / "brain"
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    import shutil
    for f in (
        REPO_ROOT / "agent" / "tools" / "migrate.py",
        REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py",
        REPO_ROOT / "agent" / "tools" / "cursor_adapter.py",
        REPO_ROOT / "agent" / "tools" / "codex_adapter.py",
        REPO_ROOT / "agent" / "tools" / "auto_migrate_install.py",
    ):
        shutil.copy(f, brain / "tools" / f.name)
    shutil.copy(REPO_ROOT / "agent" / "memory" / "_atomic.py", brain / "memory")
    return brain


# --- 1: wizard lists tools & skips Claude ---


def test_wizard_lists_tools_and_skips_claude(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    # Claude memory (gets skipped)
    (fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory" / "feedback_a.md").write_text("---\ntype: feedback\n---\nbody\n")
    # Cursor + Codex (the wizard prompts about these)
    (fake_home / ".cursor" / "plans").mkdir(parents=True)
    (fake_home / ".cursor" / "plans" / "x.plan.md").write_text("# x\n")
    sess = fake_home / ".codex" / "sessions" / "2026" / "04" / "29"
    sess.mkdir(parents=True)
    (sess / "rollout-y.jsonl").write_text('{"type":"x","timestamp":"2026-01-01","payload":{}}\n')
    (fake_home / ".codex" / "config.toml").write_text("# fake\n")

    cands = discover_enabled_candidates(env={"HOME": str(fake_home)})
    formats = {c.format for c in cands}
    # Claude excluded; Cursor + Codex in the prompt set
    assert "cursor-plans" in formats
    assert "codex-cli" in formats
    assert not any(c.format.startswith("claude-code") for c in cands)
    assert not any(c.format == "already-symlinked" for c in cands)


# --- 2: wizard writes config for enabled tools ---


def test_wizard_writes_config_for_enabled_tools(tmp_path):
    brain = tmp_path / "brain"
    brain.mkdir()
    config = {
        "schema_version": SCHEMA_VERSION,
        "interval_seconds": 3600,
        "tools": [
            {"format": "cursor-plans", "source": str(tmp_path / "cursor" / "plans")},
            {"format": "codex-cli", "source": str(tmp_path / "codex")},
        ],
    }
    write_config(brain, config)
    loaded = read_config(brain)
    assert loaded == config
    # Path on disk
    assert (brain / "auto-migrate.json").is_file()


# --- 3: plist generated with plistlib ---


def test_plist_generated_with_plistlib(tmp_path):
    brain = tmp_path / "brain"
    plist_bytes = generate_plist(
        brain_root=brain,
        python_abs=Path("/opt/homebrew/bin/python3.13"),
        interval_seconds=3600,
    )
    parsed = plistlib.loads(plist_bytes)
    assert parsed["Label"] == LABEL
    assert parsed["RunAtLoad"] is True
    assert parsed["StartInterval"] == 3600
    # ProgramArguments hits the dispatcher's auto-migrate-all subcommand
    args = parsed["ProgramArguments"]
    assert args[0] == "/opt/homebrew/bin/python3.13"
    assert args[1].endswith("/migrate_dispatcher.py")
    assert "auto-migrate-all" in args
    # Log paths exist
    assert "auto-migrate.log" in parsed["StandardOutPath"]


# --- 4: brain root with spaces ---


def test_brain_root_with_spaces(tmp_path):
    brain = tmp_path / "has spaces" / "brain"
    brain.mkdir(parents=True)
    plist_bytes = generate_plist(
        brain_root=brain,
        python_abs=Path("/opt/homebrew/bin/python3.13"),
        interval_seconds=3600,
    )
    parsed = plistlib.loads(plist_bytes)
    # Path with spaces must round-trip exactly — plistlib handles XML
    # escaping for us; sed would have mangled this.
    assert str(brain) in parsed["StandardOutPath"]
    args_joined = " ".join(parsed["ProgramArguments"])
    assert "has spaces" in args_joined


# --- 5: brain root NOT hardcoded ---


def test_resolved_brain_root_not_hardcoded(tmp_path):
    """If user passes a non-default BRAIN_ROOT, plist must reflect it."""
    brain = tmp_path / "custom-brain-loc"
    brain.mkdir()
    plist_bytes = generate_plist(
        brain_root=brain,
        python_abs=Path("/opt/homebrew/bin/python3.13"),
        interval_seconds=3600,
    )
    parsed = plistlib.loads(plist_bytes)
    args = parsed["ProgramArguments"]
    # `~/.agent` must NOT appear anywhere — only the resolved custom path
    assert not any("/.agent/" in a for a in args), \
        f"plist hardcoded ~/.agent instead of using brain_root={brain}"
    assert any(str(brain) in a for a in args), \
        f"plist doesn't reference custom brain_root: args={args}"


# --- 6: idempotent rerun byte-identical ---


def test_idempotent_rerun_byte_identical(tmp_path):
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    config = {
        "schema_version": SCHEMA_VERSION,
        "interval_seconds": 3600,
        "tools": [{"format": "cursor-plans", "source": str(tmp_path / "cursor")}],
    }
    write_config(brain, config)

    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir,
        launchctl_bin=fake,
        uid=502,
    )
    first_bytes = (plist_dir / f"{LABEL}.plist").read_bytes()

    fake2 = FakeLaunchctl()
    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir,
        launchctl_bin=fake2,
        uid=502,
    )
    second_bytes = (plist_dir / f"{LABEL}.plist").read_bytes()

    assert first_bytes == second_bytes
    # Each install: 1 bootout (tolerated) + 1 bootstrap + 1 kickstart
    bootouts = [c for c in fake2.calls if c[1] == "bootout"]
    bootstraps = [c for c in fake2.calls if c[1] == "bootstrap"]
    assert len(bootstraps) == 1
    assert len(bootouts) == 1


# --- 7: hand-edited plist backed up ---


def test_handedited_plist_backed_up(tmp_path):
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    write_config(brain, {
        "schema_version": SCHEMA_VERSION, "interval_seconds": 3600,
        "tools": [{"format": "cursor-plans", "source": str(tmp_path / "cursor")}],
    })

    # First install
    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir, launchctl_bin=fake, uid=502,
    )
    plist_path = plist_dir / f"{LABEL}.plist"

    # User hand-edits the plist (simulate)
    contents = plistlib.loads(plist_path.read_bytes())
    contents["StartInterval"] = 60  # user wanted faster polling
    plist_path.write_bytes(plistlib.dumps(contents))

    # Re-install with the canonical interval (3600)
    fake2 = FakeLaunchctl()
    result = install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable), interval_seconds=3600),
        plist_dir=plist_dir, launchctl_bin=fake2, uid=502,
    )
    # Backup of the hand-edited version exists
    backups = list(plist_dir.glob(f"{LABEL}.plist.bak.*"))
    assert backups, f"hand-edited plist not backed up; dir: {list(plist_dir.iterdir())}"
    backup_contents = plistlib.loads(backups[0].read_bytes())
    assert backup_contents["StartInterval"] == 60  # captured the user's tweak
    # The result reports the backup
    assert result.get("backed_up_path"), \
        f"result didn't surface the backup path: {result}"


# --- 8: pre-existing loaded label ---


def test_preexisting_loaded_label(tmp_path):
    """If launchctl reports the label already loaded (from another path),
    we still install cleanly via tolerated bootout + bootstrap."""
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    fake.loaded_labels.add(LABEL)  # pre-existing
    fake.return_codes["bootout"] = 0  # bootout succeeds
    write_config(brain, {
        "schema_version": SCHEMA_VERSION, "interval_seconds": 3600, "tools": [],
    })

    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir, launchctl_bin=fake, uid=502,
    )
    # Installed cleanly
    assert (plist_dir / f"{LABEL}.plist").is_file()
    # bootout was called (tolerated even on pre-existing label)
    bootouts = [c for c in fake.calls if c[1] == "bootout"]
    assert bootouts


def test_preexisting_loaded_label_bootout_returns_36_tolerated(tmp_path):
    """`launchctl bootout` returns nonzero (36) when service isn't loaded.
    install must tolerate that and proceed to bootstrap."""
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    fake.return_codes["bootout"] = 36  # "not loaded" / not found
    write_config(brain, {"schema_version": SCHEMA_VERSION, "interval_seconds": 3600, "tools": []})

    # Should NOT raise — bootout error is expected on first install
    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir, launchctl_bin=fake, uid=502,
    )
    # bootstrap still ran
    bootstraps = [c for c in fake.calls if c[1] == "bootstrap"]
    assert bootstraps


# --- 9: missing LaunchAgents dir ---


def test_missing_launchagents_dir(tmp_path):
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "Library" / "LaunchAgents"  # doesn't exist yet
    fake = FakeLaunchctl()
    write_config(brain, {"schema_version": SCHEMA_VERSION, "interval_seconds": 3600, "tools": []})

    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir, launchctl_bin=fake, uid=502,
    )
    # Helper created the dir
    assert plist_dir.is_dir()
    assert (plist_dir / f"{LABEL}.plist").is_file()


# --- 10: --enable flag (dry-run) ---


def test_noninteractive_enable_flag_dry_run(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    (fake_home / ".cursor" / "plans").mkdir(parents=True)
    (fake_home / ".cursor" / "plans" / "x.plan.md").write_text("# x\n")
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("BRAIN_ROOT", str(brain))

    fake = FakeLaunchctl()
    rc = auto_install_main([
        "setup",
        "--enable", "cursor-plans",
        "--dry-run",
        "--brain-root", str(brain),
        "--plist-dir", str(plist_dir),
    ], launchctl_bin=fake)
    assert rc == 0
    # Dry-run wrote NOTHING
    assert not (plist_dir / f"{LABEL}.plist").exists()
    assert not (brain / "auto-migrate.json").exists()
    # No launchctl calls
    assert fake.calls == []
    # But it printed the plan
    out = capsys.readouterr().out
    assert "cursor-plans" in out
    assert "would" in out.lower() or "dry" in out.lower()


# --- 11: --enable invalid format ---


def test_noninteractive_invalid_format_errors(tmp_path):
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    rc = auto_install_main([
        "setup",
        "--enable", "bogus-tool-format",
        "--brain-root", str(brain),
        "--plist-dir", str(plist_dir),
    ], launchctl_bin=fake)
    assert rc != 0
    # No state changes
    assert not (plist_dir / f"{LABEL}.plist").exists()


# --- 12: --disable subset ---


def test_noninteractive_disable_subset(tmp_path):
    brain = _stage_brain(tmp_path)
    # Pre-populate config with both
    write_config(brain, {
        "schema_version": SCHEMA_VERSION, "interval_seconds": 3600,
        "tools": [
            {"format": "cursor-plans", "source": str(tmp_path / "cursor")},
            {"format": "codex-cli", "source": str(tmp_path / "codex")},
        ],
    })
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()

    rc = auto_install_main([
        "setup",
        "--disable", "cursor-plans",
        "--brain-root", str(brain),
        "--plist-dir", str(plist_dir),
    ], launchctl_bin=fake)
    assert rc == 0
    config = read_config(brain)
    formats = [t["format"] for t in config["tools"]]
    assert formats == ["codex-cli"]


# --- 13: --print-plist ---


def test_print_plist_to_stdout(tmp_path, capsys):
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    rc = auto_install_main([
        "setup",
        "--print-plist",
        "--brain-root", str(brain),
        "--plist-dir", str(plist_dir),
    ], launchctl_bin=fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "<?xml" in out
    assert LABEL in out
    # Nothing written to disk
    assert not (plist_dir / f"{LABEL}.plist").exists()
    assert fake.calls == []


# --- 14: invalid discover JSON ---


def test_invalid_discover_json_recoverable(tmp_path, monkeypatch, capsys):
    """If discover_candidates raises, --setup-auto-migrate must surface
    a helpful error, not a Python traceback."""
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()

    def boom(env=None):
        raise RuntimeError("fake discover failure")

    import auto_migrate_install
    monkeypatch.setattr(auto_migrate_install, "discover_enabled_candidates", boom)

    rc = auto_install_main([
        "setup",
        "--all",  # interactive-equivalent: enable everything discoverable
        "--brain-root", str(brain),
        "--plist-dir", str(plist_dir),
    ], launchctl_bin=fake)
    err = capsys.readouterr().err
    assert rc != 0
    assert "discover" in err.lower()


# --- 15: dispatcher per-tool failure logged not aborted ---


def test_dispatcher_failure_per_tool_logged_not_aborted(tmp_path, monkeypatch):
    brain = _stage_brain(tmp_path)
    # Two tools — first will fail, second should still run
    write_config(brain, {
        "schema_version": SCHEMA_VERSION, "interval_seconds": 3600,
        "tools": [
            {"format": "cursor-plans", "source": str(tmp_path / "missing-cursor-dir")},
            {"format": "codex-cli", "source": str(tmp_path / "codex")},
        ],
    })
    # Make codex source real so it succeeds
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text("# fake\n")
    sess = codex / "sessions" / "2026" / "04" / "29"
    sess.mkdir(parents=True)
    (sess / "rollout-x.jsonl").write_text(
        '{"type":"event_msg","timestamp":"2026-01-01","payload":{"type":"x"}}\n'
    )

    result = auto_migrate_all(brain_root=brain)
    # Cursor failed; codex still ran
    assert "errors" in result
    assert len(result["errors"]) == 1
    assert "cursor-plans" in result["errors"][0]["format"]
    # codex episode should be in the brain
    epi = brain / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    assert epi.exists()


# --- 16: global lock prevents concurrent runs ---


def test_global_lock_prevents_concurrent_runs(tmp_path):
    brain = _stage_brain(tmp_path)
    write_config(brain, {
        "schema_version": SCHEMA_VERSION, "interval_seconds": 3600, "tools": [],
    })
    lock_path = brain / ".auto-migrate.lock"

    # Simulate the lock being held by another process: open + fcntl.flock
    import fcntl
    with open(lock_path, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Now try to auto-migrate-all in this process — it should detect
        # the lock and either skip or wait briefly + warn.
        result = auto_migrate_all(brain_root=brain, lock_timeout=0.1)
        assert result.get("skipped") is True or "lock" in str(result).lower()


# --- 17: remove auto-migrate clean teardown ---


def test_remove_auto_migrate_clean_teardown(tmp_path):
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()
    write_config(brain, {
        "schema_version": SCHEMA_VERSION, "interval_seconds": 3600, "tools": [],
    })

    # Install first
    install_plist(
        plist_bytes=generate_plist(brain, Path(sys.executable)),
        plist_dir=plist_dir, launchctl_bin=fake, uid=502,
    )
    assert (plist_dir / f"{LABEL}.plist").is_file()

    # Remove
    remove_plist(plist_dir=plist_dir, launchctl_bin=fake, uid=502)
    # Plist gone
    assert not (plist_dir / f"{LABEL}.plist").exists()
    # bootout was called for the removal
    bootouts = [c for c in fake.calls if c[1] == "bootout"]
    assert len(bootouts) >= 2  # at least one for install (tolerated) + one for remove
    # Config file kept (per UX decision in the plan)
    assert (brain / "auto-migrate.json").is_file()


# --- 18: refuses under sudo ---


def test_install_dry_run_writes_no_files(tmp_path):
    """Codex P3: install_plist(dry_run=True) was creating plist_dir +
    writing the .bak file before the dry-run check. Function-level
    contract violated. Pin the fix."""
    plist_dir = tmp_path / "non-existent-yet" / "LaunchAgents"
    fake = FakeLaunchctl()
    plist_bytes = generate_plist(tmp_path, Path(sys.executable))
    # Existing plist with different content — would normally trigger backup
    plist_dir.mkdir(parents=True)
    (plist_dir / f"{LABEL}.plist").write_bytes(b"<plist>old</plist>\n")

    # Snapshot dir state
    before = sorted(plist_dir.iterdir())

    result = install_plist(
        plist_bytes=plist_bytes, plist_dir=plist_dir,
        launchctl_bin=fake, uid=502, dry_run=True,
    )
    after = sorted(plist_dir.iterdir())
    # No new files (no .bak created)
    assert before == after, f"dry_run wrote files: {set(after) - set(before)}"
    # No launchctl calls
    assert fake.calls == []
    # But the result advisory-reports what backup WOULD be created
    assert result.dry_run is True
    assert result.backed_up_path is not None  # advisory


def test_brain_root_resolved_in_plist(tmp_path):
    """Codex P2: a relative brain_root must be resolved to absolute
    before generating the plist. launchd doesn't run from the installer's
    cwd; a relative path would dangle."""
    # Use a relative path explicitly (not Path.resolve()'d)
    rel = Path("relative-brain-dir")
    abs_brain = tmp_path / rel
    abs_brain.mkdir(parents=True)
    # Even when caller passes the relative form, plist must contain absolute
    plist_bytes = generate_plist(rel, Path(sys.executable))
    parsed = plistlib.loads(plist_bytes)
    args = parsed["ProgramArguments"]
    # Every path argument must be absolute (start with /)
    for a in args:
        if "/" in a:
            assert a.startswith("/"), f"non-absolute path in plist: {a!r}"


def test_dispatch_takes_auto_migrate_lock(tmp_path):
    """Manual dispatch() must hold the auto-migrate lock so a concurrent
    LaunchAgent firing doesn't race on `_imported.jsonl`. When the lock
    can't be acquired in 2s, dispatch refuses to run rather than proceeding
    unsafely — the earlier "warn and proceed" path produced mid-line
    sidecar offsets and "skipped malformed line" warnings on the next run.
    """
    import fcntl
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text("---\ntype: feedback\n---\nbody\n")
    dst = tmp_path / "brain"

    # Hold the lock externally before calling dispatch
    dst.mkdir()
    lock_path = dst / ".auto-migrate.lock"
    held = open(lock_path, "w")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Import dispatch in this scope
    from migrate_dispatcher import dispatch
    # Dispatch must NOT block forever; it tries the lock with a 2s timeout
    # and then bails cleanly.
    import time as _t
    start = _t.monotonic()
    result = dispatch(src=src, dst=dst, dry_run=False)
    elapsed = _t.monotonic() - start
    held.close()  # release for cleanup

    # Took at most ~2.5s (2s timeout + small slack)
    assert elapsed < 5.0, f"dispatch blocked too long under lock contention: {elapsed:.2f}s"
    # Refused to migrate under contention
    assert result.files_written == 0, \
        f"expected no files written under contention, got {result.files_written}"
    assert any("lock" in w.lower() for w in result.warnings), \
        f"contention warning missing: {result.warnings}"
    # The brain target must NOT have been written (no sidecar drift)
    assert not (dst / "memory" / "_imported.jsonl").exists(), \
        "sidecar should not be written when lock acquisition failed"


def test_install_sh_forwards_dry_run_flag_to_setup(tmp_path):
    """Codex P2 #1: `./install.sh --dry-run --setup-auto-migrate --all`
    previously performed a real install because DRY_RUN was never
    forwarded. Pin the fix by invoking install.sh that way."""
    import shutil
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / ".cursor" / "plans").mkdir(parents=True)
    (fake_home / ".cursor" / "plans" / "x.plan.md").write_text("# x\n")

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    env["HOME"] = str(fake_home)
    if "PYTHON_BIN" not in env:
        for c in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(c):
                env["PYTHON_BIN"] = c
                break

    # Note the order: --dry-run BEFORE --setup-auto-migrate.
    result = subprocess.run(
        ["bash", str(install_script), "--dry-run", "--setup-auto-migrate",
         "--all", "--plist-dir", str(plist_dir)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, \
        f"--dry-run --setup-auto-migrate failed:\n{result.stderr}\n{result.stdout}"
    # No plist installed
    assert not (plist_dir / f"{LABEL}.plist").exists(), \
        "dry-run wrote a plist; --dry-run was not forwarded to the helper"
    # No config written
    assert not (brain / "auto-migrate.json").exists()


def test_refuses_under_sudo(tmp_path, monkeypatch, capsys):
    """Helper must refuse to run as root — LaunchAgents are user-scoped."""
    brain = _stage_brain(tmp_path)
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    fake = FakeLaunchctl()

    # Simulate running as root via a `sudo_simulated` flag rather than
    # actually setuid'ing — the helper checks os.geteuid() (or accepts
    # an env override for testability).
    monkeypatch.setenv("AUTO_MIGRATE_SIMULATED_EUID", "0")

    rc = auto_install_main([
        "setup",
        "--all",
        "--brain-root", str(brain),
        "--plist-dir", str(plist_dir),
    ], launchctl_bin=fake)
    assert rc != 0
    err = capsys.readouterr().err
    assert "root" in err.lower() or "sudo" in err.lower()
    # No state change
    assert not (plist_dir / f"{LABEL}.plist").exists()
