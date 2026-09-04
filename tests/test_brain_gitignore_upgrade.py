"""`templates/brain.gitignore` + the idempotent `--upgrade` append — S5
slice C, requirement R4.

The live brain has a 107 MB `memory/episodic/codex/AGENT_LEARNINGS.jsonl`
tracked in git. GitHub refuses blobs over 100 MB, so every push has been
rejected since 2026-08-30. Rotation (slice B) bounds the file going
forward, but the rolled siblings it produces
(`AGENT_LEARNINGS.2026-09-04.jsonl`) would be tracked too unless the
template ignores them.

So `templates/brain.gitignore` gains a managed block, and `install.sh`
grows one function that appends any template rule missing from a live
`.gitignore`. Three properties matter and are pinned here:

  - **Nothing is removed.** A rule the user added by hand survives every
    upgrade. The function only ever appends.
  - **Nothing is duplicated.** Running `--upgrade` twice leaves the file
    byte-identical, so the brain does not accumulate a growing tail of
    repeated blocks.
  - **The globs actually match the rolled names.** A rule that reads
    plausibly but misses `AGENT_LEARNINGS.2026-09-04.1.jsonl` would let
    the same failure back in silently. `git check-ignore` decides, not a
    string comparison.

Subprocess-level against the real `install.sh`, isolated with a tmp HOME
plus BRAINSTACK_SKIP_LAUNCHCTL=1 / BRAINSTACK_SKIP_CLI_INSTALL=1 (same
harness as tests/test_install_hardening.py).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
TEMPLATE = REPO_ROOT / "templates" / "brain.gitignore"

# The managed block from the S5 plan. Rolled episodic files and rolled
# event logs match the `*` globs; the three runtime status files are
# machine-local and must never reach the remote.
REQUIRED_RULES = (
    "memory/episodic/AGENT_LEARNINGS*.jsonl",
    "memory/episodic/**/AGENT_LEARNINGS*.jsonl",
    "memory/episodic/**/_imported.jsonl*",
    "*.preCodexFix.bak",
    "runtime/logs/*.jsonl",
    "runtime/logs/injected/",
    "runtime/health.json",
    "runtime/dream_status.json",
    "runtime/recall.sock",
)


def _fresh_env(fake_home: Path) -> dict:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    env["GIT_AUTHOR_NAME"] = "Gitignore"
    env["GIT_AUTHOR_EMAIL"] = "gitignore@test.invalid"
    env["GIT_COMMITTER_NAME"] = "Gitignore"
    env["GIT_COMMITTER_EMAIL"] = "gitignore@test.invalid"
    return env


def _run(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False,
        stdin=subprocess.DEVNULL, timeout=180,
    )


def _rules(text: str) -> list[str]:
    """Non-blank, non-comment lines — the rules a .gitignore actually
    applies. Comments are documentation and are not copied on upgrade."""
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# ---------------------------------------------------------------------------
# The template is the single source of rules
# ---------------------------------------------------------------------------


def test_template_contains_required_rules():
    rules = _rules(TEMPLATE.read_text())
    missing = [r for r in REQUIRED_RULES if r not in rules]
    assert not missing, (
        f"templates/brain.gitignore is missing {missing}. Without these the "
        f"rolled episodic files and machine-local runtime status files get "
        f"tracked, which is what broke the push in the first place."
    )


# ---------------------------------------------------------------------------
# --upgrade appends what is missing, keeps what the user wrote
# ---------------------------------------------------------------------------


def test_upgrade_appends_missing_rules_and_keeps_user_rules(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    brain = Path(env["BRAIN_ROOT"])
    brain.mkdir(parents=True)

    # A live .gitignore as it exists on a brain installed before S5: one
    # hand-written user rule, a couple of template rules already present,
    # and every new rule absent.
    (brain / ".gitignore").write_text(
        "# my own rules\n"
        "my-private-notes/\n"
        "*.log\n"
        "PENDING_REVIEW.md\n"
    )

    res = _run("--upgrade", env=env)
    assert res.returncode == 0, (
        f"--upgrade failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )

    text = (brain / ".gitignore").read_text()
    rules = _rules(text)

    assert "my-private-notes/" in rules, (
        "the upgrade dropped a hand-written user rule; the append must "
        f"never remove anything. Result:\n{text}"
    )
    for rule in REQUIRED_RULES:
        assert rules.count(rule) == 1, (
            f"expected {rule!r} exactly once, found {rules.count(rule)}. "
            f"Result:\n{text}"
        )
    # Rules that were already present must not be appended a second time.
    for already in ("*.log", "PENDING_REVIEW.md"):
        assert rules.count(already) == 1, (
            f"{already!r} was duplicated by the upgrade. Result:\n{text}"
        )

    header = [ln for ln in text.splitlines()
              if ln.strip().startswith("#")
              and "brainstack" in ln.lower() and "upgrade" in ln.lower()]
    assert header, (
        "appended rules must sit under a header naming the upgrade, so a "
        f"user reading their .gitignore knows what added them. Result:\n{text}"
    )


def test_upgrade_second_run_is_noop(tmp_path: Path):
    """Idempotence is the whole point: `--upgrade` runs on every release,
    and an append that re-fires would grow the file without bound."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    brain = Path(env["BRAIN_ROOT"])
    brain.mkdir(parents=True)
    (brain / ".gitignore").write_text("# my own rules\nmy-private-notes/\n")

    first = _run("--upgrade", env=env)
    assert first.returncode == 0, first.stderr
    after_first = (brain / ".gitignore").read_bytes()

    second = _run("--upgrade", env=env)
    assert second.returncode == 0, second.stderr
    after_second = (brain / ".gitignore").read_bytes()

    assert after_first == after_second, (
        "the second --upgrade changed .gitignore; the append is not "
        "idempotent.\n--- after first ---\n"
        f"{after_first.decode(errors='replace')}\n--- after second ---\n"
        f"{after_second.decode(errors='replace')}"
    )


