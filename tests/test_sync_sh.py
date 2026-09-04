"""Subprocess tests for `agent/tools/sync.sh` — S5 slice C, requirement R3.

What this file pins (the frozen A<->C interface from the S5 plan):

  1. **Size gate.** A staged file larger than 50 MB is unstaged before the
     commit and logged, and *everything else still pushes*. GitHub rejects
     blobs over 100 MB with a server-side error, and today one oversize
     file blocks the whole brain: 67 commits sat unpushed for five days
     behind a 107 MB `AGENT_LEARNINGS.jsonl`. Fail open per file, exactly
     like the secret-scanner quarantine above it.
         sync: oversize (not pushed): <path> (<bytes> bytes > <limit>-byte limit)
         sync: held back <N> oversize file(s) (>50 MB); syncing the rest

  2. **Health write on every exit path.** `runtime/health.json` is written
     from an EXIT trap, so a no-op run, a failed push and a pre-commit
     rejection all leave a fresh report behind. That file is what
     PENDING_REVIEW.md and the SessionStart banner read; if it is only
     written on the happy path, the surfaces go quiet exactly when
     something is wrong.
         health: wrote runtime/health.json
         health: recall CLI not found; skipped

  3. **The `health:` prefix is load-bearing.** `render_pending_summary.
     _check_sync_status` classifies the *last* log line containing
     `sync:`. Health lines are appended after the run's terminal marker,
     so they must never carry that prefix or every run would classify as
     'ok'.

  4. **git's stderr stays adjacent to its marker.** The push error is what
     `recall health` quotes back to the user, so nothing may be logged
     between git's output and the `push failed` line.

Hermetic: a tmp HOME, a tmp git brain with a bare local remote, and a
sandboxed PATH holding symlinks to the handful of real binaries sync.sh
needs. No network, no `~/.agent`, no launchctl, no secret scanner (the
run is authorised with SYNC_ALLOW_NO_SCANNER=1). The `recall` CLI is
injected via RECALL_BIN as a stub, so nothing on the developer's PATH is
consulted.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Shells out to real git + real bash, so this module is slower than a unit
# test. It stays inside the hermetic CI subset (`-m "not embeddings and not
# machine"`) — nothing here touches the network or the developer's machine
# state.
pytestmark = pytest.mark.slow

# Everything sync.sh (and git) reaches for by bare name. Anything missing
# from this sandbox is a genuine dependency the script must not have.
SANDBOX_BINARIES = (
    "bash", "git", "wc", "tr", "date", "mkdir", "sleep", "kill", "cat",
    "env", "rm", "mv", "dirname", "basename", "python3",
)

# The SYNC_MAX_FILE_BYTES default. The gate holds a file back only when it
# is strictly greater than this, so 52428800 still syncs and 52428801 does
# not — see test_size_gate_boundary_at_50mib_and_one_byte_over.
SIZE_LIMIT_BYTES = 50 * 1024 * 1024

# 51 MB: comfortably over the limit, for the plain "held back" cases.
OVERSIZE_BYTES = 51 * 1024 * 1024

# 40 MB: the "before" size for the rename+oversize test. Git only reports a
# staged delete+add pair as a rename (`R<NNN>` in --name-status -M) when the
# two blobs clear its similarity threshold (50% by default) — an all-zero
# sparse file compared against a longer all-zero sparse file scores on
# shared length, so this must be a large enough fraction of OVERSIZE_BYTES
# to clear that bar (40 / 51 MB ≈ 78%, well above 50%). A byte-tiny "before"
# file would make git report a plain delete + add instead of a rename, which
# would not exercise the code path this test targets.
RENAME_BASE_BYTES = 40 * 1024 * 1024

_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")

# Stand-in for the `recall` CLI. Implements only the contract sync.sh
# depends on: `recall health --json --brain-root X --cwd X --write PATH`.
_RECALL_STUB = '''#!__PYTHON__
import json
import os
import sys

argv = sys.argv[1:]

# Record the exact argv so the test can pin the CLI contract.
argv_out = os.environ.get("STUB_ARGV_OUT")
if argv_out:
    with open(argv_out, "w") as fh:
        json.dump(argv, fh)


def _opt(flag):
    if flag in argv:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


write_to = _opt("--write")
if write_to:
    parent = os.path.dirname(write_to)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "schema_version": 1,
        "status": "WARN",
        "counts": {},
        "checks": [],
        "generated_at": "2026-09-04T12:00:00Z",
        "brain_root": _opt("--brain-root") or "",
        "cwd": _opt("--cwd") or "",
    }
    with open(write_to, "w") as fh:
        json.dump(payload, fh)

sys.exit(0)
'''


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _sandbox_bin(tmp_path: Path) -> Path:
    """A PATH directory holding symlinks to only the real binaries sync.sh
    needs. Keeps trufflehog / gitleaks / recall off PATH so the script takes
    the documented no-scanner and no-CLI branches deterministically."""
    d = tmp_path / "sandbox-bin"
    d.mkdir()
    for name in SANDBOX_BINARIES:
        real = shutil.which(name)
        if real:
            (d / name).symlink_to(real)
    return d


def _git(env: dict, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}:\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    return res


@pytest.fixture
def brain_repo(tmp_path: Path) -> SimpleNamespace:
    """A throwaway brain that looks like a real install: tools/, harness/
    and memory/*.py copied exactly as `install.sh --upgrade` rsyncs them,
    a git repo with a bare local remote, and a sandboxed environment."""
    home = tmp_path / "home"
    # Exists but is empty: sync.sh's `$HOME/.local/bin/recall` fallback must
    # miss, so the missing-CLI branch is reachable.
    (home / ".local" / "bin").mkdir(parents=True)
    brain = home / ".agent"
    remote = tmp_path / "remote.git"
    sandbox = _sandbox_bin(tmp_path)

    # Mirror install.sh's layout: agent/tools -> tools, agent/harness ->
    # harness, agent/memory/*.py -> memory/.
    brain.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "agent" / "tools", brain / "tools",
                    ignore=_COPY_IGNORE)
    shutil.copytree(REPO_ROOT / "agent" / "harness", brain / "harness",
                    ignore=_COPY_IGNORE)
    (brain / "memory").mkdir()
    for src in sorted((REPO_ROOT / "agent" / "memory").glob("*.py")):
        shutil.copy2(src, brain / "memory" / src.name)
    (brain / "memory" / "episodic").mkdir()
    (brain / "memory" / "notes").mkdir()
    (brain / "runtime" / "logs").mkdir(parents=True)
    (brain / "memory" / "notes" / "seed.md").write_text("seed note\n")

    # A stripped-down brain.gitignore. The machine-local files sync.sh and
    # the health write produce must be ignored, or "no changes" would never
    # be reachable on a second run.
    (brain / ".gitignore").write_text(
        "\n".join([
            "*.log",
            "*.lock",
            ".brain.lock",
            "*.tmp",
            "__pycache__/",
            "*.pyc",
            "PENDING_REVIEW.md",
            "runtime/health.json",
        ]) + "\n"
    )

    recall_stub = tmp_path / "recall-stub" / "recall"
    recall_stub.parent.mkdir()
    recall_stub.write_text(_RECALL_STUB.replace("__PYTHON__", sys.executable))
    recall_stub.chmod(0o755)

    env = {
        "HOME": str(home),
        "PATH": str(sandbox),
        "BRAIN_ROOT": str(brain),
        # No trufflehog/gitleaks in the sandbox; authorise the run instead
        # of asserting against the fail-closed branch.
        "SYNC_ALLOW_NO_SCANNER": "1",
        "PYTHON_BIN": sys.executable,
        "RECALL_BIN": str(recall_stub),
        "STUB_ARGV_OUT": str(tmp_path / "recall_argv.json"),
        # Deliberately no GIT_AUTHOR_* / GIT_COMMITTER_*: the identity comes
        # from the brain's own .git/config, exactly as it does for the
        # launchd job. An ambient env identity would hide a sync.sh that
        # cannot commit on a real machine.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }

    _git(env, tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(env, brain, "init", "-b", "main")
    _git(env, brain, "config", "user.name", "Sync Test")
    _git(env, brain, "config", "user.email", "sync@test.invalid")
    _git(env, brain, "remote", "add", "origin", str(remote))
    _git(env, brain, "add", "-A")
    _git(env, brain, "commit", "-q", "-m", "seed")
    _git(env, brain, "push", "-q", "-u", "origin", "main")

    return SimpleNamespace(
        home=home,
        brain=brain,
        remote=remote,
        sandbox=sandbox,
        env=env,
        log=brain / "sync.log",
        health=brain / "runtime" / "health.json",
        pending=brain / "PENDING_REVIEW.md",
        recall_stub=recall_stub,
        stub_argv=tmp_path / "recall_argv.json",
        bash=shutil.which("bash") or "/bin/bash",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_sync(repo: SimpleNamespace, *, drop_env: tuple[str, ...] = (),
              env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(repo.env)
    for key in drop_env:
        env.pop(key, None)
    env.update(env_overrides or {})
    return subprocess.run(
        [str(repo.bash), str(repo.brain / "tools" / "sync.sh")],
        env=env, capture_output=True, text=True, timeout=180,
    )


def _log_lines(repo: SimpleNamespace) -> list[str]:
    if not repo.log.is_file():
        return []
    return repo.log.read_text(errors="replace").splitlines()


def _debug(res: subprocess.CompletedProcess, repo: SimpleNamespace) -> str:
    return (
        f"sync.sh rc={res.returncode}\n"
        f"--- stdout ---\n{res.stdout}\n"
        f"--- stderr ---\n{res.stderr}\n"
        f"--- sync.log ---\n" + "\n".join(_log_lines(repo))
    )


def _write_sparse(path: Path, size: int = OVERSIZE_BYTES) -> None:
    """A sparse file of *exactly* `size` bytes. `wc -c` reports the full
    length while only the hole is allocated, so boundary cases cost no real
    disk and the module stays fast."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    assert path.stat().st_size == size, "sparse fixture is the wrong size"


# ---------------------------------------------------------------------------
# R3 — size gate
# ---------------------------------------------------------------------------


def test_oversize_file_is_held_back_and_rest_pushes(brain_repo):
    """One 51 MB file must not cost the user every other memory in the
    same run. The oversize path is unstaged; the rest of the tree pushes."""
    _write_sparse(brain_repo.brain / "memory" / "big.bin")
    (brain_repo.brain / "memory" / "notes" / "fresh.md").write_text("fresh\n")

    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)

    pushed = _git(
        brain_repo.env, brain_repo.remote,
        "ls-tree", "-r", "--name-only", "HEAD",
    ).stdout.split()
    assert "memory/notes/fresh.md" in pushed, (
        "the size gate held back the whole commit instead of one file; "
        f"remote tree:\n{pushed}\n\n{_debug(res, brain_repo)}"
    )
    assert "memory/big.bin" not in pushed, (
        "the 51 MB file was pushed; GitHub rejects blobs over 100 MB and "
        "the next roll would block every sync again"
    )

    # Held back, not deleted: the file stays dirty in the working tree so a
    # later run re-checks it (same contract as the quarantine path).
    assert (brain_repo.brain / "memory" / "big.bin").is_file()
    tracked = subprocess.run(
        ["git", "ls-files", "--", "memory/big.bin"],
        cwd=str(brain_repo.brain), env=brain_repo.env,
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert tracked == "", (
        f"memory/big.bin is still tracked in the index: {tracked!r}"
    )


def test_oversize_markers_in_sync_log(brain_repo):
    """The two marker strings are a frozen interface: `recall health` and
    `render_pending_summary` both parse them out of sync.log."""
    _write_sparse(brain_repo.brain / "memory" / "big.bin")
    (brain_repo.brain / "memory" / "notes" / "fresh.md").write_text("fresh\n")

    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    # Exact byte counts on both sides of the comparison, so the line is
    # unambiguous no matter how close to the limit the file sits.
    per_file = (f"sync: oversize (not pushed): memory/big.bin "
                f"({OVERSIZE_BYTES} bytes > {SIZE_LIMIT_BYTES}-byte limit)")
    summary = "sync: held back 1 oversize file(s) (>50 MB); syncing the rest"
    assert any(per_file in ln for ln in lines), (
        f"missing per-file marker {per_file!r}\n{_debug(res, brain_repo)}"
    )
    assert any(summary in ln for ln in lines), (
        f"missing summary marker {summary!r}\n{_debug(res, brain_repo)}"
    )

    # The run still ends on its own terminal marker, so the status
    # classifier reports a successful (partial) sync rather than a failure.
    sync_lines = [ln for ln in lines if "sync:" in ln]
    assert "sync: pushed" in sync_lines[-1], (
        f"last sync: line should be the push marker, got {sync_lines[-1]!r}"
    )


def test_size_gate_boundary_at_50mib_and_one_byte_over(brain_repo):
    """Exactly on the limit syncs; one byte over does not.

    Both files go through a single sync run, so this also proves the gate
    discriminates *within* a commit rather than tripping on the whole
    index. The threshold is `SYNC_MAX_FILE_BYTES:-52428800` and the
    comparison is strictly greater-than, which is the only reading that
    keeps a file the user was already syncing from suddenly stalling.
    """
    at_limit = brain_repo.brain / "memory" / "at_limit.bin"
    over_limit = brain_repo.brain / "memory" / "over_limit.bin"
    _write_sparse(at_limit, SIZE_LIMIT_BYTES)
    _write_sparse(over_limit, SIZE_LIMIT_BYTES + 1)
    (brain_repo.brain / "memory" / "notes" / "fresh.md").write_text("fresh\n")

    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)

    pushed = _git(
        brain_repo.env, brain_repo.remote,
        "ls-tree", "-r", "--name-only", "HEAD",
    ).stdout.split()
    assert "memory/at_limit.bin" in pushed, (
        f"a file of exactly {SIZE_LIMIT_BYTES} bytes is at the limit, not "
        f"over it, and must still sync; remote tree:\n{pushed}\n\n"
        f"{_debug(res, brain_repo)}"
    )
    assert "memory/over_limit.bin" not in pushed, (
        f"{SIZE_LIMIT_BYTES + 1} bytes is over the limit and must be held "
        f"back; remote tree:\n{pushed}\n\n{_debug(res, brain_repo)}"
    )
    assert "memory/notes/fresh.md" in pushed, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    # Exact bytes, not rounded megabytes. A floor-divided MB figure would
    # render this very case as "50 MB > 50 MB", which reads like a bug to
    # anyone tailing sync.log. The format is frozen: render_pending_summary
    # and `recall health` both parse these lines.
    per_file = (f"sync: oversize (not pushed): memory/over_limit.bin "
                f"({SIZE_LIMIT_BYTES + 1} bytes > {SIZE_LIMIT_BYTES}-byte "
                f"limit)")
    assert any(per_file in ln for ln in lines), (
        f"missing boundary marker {per_file!r}\n{_debug(res, brain_repo)}"
    )
    assert not any("at_limit.bin" in ln and "oversize" in ln for ln in lines), (
        f"the at-limit file was reported as oversize\n{_debug(res, brain_repo)}"
    )

    summary = "sync: held back 1 oversize file(s) (>50 MB); syncing the rest"
    assert any(summary in ln for ln in lines), (
        f"expected a count of exactly 1 held-back file (the at-limit file "
        f"must not be counted)\n{_debug(res, brain_repo)}"
    )


def test_rename_plus_oversize_restores_rename_source(brain_repo):
    """A tracked file that is renamed AND grown past the size limit in the
    same run must not lose its old path off the remote.

    `git diff --cached --name-only` for a staged rename reports only the
    new path (the paired deletion of the old path is a separate index
    entry). Resetting just the new (oversize) path out of this commit
    leaves that old-path deletion staged — so the commit would delete the
    memory from its old location on the remote while ALSO withholding the
    new, oversized content: the memory is lost outright, not just delayed.
    The fix mirrors the secret-quarantine gate above it: restore the
    rename's source alongside the oversize destination, so a rename+
    oversize is a clean no-op and the old path stays on the remote,
    unchanged.
    """
    old_path = brain_repo.brain / "memory" / "notes" / "renamed.md"
    _write_sparse(old_path, RENAME_BASE_BYTES)

    # Commit + push the tracked file on its own first, so it has a history
    # on the remote to lose.
    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)
    pushed_before = _git(
        brain_repo.env, brain_repo.remote,
        "ls-tree", "-r", "--name-only", "HEAD",
    ).stdout.split()
    assert "memory/notes/renamed.md" in pushed_before, _debug(res, brain_repo)

    # Rename it, then grow the new path past the size limit. Growing it in
    # place (rather than appending) still leaves it a large all-zero sparse
    # file like the "before" one, which is what keeps git's own rename
    # detection (see RENAME_BASE_BYTES) recognising the pair.
    _git(brain_repo.env, brain_repo.brain, "mv",
         "memory/notes/renamed.md", "memory/notes/renamed_big.md")
    new_path = brain_repo.brain / "memory" / "notes" / "renamed_big.md"
    _write_sparse(new_path, OVERSIZE_BYTES)

    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    per_file = (f"sync: oversize (not pushed): memory/notes/renamed_big.md "
                f"({OVERSIZE_BYTES} bytes > {SIZE_LIMIT_BYTES}-byte limit)")
    assert any(per_file in ln for ln in lines), (
        f"missing oversize marker for the renamed destination\n"
        f"{_debug(res, brain_repo)}"
    )

    pushed_after = _git(
        brain_repo.env, brain_repo.remote,
        "ls-tree", "-r", "--name-only", "HEAD",
    ).stdout.split()
    assert "memory/notes/renamed.md" in pushed_after, (
        "the rename source was not restored: the old path was deleted from "
        "the remote while the oversized new path was withheld, so the "
        f"memory was lost outright; remote tree:\n{pushed_after}\n\n"
        f"{_debug(res, brain_repo)}"
    )
    assert "memory/notes/renamed_big.md" not in pushed_after, (
        "the 51 MB renamed destination was pushed despite the size gate; "
        f"remote tree:\n{pushed_after}\n\n{_debug(res, brain_repo)}"
    )

    # The old path on the remote is exactly the blob that was pushed
    # before the rename — untouched, not a truncated or empty stand-in.
    old_size = _git(
        brain_repo.env, brain_repo.remote,
        "cat-file", "-s", "HEAD:memory/notes/renamed.md",
    ).stdout.strip()
    assert old_size == str(RENAME_BASE_BYTES), (
        f"remote old-path size changed unexpectedly: {old_size!r}\n"
        f"{_debug(res, brain_repo)}"
    )

    # The new path stays dirty/untracked so a later run (once it shrinks,
    # or a human untracks it) re-checks the rename automatically — same
    # contract as the plain oversize case and the secret-quarantine path.
    tracked_new = subprocess.run(
        ["git", "ls-files", "--", "memory/notes/renamed_big.md"],
        cwd=str(brain_repo.brain), env=brain_repo.env,
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert tracked_new == "", (
        f"memory/notes/renamed_big.md is still tracked in the index: "
        f"{tracked_new!r}"
    )


# ---------------------------------------------------------------------------
# R3 — health write on every exit path
# ---------------------------------------------------------------------------


def test_health_json_written_via_recall_stub(brain_repo):
    """sync.sh drives `recall health --write` and logs it under the
    `health:` prefix — never `sync:`, which the status classifier owns."""
    (brain_repo.brain / "memory" / "notes" / "fresh.md").write_text("fresh\n")

    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)

    assert brain_repo.health.is_file(), (
        f"runtime/health.json missing after a successful sync\n"
        f"{_debug(res, brain_repo)}"
    )
    payload = json.loads(brain_repo.health.read_text())
    assert payload["schema_version"] == 1
    assert payload["status"] == "WARN"
    assert payload["brain_root"] == str(brain_repo.brain)
    assert payload["cwd"] == str(brain_repo.brain)

    argv = json.loads(brain_repo.stub_argv.read_text())
    assert argv[0] == "health", f"expected the `health` subcommand, got {argv}"
    assert "--json" in argv, argv
    assert argv[argv.index("--brain-root") + 1] == str(brain_repo.brain)
    assert argv[argv.index("--cwd") + 1] == str(brain_repo.brain)
    assert argv[argv.index("--write") + 1] == str(brain_repo.health)

    lines = _log_lines(brain_repo)
    assert any("health: wrote runtime/health.json" in ln for ln in lines), (
        _debug(res, brain_repo)
    )

    health_lines = [ln for ln in lines if "health:" in ln]
    assert health_lines
    for ln in health_lines:
        assert "sync:" not in ln, (
            "a health line carries the `sync:` prefix; it would become the "
            f"last classified line and mask the run's real status: {ln!r}"
        )

    sync_lines = [ln for ln in lines if "sync:" in ln]
    assert "sync: pushed" in sync_lines[-1], (
        "the health write must be appended AFTER the terminal marker "
        f"without disturbing it; got {sync_lines[-1]!r}"
    )


def test_missing_recall_cli_logged_with_health_prefix_not_fatal(brain_repo):
    """No `recall` on PATH, none at ~/.local/bin: the health write is
    skipped with one log line and the sync's own exit code is unchanged."""
    res = _run_sync(brain_repo, drop_env=("RECALL_BIN",))
    assert res.returncode == 0, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    assert any("health: recall CLI not found; skipped" in ln for ln in lines), (
        _debug(res, brain_repo)
    )
    assert not brain_repo.health.exists(), (
        "no recall CLI, so no health report should have been written"
    )
    assert any("sync: no changes" in ln for ln in lines), (
        _debug(res, brain_repo)
    )
    for ln in [x for x in lines if "health:" in x]:
        assert "sync:" not in ln, ln


def test_no_changes_exit_zero_still_writes_health(brain_repo):
    """The most common run is a no-op. If health only refreshed on a push,
    the report would go stale on an idle machine and the staleness check
    would fire for the wrong reason."""
    res = _run_sync(brain_repo)
    assert res.returncode == 0, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    assert any("sync: no changes" in ln for ln in lines), (
        _debug(res, brain_repo)
    )
    assert brain_repo.health.is_file(), (
        f"runtime/health.json missing after a no-op run\n"
        f"{_debug(res, brain_repo)}"
    )


def test_health_and_pending_refresh_run_on_error_exit(brain_repo):
    """A rejected commit is exactly when the surfaces must be fresh. The
    EXIT trap has to fire on the failure path too."""
    hook = brain_repo.brain / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'pre-commit: refusing' >&2\nexit 1\n")
    hook.chmod(0o755)
    (brain_repo.brain / "memory" / "notes" / "fresh.md").write_text("fresh\n")

    res = _run_sync(brain_repo)
    assert res.returncode == 1, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    assert any("sync: commit blocked" in ln for ln in lines), (
        _debug(res, brain_repo)
    )
    assert brain_repo.health.is_file(), (
        f"runtime/health.json missing after a blocked commit\n"
        f"{_debug(res, brain_repo)}"
    )

    assert brain_repo.pending.is_file(), _debug(res, brain_repo)
    # The pre-sync refresh saw an empty sync.log ("sync never ran"). Only a
    # refresh that ran AFTER the terminal marker can render the pre-commit
    # branch, so this string proves the post-sync refresh happened.
    assert "pre-commit" in brain_repo.pending.read_text().lower(), (
        "PENDING_REVIEW.md still reflects the pre-sync state; the exit-path "
        f"refresh did not run\n{brain_repo.pending.read_text()}"
    )


# ---------------------------------------------------------------------------
# R3 — the push error stays quotable
# ---------------------------------------------------------------------------


def test_push_failure_leaves_git_error_before_marker(brain_repo):
    """`recall health` quotes the last remote error out of sync.log by
    scanning back from the `push failed` marker. Nothing (least of all the
    health write) may be logged in between."""
    (brain_repo.brain / "memory" / "notes" / "fresh.md").write_text("fresh\n")
    shutil.rmtree(brain_repo.remote)

    res = _run_sync(brain_repo)
    assert res.returncode == 1, _debug(res, brain_repo)

    lines = _log_lines(brain_repo)
    marker = "sync: commit succeeded but push failed"
    idx = next((i for i, ln in enumerate(lines) if marker in ln), None)
    assert idx is not None, _debug(res, brain_repo)

    before = [ln for ln in lines[:idx] if ln.strip()]
    assert before, (
        f"git wrote no error before the push-failed marker\n"
        f"{_debug(res, brain_repo)}"
    )
    assert "sync:" not in before[-1], (
        "something was logged between git's error and the push-failed "
        f"marker: {before[-1]!r}\n{_debug(res, brain_repo)}"
    )
    assert any("fatal" in ln.lower() or "error" in ln.lower()
               for ln in before[-10:]), (
        "git's push error is not in sync.log; there is nothing for "
        f"`recall health` to quote\n{_debug(res, brain_repo)}"
    )
