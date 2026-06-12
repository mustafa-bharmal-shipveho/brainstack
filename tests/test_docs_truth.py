"""Doc-truth tests: the README / CONTRIBUTING / installer help must describe
what the code actually does, with no stale claims or broken links.

Written RED-first for the adoption-audit docs/installer rewrite on this
branch (feat/adoption-audit-fixes). Each test pins a POST-rewrite contract:

  - Quickstart uses the canonical clone URL, not a `<your-org>` placeholder.
  - The "installer never touches ~/.claude/settings.json" claim is gone
    (the default install wires auto-recall; `--no-auto-recall` opts out).
  - The README's fastembed cache path matches the code's actual default.
  - `./install.sh --help` exits 0, documents `--minimal` and `--dry-run`,
    and is side-effect-free (a tmp HOME gains no files).
  - Every relative markdown link in CONTRIBUTING.md and README.md resolves
    (catches the missing CODE_OF_CONDUCT.md, and pins future root-file
    moves like STATUS.md / HALT.md to README link updates).
  - No em-dashes in the README (project style rule).
  - Quickstart ordering: `--minimal` install appears before `--brain-remote`,
    and ROADMAP.md exists and is linked.

All tests are hermetic: no network, no writes outside tmp_path, no model
downloads (recall.qdrant_backend is imported but no embedder is built).
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
INSTALL_SH = REPO_ROOT / "install.sh"

CANONICAL_CLONE_URL = (
    "git clone https://github.com/mustafa-bharmal-shipveho/brainstack.git"
)

# [text](target) — target captured up to the first `)` or whitespace.
# Optional leading `!` so image links are covered too.
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _relative_link_targets(markdown_path: Path) -> list[str]:
    """All relative markdown link targets in the file.

    Skips absolute URLs (http/https/mailto-style schemes) and pure
    in-page anchors (#...). Fragments are stripped from relative targets
    so `docs/foo.md#section` checks `docs/foo.md`.
    """
    text = markdown_path.read_text()
    targets: list[str] = []
    for match in _MD_LINK_RE.finditer(text):
        target = match.group(1)
        if target.startswith("#") or target.startswith("http"):
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):  # mailto:, ssh:, …
            continue
        target = target.split("#", 1)[0]
        if target:
            targets.append(target)
    return targets


def _missing_link_targets(markdown_path: Path) -> list[str]:
    return [
        target
        for target in _relative_link_targets(markdown_path)
        if not (REPO_ROOT / target).exists()
    ]


def _files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# ---------------------------------------------------------------------------
# Quickstart / clone URL
# ---------------------------------------------------------------------------


def test_quickstart_has_canonical_clone_url():
    text = README.read_text()
    assert CANONICAL_CLONE_URL in text, (
        f"README.md quickstart must use the canonical clone URL "
        f"({CANONICAL_CLONE_URL!r}); placeholders don't copy-paste."
    )
    assert "<your-org>" not in text, (
        "README.md still contains the '<your-org>' placeholder — replace it "
        "with the canonical repo URL."
    )


# ---------------------------------------------------------------------------
# Stale settings.json safety claim
# ---------------------------------------------------------------------------


def test_no_stale_settings_json_safety_claim():
    readme = README.read_text()
    installer = INSTALL_SH.read_text()

    assert "does not edit `~/.claude/settings.json`" not in readme, (
        "README.md still claims install.sh does not edit "
        "~/.claude/settings.json — the default install wires auto-recall "
        "hooks, so this claim is stale."
    )
    assert "never auto-edits user settings" not in installer, (
        "install.sh header still claims it 'never auto-edits user settings' "
        "— stale since the auto-recall default landed."
    )
    assert "--no-auto-recall" in readme, (
        "README.md must document --no-auto-recall as the opt-out for the "
        "settings.json auto-recall wiring."
    )


# ---------------------------------------------------------------------------
# fastembed cache path: README claim == code default
# ---------------------------------------------------------------------------


def test_fastembed_cache_path_matches_code(monkeypatch):
    # The code default must be computed with XDG_CACHE_HOME unset, so the
    # claim is about the true out-of-the-box path.
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    qb = importlib.import_module("recall.qdrant_backend")
    cache_attr = getattr(qb, "_fastembed_cache_dir", None)
    if cache_attr is None:
        cache_attr = getattr(qb, "FASTEMBED_CACHE_DIR", None)
    assert cache_attr is not None, (
        "recall.qdrant_backend must expose the fastembed cache location as "
        "either a _fastembed_cache_dir() function or a FASTEMBED_CACHE_DIR "
        "constant, so docs can be checked against the real default."
    )

    raw = cache_attr() if callable(cache_attr) else cache_attr
    resolved = Path(raw).expanduser()
    claimed = str(resolved).replace(str(Path.home()), "~")

    readme = README.read_text()
    assert "~/.cache/fastembed" in readme, (
        "README.md must document the fastembed model cache path "
        "(~/.cache/fastembed)."
    )
    assert str(resolved).rstrip("/").endswith("fastembed"), (
        f"code default fastembed cache dir is {resolved} (claimed: "
        f"{claimed}); expected the default (XDG_CACHE_HOME unset) to end "
        "with 'fastembed' to match the README claim."
    )


# ---------------------------------------------------------------------------
# install.sh --help
# ---------------------------------------------------------------------------


def test_install_help_documents_minimal_and_dry_run(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    before = _files_under(home)

    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BRAINSTACK_SKIP_LAUNCHCTL": "1",
    }
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"./install.sh --help must exit 0; got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )
    assert "--minimal" in result.stdout, (
        "--help output must document the --minimal install mode.\n"
        f"stdout was:\n{result.stdout}"
    )
    assert "--dry-run" in result.stdout, (
        "--help output must document --dry-run.\n"
        f"stdout was:\n{result.stdout}"
    )

    after = _files_under(home)
    assert after == before, (
        "--help must be side-effect-free; tmp HOME gained: "
        f"{sorted(after - before)}"
    )


# ---------------------------------------------------------------------------
# Relative markdown links resolve
# ---------------------------------------------------------------------------


def test_contributing_coc_link_resolves():
    missing = _missing_link_targets(CONTRIBUTING)
    assert not missing, (
        "CONTRIBUTING.md links to files that don't exist relative to the "
        f"repo root: {missing} (CODE_OF_CONDUCT.md must ship with the repo "
        "if it's linked)."
    )


def test_readme_relative_links_resolve():
    missing = _missing_link_targets(README)
    assert not missing, (
        "README.md links to files that don't exist relative to the repo "
        f"root: {missing} (if root files like STATUS.md / HALT.md moved, "
        "update the README links)."
    )


# ---------------------------------------------------------------------------
# Style: no em-dashes
# ---------------------------------------------------------------------------


def test_readme_has_no_em_dash():
    text = README.read_text()
    lines_with_em_dash = [
        f"  line {i}: {line.strip()!r}"
        for i, line in enumerate(text.splitlines(), start=1)
        if "—" in line
    ]
    assert "—" not in text, (
        "README.md contains em-dashes (project style rule forbids them):\n"
        + "\n".join(lines_with_em_dash)
    )


# ---------------------------------------------------------------------------
# Quickstart ordering: minimal install first, roadmap linked
# ---------------------------------------------------------------------------


def test_readme_mentions_minimal_install_first():
    text = README.read_text()

    minimal_idx = text.find("./install.sh --minimal")
    remote_idx = text.find("./install.sh --brain-remote")
    assert minimal_idx != -1, (
        "README.md must show './install.sh --minimal' as a quickstart path."
    )
    assert remote_idx != -1, (
        "README.md must still document './install.sh --brain-remote'."
    )
    assert minimal_idx < remote_idx, (
        "Quickstart ordering contract: the first './install.sh --minimal' "
        f"(index {minimal_idx}) must appear BEFORE the first "
        f"'./install.sh --brain-remote' (index {remote_idx}) in README.md."
    )

    roadmap = REPO_ROOT / "ROADMAP.md"
    assert roadmap.is_file(), "ROADMAP.md must exist at the repo root."
    assert "ROADMAP.md" in _relative_link_targets(README), (
        "README.md must link to ROADMAP.md (relative markdown link)."
    )