def test_fresh_install_gitignore_matches_template_rules(tmp_path: Path):
    """A brand-new brain gets every template rule — no upgrade required."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)

    res = _run("--minimal", env=env)
    assert res.returncode == 0, (
        f"--minimal failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )

    live = Path(env["BRAIN_ROOT"]) / ".gitignore"
    assert live.is_file(), "fresh install did not create <brain>/.gitignore"

    live_rules = _rules(live.read_text())
    missing = [r for r in _rules(TEMPLATE.read_text()) if r not in live_rules]
    assert not missing, (
        f"fresh install .gitignore is missing template rules {missing}"
    )


# ---------------------------------------------------------------------------
# The globs match the names rotation actually produces
# ---------------------------------------------------------------------------


def test_rolled_names_match_gitignore_globs(tmp_path: Path):
    """`git check-ignore` is the arbiter. A rule that reads right but does
    not match `AGENT_LEARNINGS.2026-09-04.1.jsonl` puts a 100 MB blob back
    in the index on the next roll."""
    repo = tmp_path / "brain"
    repo.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "fakehome")
    (tmp_path / "fakehome").mkdir()
    # A global core.excludesFile on the developer's machine could make this
    # pass for the wrong reason.
    env["GIT_CONFIG_NOSYSTEM"] = "1"

    subprocess.run(["git", "init", "-b", "main", "."], cwd=str(repo), env=env,
                   capture_output=True, text=True, check=True, timeout=60)
    (repo / ".gitignore").write_text(TEMPLATE.read_text())

    ignored = [
        # Rotation output (slice B) for the two files that grew unbounded.
        "memory/episodic/codex/AGENT_LEARNINGS.2026-09-04.1.jsonl",
        "memory/episodic/codex/AGENT_LEARNINGS.2026-09-04.jsonl",
        "runtime/logs/events.log.2026-09-04.jsonl",
        # The current files these roll out of.
        "memory/episodic/codex/AGENT_LEARNINGS.jsonl",
        "memory/episodic/AGENT_LEARNINGS.jsonl",
        "runtime/logs/events.log.jsonl",
        # Machine-local status files written by sync.sh / the dream cycle.
        "runtime/health.json",
        "runtime/dream_status.json",
    ]
    for rel in ignored:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        res = subprocess.run(
            ["git", "check-ignore", "-q", "--", rel],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, (
            f"{rel} is NOT ignored by templates/brain.gitignore; it would be "
            f"committed and (for the big ones) rejected by GitHub"
        )

    # Negative control: the rules must not be broad enough to swallow the
    # user's actual memories.
    keep = "memory/personal/notes/hello.md"
    (repo / keep).parent.mkdir(parents=True, exist_ok=True)
    (repo / keep).write_text("hello\n")
    res = subprocess.run(
        ["git", "check-ignore", "-q", "--", keep],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 1, (
        f"{keep} is ignored — the new rules are too broad and would stop "
        f"the user's notes from syncing"
    )
