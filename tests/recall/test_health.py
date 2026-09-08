"""Unit tests for `recall/health.py` — the health-check catalogue (S5 R1).

Every check is a pure function of an injected `HealthEnv`, so these tests
run with zero side effects on the developer's machine: no real git, no
`launchctl`, no network, no sockets. The only real I/O is inside `tmp_path`.

The fake environment
--------------------
`make_env` builds a `recall.health.HealthEnv` whose `run` is a lookup into
a caller-supplied `run_map` keyed by the exact argv tuple. An unstubbed
command returns `(1, "", "not stubbed")` so a check that shells out to
something the test did not anticipate degrades instead of hanging.

The argv tuples the checks are expected to issue are built by the `_git_*`
helpers below. They are part of the contract this file pins: if the
implementation changes the shape of a git invocation, the corresponding
helper here must change with it.

Sizes are written as sparse files (`seek` + one byte) so a 90 MB fixture
costs no disk and no time. Mtimes are set with `os.utime` relative to the
frozen `NOW` that the env carries, so nothing depends on wall-clock time.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import recall
from recall import health

REPO_ROOT = Path(__file__).resolve().parents[2]

MB = 1024 * 1024

# Frozen "current time" for every test in this file. Matches the dates used
# in the S5 plan's worked examples so evidence strings read the same way.
NOW = datetime(2026, 9, 4, 13, 0, 0, tzinfo=timezone.utc)


# ---------- helpers ----------------------------------------------------


def _hours_ago(hours: float) -> float:
    """Epoch seconds for a moment `hours` before the frozen NOW."""
    return (NOW - timedelta(hours=hours)).timestamp()


def _write(path: Path, text: str = "x", *, hours_ago: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if hours_ago is not None:
        ts = _hours_ago(hours_ago)
        os.utime(path, (ts, ts))
    return path


def _utime(path: Path, ts: float) -> Path:
    """Pin an mtime to an exact epoch second.

    Every timestamp these tests use is a whole number of seconds, which is
    exactly representable as a float and as integer nanoseconds. That matters
    for the threshold tests: a lag of "exactly 2 h" has to actually be
    7200.000000 s, not 7200.0000001, or the boundary assertion becomes a coin
    flip on the filesystem's timestamp rounding.
    """
    os.utime(path, (ts, ts))
    return path


def _sparse(path: Path, size_bytes: int) -> Path:
    """A file that reports `size_bytes` from stat() but occupies no blocks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.seek(size_bytes - 1)
        f.write(b"\0")
    return path


def _git(brain: Path, *args: str) -> tuple[str, ...]:
    return ("git", "-C", str(brain), *args)


def _git_remote(brain: Path) -> tuple[str, ...]:
    return _git(brain, "remote")


def _git_upstream(brain: Path) -> tuple[str, ...]:
    return _git(brain, "rev-parse", "--abbrev-ref", "@{u}")


def _git_ahead(brain: Path, upstream: str) -> tuple[str, ...]:
    return _git(brain, "rev-list", "--count", f"{upstream}..HEAD")


def _git_upstream_ct(brain: Path, upstream: str) -> tuple[str, ...]:
    return _git(brain, "log", "-1", "--format=%ct", upstream)


def _git_ls_files(brain: Path) -> tuple[str, ...]:
    return _git(brain, "ls-files", "-z")


def _launchctl_list(labels: dict[str, tuple[str, str]]) -> str:
    """Render `launchctl list` output. Values are (pid, last_exit) columns."""
    lines = ["PID\tStatus\tLabel"]
    for label, (pid, status) in labels.items():
        lines.append(f"{pid}\t{status}\t{label}")
    return "\n".join(lines) + "\n"


def _push_run_map(
    brain: Path,
    *,
    remotes: str = "origin\n",
    upstream: str = "origin/main",
    ahead: int = 0,
    last_push_hours_ago: float = 1.0,
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    return {
        _git_remote(brain): (0, remotes, ""),
        _git_upstream(brain): (0, f"{upstream}\n", ""),
        _git_ahead(brain, upstream): (0, f"{ahead}\n", ""),
        _git_upstream_ct(brain, upstream): (
            0, f"{int(_hours_ago(last_push_hours_ago))}\n", "",
        ),
    }


def _claude_source(home: Path, slug: str, name: str, *, hours_ago: float) -> Path:
    return _write(
        home / ".claude" / "projects" / slug / "memory" / name,
        "source memory\n",
        hours_ago=hours_ago,
    )


def _mirror(brain: Path, slug: str, name: str, *, hours_ago: float) -> Path:
    return _write(
        brain / "imports" / "claude" / "projects" / slug / "memory" / name,
        "mirrored memory\n",
        hours_ago=hours_ago,
    )


def _synthetic_repo_and_brain(tmp_path: Path, brain: Path) -> Path:
    """Minimal brainstack repo + matching brain layout (shape borrowed from
    tests/test_check_freshness.py) plus a real check_freshness.py so the
    drift check can import it by path."""
    repo = tmp_path / "repo"
    _write(repo / "agent" / "tools" / "alpha.py", "print('alpha v1')\n")
    _write(repo / "agent" / "memory" / "core.py", "X = 1\n")
    _write(repo / "agent" / "harness" / "hook.py", "Y = 1\n")
    _write(repo / "install.sh", "#!/bin/sh\n")
    shutil.copy2(
        REPO_ROOT / "agent" / "tools" / "check_freshness.py",
        repo / "agent" / "tools" / "check_freshness.py",
    )
    for sub in ("tools", "memory", "harness"):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    _write(brain / "tools" / "alpha.py", "print('alpha v1')\n")
    _write(brain / "tools" / "check_freshness.py",
           (REPO_ROOT / "agent" / "tools" / "check_freshness.py").read_text())
    _write(brain / "memory" / "core.py", "X = 1\n")
    _write(brain / "harness" / "hook.py", "Y = 1\n")
    return repo


# ---------- fixtures ---------------------------------------------------


@pytest.fixture
def make_env():
    """Factory for a fully injected `HealthEnv`.

    Usage:
        env = make_env(tmp_path, run_map={...}, now=NOW)

    `brain_root`, `home` and `cwd` are created empty under `tmp_path`; each
    test populates only what its check reads.
    """

    def _make(
        tmp_path: Path,
        *,
        run_map: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
        now: datetime = NOW,
        platform: str = "darwin",
        connect=lambda p, t: False,
    ):
        table = dict(run_map or {})
        brain = tmp_path / "brain"
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        for d in (brain, home, cwd):
            d.mkdir(parents=True, exist_ok=True)

        def _run(argv):
            return table.get(tuple(argv), (1, "", "not stubbed"))

        return health.HealthEnv(
            brain_root=brain,
            home=home,
            cwd=cwd,
            now=now,
            platform=platform,
            run=_run,
            connect_unix=connect,
        )

    return _make


# ---------- imports_freshness ------------------------------------------


def test_imports_freshness_pass_when_mirror_current(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=5)
    _mirror(env.brain_root, "-Users-x-proj", "MEMORY.md", hours_ago=1)

    res = health.check_imports_freshness(env)

    assert res.id == "imports_freshness"
    assert res.status == "PASS"


def test_imports_freshness_warn_after_2h(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=1)
    _mirror(env.brain_root, "-Users-x-proj", "MEMORY.md", hours_ago=4)

    res = health.check_imports_freshness(env)

    assert res.status == "WARN"
    assert "MEMORY.md" in res.evidence
    assert "-Users-x-proj" in res.evidence
    assert "lag" in res.evidence
    assert "3h" in res.evidence
    assert "--setup-claude-extras" in res.fix


def test_imports_freshness_fail_after_24h(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=1)
    _mirror(env.brain_root, "-Users-x-proj", "MEMORY.md", hours_ago=31)

    res = health.check_imports_freshness(env)

    assert res.status == "FAIL"
    assert "lag" in res.evidence
    assert "never mirrored" not in res.evidence


def test_imports_freshness_warn_threshold_is_exclusive_at_2h(make_env, tmp_path: Path):
    """`IMPORTS_WARN_HOURS` is a strict `>` bound (plan: "> 2 -> WARN"), so a
    lag of exactly 2 h is still a PASS. One second more is a WARN.

    The mirror runs hourly, so a lag hovering at the threshold is the normal
    steady state; making 2.0 h itself a WARN would fire on every healthy brain.
    """
    env = make_env(tmp_path)
    src = _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=1)
    dst = _mirror(env.brain_root, "-Users-x-proj", "MEMORY.md", hours_ago=3)
    assert src.stat().st_mtime - dst.stat().st_mtime == 2 * 3600

    assert health.check_imports_freshness(env).status == "PASS"

    _utime(dst, _hours_ago(3) - 1)
    assert health.check_imports_freshness(env).status == "WARN"


