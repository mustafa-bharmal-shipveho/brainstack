#!/usr/bin/env python3
"""Detects drift between framework code in the brainstack repo and the
copy installed at $BRAIN_ROOT.

Why this exists
---------------

`install.sh` seeds `~/.agent/{tools,memory,harness}/` from the brainstack
repo at install time. After that, `git pull` of the brainstack repo does
NOT propagate those updates — only `./install.sh --upgrade` does. Users
routinely miss the upgrade step and run stale framework code for weeks
without noticing. Two real-world examples (2026-05-04):

  1. `~/.agent/tools/auto_migrate_install.py` was missing entirely on a
     brain installed before the auto-migrate feature shipped, so
     `./install.sh --setup-auto-migrate` failed with "tools/...py is
     missing" instead of working.

  2. `~/.agent/memory/auto_dream.py` was the v0.1 version (no namespace
     support) on a brain installed before the namespace work, so
     `dream_runner.py` only clustered the default-namespace episodic
     stream and silently ignored 36k+ episodes from codex and
     claude-sessions namespaces.

Both failure modes are silent — runtime tools just behave as if the
feature isn't there. This module makes drift loud.

Usage
-----

    check_freshness.py [--repo REPO_DIR] [--brain BRAIN_ROOT]
                       [--quiet] [--json]

Exit codes:
    0  no drift — runtime is in sync with the repo
    1  drift detected — at least one file differs
    2  cannot run (repo or brain not found)

Quiet mode prints nothing on success and a single one-line summary on
drift, suitable for embedding in launchd output where a quiet brain is
the goal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional


# (repo subdir, brain subdir, file pattern). For memory/, the brain dir
# also holds user data (working/, episodic/, candidates/, …) so the sync
# is per-file rather than per-tree.
_TRACKED = [
    ("agent/tools",   "tools",   "*.py"),
    ("agent/tools",   "tools",   "*.sh"),
    ("agent/memory",  "memory",  "*.py"),
    ("agent/harness", "harness", "*.py"),
]


def _file_sha(path: Path) -> str:
    """SHA-256 of file contents. Empty string on read error."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _walk_pattern(root: Path, pattern: str) -> list[Path]:
    """Return sorted list of files matching `pattern` under `root`,
    excluding __pycache__ and *.user.* (the user-local sentinel)."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob(pattern)):
        if p.is_symlink() or not p.is_file():
            continue
        # Skip cache and user-local files
        if "__pycache__" in p.parts:
            continue
        if ".user." in p.name:
            continue
        out.append(p)
    return out


def detect_drift(repo_dir: Path, brain_root: Path) -> dict:
    """Compare every tracked file in `repo_dir` against `brain_root`.

    Returns a structured report:
        {
          "in_sync":     bool,
          "missing":     [str, ...]   # in repo, absent from brain
          "stale":       [str, ...]   # different content
          "extra":       [str, ...]   # in brain, not in repo (user-added or removed upstream)
          "summary":     str          # one-line human summary
        }
    """
    missing: list[str] = []
    stale: list[str] = []
    seen_brain: set[Path] = set()

    for repo_sub, brain_sub, pattern in _TRACKED:
        repo_root = repo_dir / repo_sub
        brain_dir = brain_root / brain_sub
        for repo_file in _walk_pattern(repo_root, pattern):
            rel = repo_file.relative_to(repo_root)
            brain_file = brain_dir / rel
            seen_brain.add(brain_file.resolve() if brain_file.exists() else brain_file)
            if not brain_file.is_file():
                missing.append(str(rel))
                continue
            if _file_sha(repo_file) != _file_sha(brain_file):
                stale.append(str(rel))

    # `extra` = files in brain that no longer exist in the repo. Only
    # checked under tools/ and harness/ where rsync --delete cleans up
    # during --upgrade. memory/ is intentionally excluded — user data
    # subdirs live there.
    extra: list[str] = []
    for repo_sub, brain_sub, pattern in _TRACKED:
        if brain_sub == "memory":
            continue
        repo_root = repo_dir / repo_sub
        brain_dir = brain_root / brain_sub
        for brain_file in _walk_pattern(brain_dir, pattern):
            rel = brain_file.relative_to(brain_dir)
            if not (repo_root / rel).is_file():
                extra.append(str(rel))

    # `extra` files (deleted upstream but still in the brain) DO count
    # as drift. Without this, an upstream-removed tool stays in the brain
    # forever and the CLI exits 0 — exactly the silent-stale state this
    # tool exists to surface. Codex 2026-05-04 P2.
    drift_count = len(missing) + len(stale) + len(extra)
    in_sync = drift_count == 0
    if in_sync:
        summary = "in sync"
    else:
        parts = []
        if missing:
            parts.append(f"{len(missing)} missing")
        if stale:
            parts.append(f"{len(stale)} stale")
        if extra:
            parts.append(f"{len(extra)} extra")
        summary = "drift detected — " + ", ".join(parts) + ". Run `./install.sh --upgrade` from the brainstack repo to refresh."

    return {
        "in_sync": in_sync,
        "missing": sorted(missing),
        "stale": sorted(stale),
        "extra": sorted(extra),
        "summary": summary,
    }


_REPO_PATH_PIN = ".brainstack-repo-path"


def _default_repo_dir(brain_root: Optional[Path] = None) -> Optional[Path]:
    """Find the brainstack repo dir.

    Resolution order:
      1. This file lives at <repo>/agent/tools/check_freshness.py — return
         the repo directly. Works when invoked from the repo side.
      2. Brain has `<brain>/.brainstack-repo-path` (a one-line file with
         the absolute repo path written by install.sh). Works when
         invoked from the brain side.
      3. Return None — caller must pass --repo explicitly.
    """
    here = Path(__file__).resolve()
    if here.parent.name == "tools" and here.parent.parent.name == "agent":
        candidate = here.parent.parent.parent
        if (candidate / "install.sh").is_file():
            return candidate
    if brain_root is None:
        brain_root = Path(os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")))
    pin = brain_root / _REPO_PATH_PIN
    if pin.is_file():
        try:
            path = Path(pin.read_text().strip()).expanduser()
            if path.is_dir() and (path / "install.sh").is_file():
                return path
        except OSError:
            pass
    return None


def warn_if_drift(brain_root: Optional[Path] = None,
                  repo_dir: Optional[Path] = None,
                  stream=None) -> bool:
    """Helper for runtime entry points (dream_runner, sync_claude_extras,
    auto-migrate-all). Emits one stderr line if drift is detected.

    Returns True if drift was detected, False on in-sync or unknown.
    Never raises — drift detection failure must not break the runtime.
    """
    if stream is None:
        stream = sys.stderr
    try:
        brain = brain_root or Path(os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")))
        repo = repo_dir or _default_repo_dir(brain)
        if repo is None or not brain.is_dir():
            return False  # silently skip — not enough info to check
        report = detect_drift(repo, brain)
        if report["in_sync"]:
            return False
        stream.write(f"brainstack: {report['summary']}\n")
        return True
    except Exception:
        return False


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="check_freshness")
    p.add_argument("--repo", default=None,
                   help="brainstack repo dir (default: walked from this file's path; required if invoked from the brain-side copy)")
    p.add_argument("--brain", default=os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")),
                   help="brain root (default: $BRAIN_ROOT or ~/.agent)")
    p.add_argument("--quiet", action="store_true",
                   help="silent on success; one-line warning on drift")
    p.add_argument("--json", dest="emit_json", action="store_true",
                   help="emit JSON instead of human text")
    args = p.parse_args(argv)

    brain_root = Path(args.brain).expanduser()
    repo_dir = Path(args.repo).expanduser() if args.repo else _default_repo_dir(brain_root)

    if repo_dir is None or not repo_dir.is_dir():
        sys.stderr.write(
            "check_freshness: repo dir unknown. Pass --repo /path/to/brainstack\n"
        )
        return 2
    if not brain_root.is_dir():
        sys.stderr.write(f"check_freshness: brain root not found: {brain_root}\n")
        return 2

    report = detect_drift(repo_dir, brain_root)

    if args.emit_json:
        print(json.dumps(report, indent=2))
        return 0 if report["in_sync"] else 1

    if args.quiet:
        if not report["in_sync"]:
            sys.stderr.write(f"brainstack: {report['summary']}\n")
        return 0 if report["in_sync"] else 1

    if report["in_sync"]:
        print(f"✅  brain is in sync with {repo_dir}")
        return 0

    print(f"⚠️   brain at {brain_root} is OUT OF SYNC with {repo_dir}")
    print()
    if report["missing"]:
        print(f"  Missing in brain ({len(report['missing'])}):")
        for f in report["missing"]:
            print(f"    - {f}")
    if report["stale"]:
        print(f"  Stale (content differs) ({len(report['stale'])}):")
        for f in report["stale"]:
            print(f"    ~ {f}")
    if report["extra"]:
        print(f"  Extra (in brain, not in repo) ({len(report['extra'])}):")
        for f in report["extra"]:
            print(f"    + {f}")
    print()
    print(f"  → fix: cd {repo_dir} && ./install.sh --upgrade")
    return 1


if __name__ == "__main__":
    sys.exit(main())
