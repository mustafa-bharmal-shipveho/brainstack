"""`bin/install-recall-cli.sh` must regenerate a stale console-script wrapper.

The `recall` entry point moved from `recall.cli:app` to `recall.cli:main`
(deterministic exit, v0.7.0). pip writes the wrapper once; the helper then
skipped `pip install -e` whenever `.venv/bin/recall` already existed, so
every EXISTING installation kept a wrapper that imports `app` directly and
bypasses `main()`/`hard_exit` — the fix shipped only to fresh installs
(Codex review, pass 4).

Driven against a fake venv: a recording `pip` that rewrites the wrapper the
way the real one would, so the test proves the decision, not pip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "bin" / "install-recall-cli.sh"

OLD_WRAPPER = (
    "#!/usr/bin/python3\nimport re, sys\nfrom recall.cli import app\n"
    "if __name__ == '__main__':\n    sys.exit(app())\n"
)
NEW_WRAPPER = (
    "#!/usr/bin/python3\nimport re, sys\nfrom recall.cli import main\n"
    "if __name__ == '__main__':\n    sys.exit(main())\n"
)


@pytest.fixture
def fake_repo(tmp_path: Path):
    """A copy of the helper inside a fake repo dir, with a fake venv whose
    `pip` records its argv and regenerates the wrapper."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    shutil.copy(HELPER, repo / "bin" / "install-recall-cli.sh")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    calls = tmp_path / "pip-calls.log"
    (venv_bin / "pip").write_text(
        "#!/bin/sh\n"
        f'echo "pip $*" >> "{calls}"\n'
        "case \"$*\" in *install*-e*) cat > \"$(dirname \"$0\")/recall\" <<'W'\n"
        + NEW_WRAPPER + "W\nchmod +x \"$(dirname \"$0\")/recall\";; esac\nexit 0\n"
    )
    (venv_bin / "pip").chmod(0o755)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
    return {"repo": repo, "wrapper": venv_bin / "recall", "calls": calls, "env": env}


def _run(fr) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(fr["repo"] / "bin" / "install-recall-cli.sh"), "--quiet"],
        env=fr["env"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


def _pip_calls(fr) -> str:
    return fr["calls"].read_text() if fr["calls"].exists() else ""


def test_stale_wrapper_is_regenerated(fake_repo):
    fake_repo["wrapper"].write_text(OLD_WRAPPER)
    fake_repo["wrapper"].chmod(0o755)

    r = _run(fake_repo)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "install" in _pip_calls(fake_repo) and "-e" in _pip_calls(fake_repo), _pip_calls(fake_repo)
    assert "from recall.cli import main" in fake_repo["wrapper"].read_text()


def test_current_wrapper_is_left_alone(fake_repo):
    fake_repo["wrapper"].write_text(NEW_WRAPPER)
    fake_repo["wrapper"].chmod(0o755)

    r = _run(fake_repo)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _pip_calls(fake_repo) == "", "no wrapper regeneration was needed"


def test_missing_wrapper_still_installs_with_extras(fake_repo):
    r = _run(fake_repo)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "[embeddings,mcp]" in _pip_calls(fake_repo), _pip_calls(fake_repo)
    assert fake_repo["wrapper"].exists()