def test_imports_freshness_fail_threshold_is_exclusive_at_24h(make_env, tmp_path: Path):
    """`IMPORTS_FAIL_HOURS` is the same strict bound (plan: "> 24 -> FAIL"):
    exactly 24 h is the worst WARN, 24 h + 1 s is the first FAIL."""
    env = make_env(tmp_path)
    src = _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=1)
    dst = _mirror(env.brain_root, "-Users-x-proj", "MEMORY.md", hours_ago=25)
    assert src.stat().st_mtime - dst.stat().st_mtime == 24 * 3600

    assert health.check_imports_freshness(env).status == "WARN"

    _utime(dst, _hours_ago(25) - 1)
    assert health.check_imports_freshness(env).status == "FAIL"


def test_imports_freshness_fail_when_never_mirrored(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=1)
    # No brain/imports/claude/projects tree at all.

    res = health.check_imports_freshness(env)

    assert res.status == "FAIL"
    assert "never mirrored" in res.evidence
    assert "--setup-claude-extras" in res.fix


def test_imports_freshness_skip_without_sources(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    # No ~/.claude/projects at all: nothing to mirror, nothing to report.

    res = health.check_imports_freshness(env)

    assert res.status == "SKIP"


def test_imports_freshness_ignores_symlinked_memory_dirs(make_env, tmp_path: Path):
    """A project whose `memory/` is a symlink into the brain is the brain
    itself (claude_misc_adapter skips those). It must not count as a source,
    otherwise the check reports a permanent lag against the brain's own files."""
    env = make_env(tmp_path)
    real = env.brain_root / "memory"
    _write(real / "MEMORY.md", "brain memory\n", hours_ago=1)
    proj = env.home / ".claude" / "projects" / "-Users-x-brainstack"
    proj.mkdir(parents=True)
    (proj / "memory").symlink_to(real, target_is_directory=True)

    res = health.check_imports_freshness(env)

    assert res.status == "SKIP"


# ---------- launch_agents ----------------------------------------------


def test_launch_agents_skip_on_linux(make_env, tmp_path: Path):
    env = make_env(tmp_path, platform="linux")

    res = health.check_launch_agents(env)

    assert res.status == "SKIP"


def test_launch_agents_fail_when_plist_present_not_loaded(make_env, tmp_path: Path):
    listing = _launchctl_list({"com.user.agent-dream": ("2001", "0")})
    env = make_env(tmp_path, run_map={("launchctl", "list"): (0, listing, "")})
    agents = env.home / "Library" / "LaunchAgents"
    _write(agents / "com.user.agent-sync.plist", "<plist/>")
    _write(agents / "com.user.agent-dream.plist", "<plist/>")

    res = health.check_launch_agents(env)

    assert res.status == "FAIL"
    assert "sync" in res.evidence
    assert "missing" in res.evidence


def test_launch_agents_warn_on_nonzero_last_exit(make_env, tmp_path: Path):
    listing = _launchctl_list({
        "com.user.agent-sync": ("-", "1"),
        "com.user.agent-dream": ("2001", "0"),
    })
    env = make_env(tmp_path, run_map={("launchctl", "list"): (0, listing, "")})
    agents = env.home / "Library" / "LaunchAgents"
    _write(agents / "com.user.agent-sync.plist", "<plist/>")
    _write(agents / "com.user.agent-dream.plist", "<plist/>")

    res = health.check_launch_agents(env)

    assert res.status == "WARN"
    assert "sync(last exit 1)" in res.evidence


def test_launch_agents_warn_when_claude_extras_missing_with_sources(
    make_env, tmp_path: Path
):
    listing = _launchctl_list({
        "com.user.agent-sync": ("1000", "0"),
        "com.user.agent-dream": ("2001", "0"),
    })
    env = make_env(tmp_path, run_map={("launchctl", "list"): (0, listing, "")})
    agents = env.home / "Library" / "LaunchAgents"
    _write(agents / "com.user.agent-sync.plist", "<plist/>")
    _write(agents / "com.user.agent-dream.plist", "<plist/>")
    # There ARE Claude project memories to mirror, but no extras agent.
    _claude_source(env.home, "-Users-x-proj", "MEMORY.md", hours_ago=1)

    res = health.check_launch_agents(env)

    assert res.status == "WARN"
    assert "claude-extras" in res.evidence
    assert "--setup-claude-extras" in res.fix


def test_launch_agents_pass_all_loaded(make_env, tmp_path: Path):
    listing = _launchctl_list({
        "com.user.agent-sync": ("1000", "0"),
        "com.user.agent-dream": ("2001", "0"),
    })
    env = make_env(tmp_path, run_map={("launchctl", "list"): (0, listing, "")})
    agents = env.home / "Library" / "LaunchAgents"
    _write(agents / "com.user.agent-sync.plist", "<plist/>")
    _write(agents / "com.user.agent-dream.plist", "<plist/>")
    # auto-migrate and recall-daemon plists are absent: not mentioned, not a problem.

    res = health.check_launch_agents(env)

    assert res.status == "PASS"
    assert "auto-migrate" not in res.evidence
    assert "recall-daemon" not in res.evidence
    # launchd is per-USER, not per-brain: run against a sandbox brain root
    # this check still reports the developer's live agents. Say whose
    # launchd it is so the line is not read as a fact about the brain root
    # printed above it (2026-09-04 smoke test, "Read as a human" #9).
    assert res.evidence.startswith("user launchd: loaded:")


# ---------- brain_push -------------------------------------------------


def test_brain_push_skip_without_origin(make_env, tmp_path: Path):
    """A local-only brain (git repo, no remote) has nothing to be behind on."""
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    env = make_env(tmp_path, run_map={_git_remote(brain): (0, "", "")})
    (brain / ".git").mkdir(exist_ok=True)

    res = health.check_brain_push(env)

    assert res.status == "SKIP"


def test_brain_push_pass_in_sync(make_env, tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    env = make_env(tmp_path, run_map=_push_run_map(brain, ahead=0))
    (brain / ".git").mkdir(exist_ok=True)

    res = health.check_brain_push(env)

    assert res.status == "PASS"
    assert "origin/main" in res.evidence


def test_brain_push_pass_ahead_recent(make_env, tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    env = make_env(
        tmp_path,
        run_map=_push_run_map(brain, ahead=2, last_push_hours_ago=1.0),
    )
    (brain / ".git").mkdir(exist_ok=True)

    res = health.check_brain_push(env)

    assert res.status == "PASS"
    assert "2 commits ahead of origin/main" in res.evidence


def test_brain_push_evidence_is_singular_for_one_commit(make_env, tmp_path: Path):
    """"1 commits ahead" reads as a bug in the tool, which costs the whole
    line its credibility (2026-09-04 smoke test)."""
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    env = make_env(
        tmp_path,
        run_map=_push_run_map(brain, ahead=1, last_push_hours_ago=1.0),
    )
    (brain / ".git").mkdir(exist_ok=True)

    res = health.check_brain_push(env)

    assert "1 commit ahead of origin/main" in res.evidence
    assert "1 commits" not in res.evidence


def test_brain_push_fail_ahead_stale_quotes_remote_error(make_env, tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    remote_error = (
        "remote: error: File memory/episodic/codex/AGENT_LEARNINGS.jsonl is "
        "107.26 MB; this exceeds GitHub's file size limit of 100.00 MB"
    )
    env = make_env(
        tmp_path,
        run_map=_push_run_map(brain, ahead=67, last_push_hours_ago=125.0),
    )
    (brain / ".git").mkdir(exist_ok=True)
    _write(brain / "sync.log", "\n".join([
        "2026-08-30T06:53:00Z sync: starting",
        f"2026-08-30T06:53:10Z {remote_error}",
        "2026-08-30T06:53:11Z sync: commit succeeded but push failed",
    ]) + "\n")

    res = health.check_brain_push(env)

    assert res.status == "FAIL"
    assert "67 commits ahead of origin/main" in res.evidence
    assert "last remote error:" in res.evidence
    assert remote_error in res.evidence
    assert "large_tracked_files" in res.fix


def test_sync_log_remote_error_prefers_remote_error_line(tmp_path: Path):
    text = "\n".join([
        "2026-09-04T12:00:00Z sync: starting",
        "2026-09-04T12:00:01Z Enumerating objects: 42, done.",
        "2026-09-04T12:00:02Z remote: error: File big.jsonl is 107.26 MB; this "
        "exceeds GitHub's file size limit of 100.00 MB",
        "2026-09-04T12:00:03Z error: failed to push some refs",
        "2026-09-04T12:00:04Z sync: commit succeeded but push failed",
    ]) + "\n"

    got = health._sync_log_remote_error(text)

    assert got is not None
    # The timestamp prefix sync.sh adds is stripped; the git line is quoted raw.
    assert got.startswith("remote: error: File big.jsonl")
    assert "100.00 MB" in got


def test_sync_log_remote_error_scoped_to_last_run(tmp_path: Path):
    """Two consecutive runs, in the order sync.sh actually writes them:
    each run's terminal marker is followed by its EXIT trap's `health:`
    line. Only the newer run's error may be quoted."""
    text = "\n".join([
        "2026-09-03T10:00:00Z remote: error: OLD-RUN-ERROR should not be quoted",
        "2026-09-03T10:00:01Z sync: commit succeeded but push failed",
        "2026-09-03T10:00:02Z health: wrote runtime/health.json",
        "2026-09-04T12:00:00Z remote: error: NEW-RUN-ERROR exceeds GitHub's file "
        "size limit of 100.00 MB",
        "2026-09-04T12:00:01Z sync: commit succeeded but push failed",
        "2026-09-04T12:00:02Z health: wrote runtime/health.json",
    ]) + "\n"

    got = health._sync_log_remote_error(text)

    assert got is not None
    assert "NEW-RUN-ERROR" in got
    assert "OLD-RUN-ERROR" not in got
    assert "health:" not in got


def test_sync_log_remote_error_skips_interleaved_health_lines(tmp_path: Path):
    """A `health:` line can also land mid-run (a manual `sync.sh` while the
    hourly agent is writing). It is still transparent: it neither ends the
    run nor gets quoted, even when it contains the word "error:"."""
    text = "\n".join([
        "2026-09-03T10:00:00Z remote: error: OLD-RUN-ERROR should not be quoted",
        "2026-09-03T10:00:01Z sync: pushed",
        "2026-09-04T12:00:00Z health: wrote runtime/health.json",
        "2026-09-04T12:00:01Z health: error: could not write runtime/health.json",
        "2026-09-04T12:00:02Z remote: error: NEW-RUN-ERROR exceeds GitHub's file "
        "size limit of 100.00 MB",
        "2026-09-04T12:00:03Z sync: commit succeeded but push failed",
    ]) + "\n"

    got = health._sync_log_remote_error(text)

    assert got is not None
    assert "NEW-RUN-ERROR" in got
    assert "OLD-RUN-ERROR" not in got
    assert "health:" not in got


# The exact tail `sync.sh` writes when a push is rejected: git's stderr,
# then the run's terminal marker, then the EXIT trap's `health:` line. The
# trap fires on EVERY exit path, so the terminal marker is never physically
# the last line — the scan has to treat `health:` as transparent or it
# breaks one line above the error it was sent to find (2026-09-04 smoke
# test, Defect 1). The twin parser in
# `agent/tools/render_pending_summary.py` is pinned on this same text by
# `tests/test_render_pending.py::...::test_remote_error_parsers_agree`.
REMOTE_ERROR_LINE = (
    "remote: error: File memory/semantic/digests/huge-transcript-dump.md is "
    "51.00 MB; this exceeds GitHub's file size limit of 100.00 MB"
)
SYNC_TAIL_WITH_HEALTH_TRAILER = [
    "2026-09-04T15:36:20Z sync: starting",
    f"2026-09-04T15:36:22Z {REMOTE_ERROR_LINE}",
    "2026-09-04T15:36:23Z sync: commit succeeded but push failed; brain is "
    "committed locally",
    "2026-09-04T15:36:24Z health: wrote runtime/health.json",
]

# A dead remote: git never reaches GitHub, so there is no `remote: error:`
# at all — only `fatal:`. The first one names the cause; the second is
# boilerplate.
FIRST_FATAL_LINE = (
    "fatal: '/tmp/rsd-smoke-oAQ6/remote.git' does not appear to be a git repository"
)
SYNC_TAIL_TWO_FATALS = [
    "2026-09-04T15:36:20Z sync: starting",
    f"2026-09-04T15:36:21Z {FIRST_FATAL_LINE}",
    "2026-09-04T15:36:21Z fatal: Could not read from remote repository.",
    "2026-09-04T15:36:22Z sync: commit succeeded but push failed; brain is "
    "committed locally",
    "2026-09-04T15:36:23Z health: wrote runtime/health.json",
]


def test_sync_log_remote_error_survives_the_trailing_health_line():
    """The shape production actually writes: ERR, terminal marker, `health:`.

    Before the fix this returned `None` on every real sync failure, because
    the marker was never at the physical last index and the scan broke
    before reaching git's stderr.
    """
    got = health._sync_log_remote_error("\n".join(SYNC_TAIL_WITH_HEALTH_TRAILER) + "\n")

    assert got == REMOTE_ERROR_LINE


def test_sync_log_remote_error_returns_first_fatal_when_no_remote_error():
    """Client-side failures (dead remote, bad auth, DNS) print `fatal:`,
    never `remote: error:`. Quote the first one — it carries the cause."""
    got = health._sync_log_remote_error("\n".join(SYNC_TAIL_TWO_FATALS) + "\n")

    assert got == FIRST_FATAL_LINE


@pytest.mark.parametrize("tail", [
    ["2026-09-04T15:36:23Z sync: pushed",
     "2026-09-04T15:36:24Z health: error: could not write runtime/health.json"],
    ["2026-09-04T15:36:23Z sync: pushed",
     "2026-09-04T15:36:24Z health: fatal: runtime/health.json is unwritable"],
])
def test_sync_log_remote_error_never_quotes_a_health_line(tail):
    """`health:` lines are brainstack's own output, not git's. A clean push
    followed by a failed health write must not look like a push failure."""
    assert health._sync_log_remote_error("\n".join(tail) + "\n") is None


def test_brain_push_quotes_remote_error_past_the_health_trailer(make_env, tmp_path: Path):
    """End to end: the FAIL evidence carries the git line even though the
    EXIT trap appended a `health:` line after the run's terminal marker."""
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    env = make_env(
        tmp_path,
        run_map=_push_run_map(brain, ahead=2, last_push_hours_ago=6.0),
    )
    (brain / ".git").mkdir(exist_ok=True)
    _write(brain / "sync.log", "\n".join(SYNC_TAIL_WITH_HEALTH_TRAILER) + "\n")

    res = health.check_brain_push(env)

    assert res.status == "FAIL"
    assert f"last remote error: {REMOTE_ERROR_LINE}" in res.evidence


# ---------- large_tracked_files ----------------------------------------


def test_large_tracked_files_fail_lists_path_and_mb(make_env, tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    tracked = [
        "memory/episodic/codex/AGENT_LEARNINGS.jsonl",
        "runtime/logs/events.log.jsonl",
        "memory/episodic/claude-sessions/AGENT_LEARNINGS.jsonl",
        "memory/episodic/AGENT_LEARNINGS.jsonl",
        "MEMORY.md",
    ]
    sizes = [110 * MB, 90 * MB, 70 * MB, 55 * MB, 4 * 1024]
    for rel, size in zip(tracked, sizes):
        _sparse(brain / rel, size)
    env = make_env(tmp_path, run_map={
        _git_ls_files(brain): (0, "\0".join(tracked) + "\0", ""),
    })

    res = health.check_large_tracked_files(env)

    assert res.status == "FAIL"
    # Largest three only: a fourth oversize file is over the limit but is not
    # listed, so the line stays readable in a session banner.
    assert "memory/episodic/codex/AGENT_LEARNINGS.jsonl" in res.evidence
    assert "runtime/logs/events.log.jsonl" in res.evidence
    assert "memory/episodic/claude-sessions/AGENT_LEARNINGS.jsonl" in res.evidence
    assert "memory/episodic/AGENT_LEARNINGS.jsonl " not in res.evidence
    assert "MEMORY.md" not in res.evidence
    assert "rm --cached" in res.fix
    assert "exceed 50 MB" in res.evidence


def test_large_tracked_files_verb_agrees_with_one_file(make_env, tmp_path: Path):
    """One file exceedS. The banner shows this line verbatim, so the
    disagreement is read by a human every session (2026-09-04 smoke test)."""
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    tracked = ["memory/semantic/digests/huge-transcript-dump.md", "MEMORY.md"]
    for rel, size in zip(tracked, [51 * MB, 4 * 1024]):
        _sparse(brain / rel, size)
    env = make_env(tmp_path, run_map={
        _git_ls_files(brain): (0, "\0".join(tracked) + "\0", ""),
    })

    res = health.check_large_tracked_files(env)

    assert res.status == "FAIL"
    assert "exceeds 50 MB" in res.evidence
    assert "exceed 50 MB" not in res.evidence


def test_large_tracked_files_pass(make_env, tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    tracked = ["MEMORY.md", "memory/episodic/AGENT_LEARNINGS.jsonl"]
    for rel in tracked:
        _sparse(brain / rel, 3 * MB)
    env = make_env(tmp_path, run_map={
        _git_ls_files(brain): (0, "\0".join(tracked) + "\0", ""),
    })

    res = health.check_large_tracked_files(env)

    assert res.status == "PASS"


def test_large_tracked_files_threshold_is_exclusive_at_50mib(make_env, tmp_path: Path):
    """`TRACKED_FILE_FAIL_BYTES` is 50 * 1024 * 1024 and the rule is "> 50 MB",
    so a file of exactly that size passes and one byte more fails."""
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    rel = "memory/episodic/codex/AGENT_LEARNINGS.jsonl"
    run_map = {_git_ls_files(brain): (0, rel + "\0", "")}

    _sparse(brain / rel, health.TRACKED_FILE_FAIL_BYTES)
    env = make_env(tmp_path, run_map=run_map)
    assert health.check_large_tracked_files(env).status == "PASS"

    _sparse(brain / rel, health.TRACKED_FILE_FAIL_BYTES + 1)
    assert health.check_large_tracked_files(env).status == "FAIL"


# ---------- log_sizes --------------------------------------------------


def test_log_sizes_warn_over_20mb_includes_rolled_and_namespaces(
    make_env, tmp_path: Path
):
    env = make_env(tmp_path)
    brain = env.brain_root
    # Over threshold: one ROLLED event log and one NAMESPACED episodic file.
    _sparse(brain / "runtime" / "logs" / "events.log.2026-09-03.jsonl", 30 * MB)
    _sparse(brain / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl", 25 * MB)
    # Under threshold: must not be listed.
    _sparse(brain / "runtime" / "logs" / "events.log.jsonl", 1 * MB)
    _sparse(brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 1 * MB)

    res = health.check_log_sizes(env)

    assert res.status == "WARN"
    assert "events.log.2026-09-03.jsonl" in res.evidence
    assert "codex/AGENT_LEARNINGS.jsonl" in res.evidence
    assert "exceed 20 MB" in res.evidence
    assert res.evidence.count("events.log") == 1
    assert res.evidence.count("AGENT_LEARNINGS") == 1


def test_log_sizes_verb_agrees_with_one_file(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _sparse(env.brain_root / "runtime" / "logs" / "events.log.jsonl", 30 * MB)

    res = health.check_log_sizes(env)

    assert res.status == "WARN"
    assert "exceeds 20 MB" in res.evidence


def test_log_sizes_pass(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _sparse(env.brain_root / "runtime" / "logs" / "events.log.jsonl", 2 * MB)
    _sparse(env.brain_root / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 2 * MB)

    res = health.check_log_sizes(env)

    assert res.status == "PASS"


def test_log_sizes_threshold_is_exclusive_at_20mib(make_env, tmp_path: Path):
    """`LOG_WARN_BYTES` is 20 * 1024 * 1024 and the rule is "> 20 MB". A log
    sitting exactly on the rotation threshold has not overshot it yet, so it
    is a PASS; one byte past it is the first WARN."""
    env = make_env(tmp_path)
    log = env.brain_root / "runtime" / "logs" / "events.log.jsonl"

    _sparse(log, health.LOG_WARN_BYTES)
    assert health.check_log_sizes(env).status == "PASS"

    _sparse(log, health.LOG_WARN_BYTES + 1)
    assert health.check_log_sizes(env).status == "WARN"


# ---------- dream_cycle ------------------------------------------------


def _dream_status(
    brain: Path,
    *,
    hours_ago: float,
    llm_errors: dict | None = None,
    ok: bool = True,
) -> Path:
    ts = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema_version": 1,
        "ts": ts,
        "namespace": "default",
        "ok": ok,
        "summary": "dream cycle: patterns=0 staged=0 kept=43770",
        "staged": 0,
        "kept": 43770,
        "archived": 25,
        "consolidate_claims": 0,
        "llm_calls": 3,
        "llm_errors": llm_errors or {},
        "error": None,
    }
    path = brain / "runtime" / "dream_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_dream_fail_when_status_older_than_36h(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _dream_status(env.brain_root, hours_ago=40)

    res = health.check_dream_cycle(env)

    assert res.status == "FAIL"
    assert "last cycle" in res.evidence
    assert "ago" in res.evidence


def test_dream_fail_on_provider_unavailable(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _dream_status(env.brain_root, hours_ago=6,
                  llm_errors={"provider_unavailable": 3})

    res = health.check_dream_cycle(env)

    assert res.status == "FAIL"
    assert "llm_errors=provider_unavailable=3" in res.evidence
    assert "--setup-launchd" in res.fix


def test_dream_pass_recent_clean(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    _dream_status(env.brain_root, hours_ago=6)

    res = health.check_dream_cycle(env)

    assert res.status == "PASS"


def test_dream_fallback_parses_dream_log(make_env, tmp_path: Path):
    """No dream_status.json yet (pre-upgrade brain): fall back to dream.log's
    mtime plus its last `dream cycle:` line."""
    env = make_env(tmp_path)
    _write(
        env.brain_root / "dream.log",
        "dream cycle: patterns=0 staged=0 kept=100 "
        "llm_calls=3 llm_errors=provider_unavailable=2\n",
        hours_ago=2,
    )

    res = health.check_dream_cycle(env)

    assert res.status == "FAIL"
    assert "provider_unavailable" in res.evidence


def test_dream_skip_when_never_ran(make_env, tmp_path: Path):
    env = make_env(tmp_path)

    res = health.check_dream_cycle(env)

    assert res.status == "SKIP"
    assert "dream never ran" in res.evidence


# ---------- drift ------------------------------------------------------


def test_drift_warn_uses_check_freshness_summary(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    repo = _synthetic_repo_and_brain(tmp_path, env.brain_root)
    # Repo gains a tool the brain never received.
    _write(repo / "agent" / "tools" / "gamma.py", "print('gamma')\n")
    _write(env.brain_root / ".brainstack-repo-path", str(repo) + "\n")

    res = health.check_drift(env)

    assert res.status == "WARN"
    assert "drift detected:" in res.evidence
    assert "missing" in res.evidence
    assert "--upgrade" in res.fix


def test_drift_pass_in_sync(make_env, tmp_path: Path):
    env = make_env(tmp_path)
    repo = _synthetic_repo_and_brain(tmp_path, env.brain_root)
    _write(env.brain_root / ".brainstack-repo-path", str(repo) + "\n")

    res = health.check_drift(env)

    assert res.status == "PASS"
    assert "in sync" in res.evidence
    assert str(repo) in res.evidence


def test_drift_skip_without_pin_or_repo(make_env, tmp_path: Path, monkeypatch):
    """No `.brainstack-repo-path` pin and the installed `recall` package is
    not sitting inside a brainstack checkout: nothing to compare against."""
    env = make_env(tmp_path)
    fake_pkg = tmp_path / "site-packages" / "recall" / "__init__.py"
    fake_pkg.parent.mkdir(parents=True)
    fake_pkg.write_text("", encoding="utf-8")
    monkeypatch.setattr(recall, "__file__", str(fake_pkg))

    res = health.check_drift(env)

    assert res.status == "SKIP"


# ---------- auto_recall_config -----------------------------------------


_RUNTIME_SECTION = "[tool.recall.runtime]\nenable_auto_recall = {}\n"


def test_auto_recall_fail_names_the_layer_that_wrote_false(
    make_env, tmp_path: Path, monkeypatch
):
    """S1 merges per key, so an incomplete cwd section shadows nothing. The
    only way to be OFF is a layer that writes `false` — name that file and
    that value, not whichever layer happens to rank highest."""
    env = make_env(tmp_path)
    monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(env.home))
    _write(env.brain_root / "runtime" / "pyproject.toml",
           _RUNTIME_SECTION.format("true"))
    # The worktree's own pyproject explicitly turns auto-recall OFF.
    cwd_cfg = _write(env.cwd / "pyproject.toml", _RUNTIME_SECTION.format("false"))

    res = health.check_auto_recall_config(env)

    assert res.status == "FAIL"
    assert "auto-recall OFF" in res.evidence
    assert str(env.cwd) in res.evidence
    assert f"{cwd_cfg} sets enable_auto_recall = false" in res.evidence
    assert f"set enable_auto_recall = true in {cwd_cfg}" in res.fix
    assert "remove the key" in res.fix
    assert "[tool.recall.runtime]" in res.fix
    # The pre-S1 wording promised a merge that has since landed.
    assert "S1 merges per key once landed" not in res.fix
    assert "shadows" not in res.evidence


def test_auto_recall_fail_names_the_env_override_layer(
    make_env, tmp_path: Path, monkeypatch
):
    """`$RECALL_RUNTIME_CONFIG` outranks everything. Saying so is the
    difference between a 5-second fix and a hunt through three files."""
    env = make_env(tmp_path)
    monkeypatch.setenv("HOME", str(env.home))
    _write(env.brain_root / "runtime" / "pyproject.toml",
           _RUNTIME_SECTION.format("true"))
    _write(env.cwd / "pyproject.toml", _RUNTIME_SECTION.format("true"))
    override = _write(tmp_path / "override.toml", _RUNTIME_SECTION.format("false"))
    monkeypatch.setenv("RECALL_RUNTIME_CONFIG", str(override))

    res = health.check_auto_recall_config(env)

    assert res.status == "FAIL"
    assert f"{override} (via $RECALL_RUNTIME_CONFIG)" in res.evidence
    assert str(override) in res.fix


def test_auto_recall_pass_when_enabled(make_env, tmp_path: Path, monkeypatch):
    env = make_env(tmp_path)
    monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(env.home))
    _write(env.brain_root / "runtime" / "pyproject.toml",
           _RUNTIME_SECTION.format("true"))
    _write(env.cwd / "pyproject.toml", _RUNTIME_SECTION.format("true"))

    res = health.check_auto_recall_config(env)

    assert res.status == "PASS"


def test_auto_recall_skip_when_not_enabled_globally(make_env, tmp_path: Path, monkeypatch):
    env = make_env(tmp_path)
    monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(env.home))
    # No global runtime pyproject anywhere: the user never opted in.

    res = health.check_auto_recall_config(env)

    assert res.status == "SKIP"
    assert "not enabled globally" in res.evidence


# ---------- daemon -----------------------------------------------------


def test_daemon_skip_when_not_configured(make_env, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RECALL_DAEMON_SOCKET", raising=False)
    env = make_env(tmp_path)

    res = health.check_daemon(env)

    assert res.status == "SKIP"
    assert "not configured" in res.evidence
    # Even the SKIP names the path it looked at: "no socket" is only
    # believable if the reader can see WHICH socket was probed.
    assert str(env.brain_root / "runtime" / "recall.sock") in res.evidence


def test_daemon_fail_when_socket_refuses(make_env, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RECALL_DAEMON_SOCKET", raising=False)
    calls: list[Path] = []

    def _connect(path, timeout):
        calls.append(Path(path))
        return False

    env = make_env(tmp_path, connect=_connect)
    _write(
        env.home / "Library" / "LaunchAgents" / "com.brainstack.recall-daemon.plist",
        "<plist/>",
    )

    res = health.check_daemon(env)

    assert res.status == "FAIL"
    assert "refused" in res.evidence
    assert calls == [env.brain_root / "runtime" / "recall.sock"]
    assert "launchctl kickstart" in res.fix


def test_daemon_pass_when_connect_ok(make_env, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RECALL_DAEMON_SOCKET", raising=False)
    env = make_env(tmp_path, connect=lambda p, t: True)
    _write(env.brain_root / "runtime" / "recall.sock", "")

    res = health.check_daemon(env)

    assert res.status == "PASS"


def test_daemon_honours_recall_daemon_socket_env(make_env, tmp_path: Path, monkeypatch):
    """`recall doctor` and the hook resolve the socket through
    `recall.config.daemon_socket_path()`. Health was a fourth surface with
    its own hardcoded `<brain>/runtime/recall.sock`, so a daemon started
    with `$RECALL_DAEMON_SOCKET` elsewhere was reported as absent while
    `recall doctor` reported it running (2026-09-04 smoke test, Defect 2)."""
    sock = tmp_path / "elsewhere" / "recall.sock"
    _write(sock, "")
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(sock))
    calls: list[Path] = []

    def _connect(path, timeout):
        calls.append(Path(path))
        return True

    env = make_env(tmp_path, connect=_connect)
    # No plist, and nothing at the brain-root default: the old code SKIPped.
    assert not (env.brain_root / "runtime" / "recall.sock").exists()

    res = health.check_daemon(env)

    assert res.status == "PASS"
    assert calls == [sock]
    assert str(sock) in res.evidence


# ---------- report model ------------------------------------------------


def _fixed(id_: str, status: str, evidence: str = "e", fix: str = ""):
    def _check(env):
        return health.CheckResult(id=id_, status=status, evidence=evidence, fix=fix)

    return _check


def test_run_health_status_fail_if_any_fail(make_env, tmp_path: Path, monkeypatch):
    env = make_env(tmp_path)
    monkeypatch.setattr(health, "CHECKS", (
        _fixed("alpha", "PASS"),
        _fixed("beta", "WARN"),
        _fixed("gamma", "FAIL", "gamma is broken"),
        _fixed("delta", "SKIP"),
    ))

    report = health.run_health(env)

    assert report.status == "FAIL"
    assert report.generated_at == "2026-09-04T13:00:00Z"
    assert report.brain_root == str(env.brain_root)
    counts = report.counts()
    assert counts["FAIL"] == 1
    assert counts["WARN"] == 1
    assert counts["PASS"] == 1
    assert counts["SKIP"] == 1
    assert [c.id for c in report.failures()] == ["gamma"]


def test_check_exception_becomes_warn(make_env, tmp_path: Path, monkeypatch):
    def check_boom(env):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(health, "CHECKS", (_fixed("alpha", "PASS"), check_boom))
    env = make_env(tmp_path)

    report = health.run_health(env)

    crashed = [c for c in report.checks if c.status == "WARN"]
    assert len(crashed) == 1
    assert "boom" in crashed[0].id
    assert crashed[0].evidence.startswith("check crashed:")
    assert "kaboom" in crashed[0].evidence
    # A crashing check degrades the report; it never takes the process down.
    assert report.status == "WARN"


def test_report_json_round_trip():
    report = health.HealthReport(
        generated_at="2026-09-04T13:02:11Z",
        brain_root="/Users/x/.agent",
        cwd="/Users/x/.agent",
        checks=[
            health.CheckResult("brain_push", "FAIL", "67 commits ahead", "sync.sh"),
            health.CheckResult("daemon", "SKIP", "not configured"),
        ],
    )

    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["schema_version"] == health.SCHEMA_VERSION
    assert payload["generated_at"] == "2026-09-04T13:02:11Z"
    assert payload["brain_root"] == "/Users/x/.agent"
    assert payload["cwd"] == "/Users/x/.agent"
    assert payload["status"] == "FAIL"
    assert payload["counts"]["FAIL"] == 1
    assert payload["checks"][0] == {
        "id": "brain_push",
        "status": "FAIL",
        "evidence": "67 commits ahead",
        "fix": "sync.sh",
    }

    back = health.HealthReport.from_dict(payload)

    assert back.generated_at == report.generated_at
    assert back.brain_root == report.brain_root
    assert back.cwd == report.cwd
    assert back.checks == report.checks
    assert back.status == "FAIL"


def test_render_human_line_shapes():
    report = health.HealthReport(
        generated_at="2026-09-04T13:02:11Z",
        brain_root="/Users/x/.agent",
        cwd="/Users/x/.agent",
        checks=[
            health.CheckResult(
                "imports_freshness", "FAIL", "never mirrored",
                "./install.sh --setup-claude-extras",
            ),
            health.CheckResult("log_sizes", "WARN", "events.log.jsonl 66.4 MB"),
            health.CheckResult("drift", "PASS", "in sync with /Users/x/brainstack"),
            health.CheckResult("daemon", "SKIP", "not configured"),
        ],
    )

    text = report.render_human()
    lines = text.splitlines()

    assert lines[0].startswith("== recall health ==")
    assert "brain: /Users/x/.agent" in lines[0]
    assert "generated: 2026-09-04T13:02:11Z" in lines[0]

    check_lines = [ln for ln in lines
                   if ln[:4] in ("FAIL", "WARN", "PASS", "SKIP")]
    assert len(check_lines) == 4
    # Status occupies the first column; the id column is aligned across rows.
    id_columns = {ln.index(c.id) for ln, c in zip(check_lines, report.checks)}
    assert len(id_columns) == 1
    for ln, c in zip(check_lines, report.checks):
        assert ln.startswith(c.status)
        assert ln.endswith(c.evidence)

    fix_lines = [ln for ln in lines if ln.strip().startswith("fix:")]
    assert fix_lines == ["      fix: ./install.sh --setup-claude-extras"]

    assert lines[-1] == "overall: FAIL (1 fail, 1 warn, 1 pass, 1 skip)"


def test_load_report_stale_returns_none(tmp_path: Path):
    path = tmp_path / "health.json"
    fresh = health.HealthReport(
        generated_at=(NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        brain_root=str(tmp_path),
        cwd=str(tmp_path),
        checks=[health.CheckResult("daemon", "SKIP", "not configured")],
    )
    path.write_text(json.dumps(fresh.to_dict()), encoding="utf-8")

    assert health.load_report(path, now=NOW) is not None

    stale = health.HealthReport(
        generated_at=(NOW - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        brain_root=str(tmp_path),
        cwd=str(tmp_path),
        checks=[health.CheckResult("daemon", "SKIP", "not configured")],
    )
    path.write_text(json.dumps(stale.to_dict()), encoding="utf-8")

    assert health.load_report(path, now=NOW) is None
    # An explicit wider window still returns it.
    assert health.load_report(path, now=NOW, max_age_hours=48.0) is not None


def _report_at(tmp_path: Path, hours_old: float) -> health.HealthReport:
    return health.HealthReport(
        generated_at=(NOW - timedelta(hours=hours_old)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        brain_root=str(tmp_path),
        cwd=str(tmp_path),
        checks=[health.CheckResult("daemon", "FAIL", "socket refused")],
    )


def test_read_report_separates_missing_from_stale(tmp_path: Path):
    """`load_report` collapses missing, corrupt and stale into one `None`,
    so the SessionStart banner — which must stay silent for the first two
    and warn for the third — had to read and parse the file twice.
    `read_report` answers both off one parse."""
    path = tmp_path / "health.json"

    assert health.read_report(path, now=NOW) == (None, False)

    path.write_text("{ not json", encoding="utf-8")
    assert health.read_report(path, now=NOW) == (None, False)

    path.write_text(json.dumps(_report_at(tmp_path, 2).to_dict()), encoding="utf-8")
    report, stale = health.read_report(path, now=NOW)
    assert report is not None and stale is False
    # The parsed report comes back whole, not just a freshness verdict.
    assert [c.id for c in report.failures()] == ["daemon"]

    path.write_text(json.dumps(_report_at(tmp_path, 40).to_dict()), encoding="utf-8")
    report, stale = health.read_report(path, now=NOW)
    assert stale is True
    # Stale still hands the report back: the banner quotes `generated_at`.
    assert report is not None
    assert report.generated_at.startswith("20")


def test_read_report_uses_the_module_stale_window(tmp_path: Path):
    """One window, `HEALTH_STALE_HOURS`, not a second copy of the number."""
    path = tmp_path / "health.json"
    just_inside = health.HEALTH_STALE_HOURS - 1
    just_outside = health.HEALTH_STALE_HOURS + 1

    path.write_text(
        json.dumps(_report_at(tmp_path, just_inside).to_dict()), encoding="utf-8")
    assert health.read_report(path, now=NOW)[1] is False

    path.write_text(
        json.dumps(_report_at(tmp_path, just_outside).to_dict()), encoding="utf-8")
    assert health.read_report(path, now=NOW)[1] is True


def test_read_report_unparseable_timestamp_is_stale(tmp_path: Path):
    """An unreadable `generated_at` is not evidence of freshness."""
    path = tmp_path / "health.json"
    report = _report_at(tmp_path, 1)
    data = report.to_dict()
    data["generated_at"] = "whenever"
    path.write_text(json.dumps(data), encoding="utf-8")

    got, stale = health.read_report(path, now=NOW)
    assert got is not None
    assert stale is True


def test_write_report_atomic_replaces(tmp_path: Path):
    path = tmp_path / "runtime" / "health.json"
    first = health.HealthReport(
        generated_at="2026-09-04T13:02:11Z",
        brain_root=str(tmp_path),
        cwd=str(tmp_path),
        checks=[health.CheckResult("daemon", "SKIP", "not configured")],
    )

    health.write_report(first, path)

    assert path.is_file(), "write_report must create the parent directory"
    assert json.loads(path.read_text())["checks"][0]["id"] == "daemon"

    second = health.HealthReport(
        generated_at="2026-09-04T14:02:11Z",
        brain_root=str(tmp_path),
        cwd=str(tmp_path),
        checks=[health.CheckResult("brain_push", "FAIL", "67 commits ahead")],
    )
    health.write_report(second, path)

    payload = json.loads(path.read_text())
    assert payload["generated_at"] == "2026-09-04T14:02:11Z"
    assert payload["status"] == "FAIL"
    # os.replace semantics: the temp file never survives a successful write.
    leftovers = [p.name for p in path.parent.iterdir() if p.name != "health.json"]
    assert leftovers == []


# What the EXIT trap's `recall health` can leave in sync.log when a venv's
# grpcio aborts at interpreter teardown: un-prefixed, after the run's marker.
STRAY_STDERR_AFTER_MARKER = [
    "Traceback (most recent call last):",
    '  File "/Users/me/.local/bin/recall", line 8, in <module>',
    "libc++abi: terminating due to uncaught exception of type "
    "std::__1::system_error: recursive_mutex lock failed: Invalid argument",
]


def test_sync_log_remote_error_survives_unprefixed_stderr_after_the_marker():
    """Un-prefixed lines after the marker are neither `health:` (transparent)
    nor `sync:` (a run's own line). Treating them as run lines made the
    current run's own marker end the scan before the git stderr above it,
    so every real failure read as "no error" (staff follow-up review, M1)."""
    tail = (
        SYNC_TAIL_WITH_HEALTH_TRAILER[:-1]
        + STRAY_STDERR_AFTER_MARKER
        + SYNC_TAIL_WITH_HEALTH_TRAILER[-1:]
    )

    got = health._sync_log_remote_error("\n".join(tail) + "\n")

    assert got == REMOTE_ERROR_LINE


def test_sync_log_remote_error_stays_scoped_with_stray_stderr_after_a_clean_run():
    """The previous run failed, the current one pushed; garbage after the
    current marker must not make yesterday's error current."""
    tail = SYNC_TAIL_WITH_HEALTH_TRAILER + [
        "2026-09-04T16:36:20Z sync: starting",
        "2026-09-04T16:36:23Z sync: pushed",
        *STRAY_STDERR_AFTER_MARKER,
        "2026-09-04T16:36:24Z health: wrote runtime/health.json",
    ]

    assert health._sync_log_remote_error("\n".join(tail) + "\n") is None
