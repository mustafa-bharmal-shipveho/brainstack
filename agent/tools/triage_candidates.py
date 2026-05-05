#!/usr/bin/env python3
"""Interactive REPL for triaging staged candidate lessons.

The user wanted `recall pending --review` to *actually* be interactive:
read decisions from THEIR keyboard, never auto-decide on their behalf.
Previous iterations relied on the /dream skill telling Claude to ask
per-candidate; Claude ignored that and rejected 22 candidates across
two sessions without prompting.

This tool enforces the contract IN CODE: each candidate triggers a
blocking `input()` call; the next decision can't be applied until the
user types `g`, `r`, `s`, `q`, or `e` on stdin. If stdin isn't a TTY
(e.g., Claude calls this via Bash tool with no PTY), the tool refuses
to run and prints instructions for the user to open a real terminal.

Usage
-----

    triage_candidates.py [--brain DIR] [--namespace NS]

The user runs this in their own terminal. Each iteration:
  1. One-screen summary of the next candidate
  2. Prompt `[g]raduate / [r]eject / [s]kip / [e]vidence / [q]uit:`
  3. Wait for keyboard input
  4. On g/r: ask for required rationale, then invoke graduate.py /
     reject.py (those tools enforce the rationale field, so empty
     input fails).
  5. Loop until queue is empty or user quits.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _resolve_brain(arg: Optional[str]) -> Path:
    """Honor --brain, else $BRAIN_ROOT, else ~/.agent."""
    if arg:
        return Path(arg).expanduser()
    return Path(os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")))


def _candidate_dir(brain: Path, namespace: str) -> Path:
    if namespace == "default":
        return brain / "memory" / "candidates"
    return brain / "memory" / "candidates" / namespace


def _list_candidates(candidates_dir: Path) -> list[Path]:
    """All staged *.json files at the top of candidates_dir, sorted by
    descending priority (cluster_size * salience)."""
    if not candidates_dir.is_dir():
        return []
    files: list[tuple[float, Path]] = []
    for p in candidates_dir.glob("*.json"):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") != "staged":
            continue
        cs = data.get("cluster_size", 0)
        sal = data.get("canonical_salience", 0)
        files.append((cs * sal, p))
    files.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in files]


def _print_candidate(data: dict, idx: int, total: int) -> None:
    cid = data.get("id", "?")
    claim = (data.get("claim") or "").strip()
    cs = data.get("cluster_size", "?")
    sal = data.get("canonical_salience", 0)
    eids = data.get("evidence_ids") or []
    rejs = data.get("rejection_count", 0)
    staged = data.get("staged_at", "?")[:19]

    print()
    print(f"=== Candidate {idx + 1} of {total} ===")
    print(f"  id:            {cid}")
    print(f"  priority:      {cs} cluster x {sal:.1f} salience = {cs * sal if isinstance(cs, (int, float)) else '?':.1f}")
    print(f"  claim:         {claim[:200]}")
    print(f"  staged_at:     {staged}")
    if rejs:
        print(f"  rejected:      {rejs} prior time(s)")
    if eids:
        print(f"  evidence (first 3 of {len(eids)}):")
        for e in eids[:3]:
            print(f"                 {str(e)[:80]}")


def _print_full_evidence(data: dict) -> None:
    print()
    print("--- full candidate JSON ---")
    print(json.dumps(data, indent=2))
    print("--- end ---")


def _prompt_for_text(prompt: str) -> str:
    """Read a non-empty single-line input. Loop until non-empty."""
    while True:
        try:
            text = input(prompt).strip()
        except EOFError:
            return ""
        if text:
            return text
        print("  (required, try again)")


def _apply_graduate(brain: Path, namespace: str, cid: str, rationale: str) -> bool:
    cmd = [
        sys.executable, str(brain / "tools" / "graduate.py"),
        cid,
        "--rationale", rationale,
        "--namespace", namespace,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] graduate.py {cid}")
        return False
    if result.stdout:
        print(f"  {result.stdout.rstrip()}")
    if result.returncode != 0:
        print(f"  [FAIL exit={result.returncode}] {result.stderr.rstrip()}")
        return False
    return True


def _apply_reject(brain: Path, namespace: str, cid: str, reason: str) -> bool:
    cmd = [
        sys.executable, str(brain / "tools" / "reject.py"),
        cid,
        "--reason", reason,
        "--namespace", namespace,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] reject.py {cid}")
        return False
    if result.stdout:
        print(f"  {result.stdout.rstrip()}")
    if result.returncode != 0:
        print(f"  [FAIL exit={result.returncode}] {result.stderr.rstrip()}")
        return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="triage_candidates",
                                 description=__doc__.split("\n")[0])
    p.add_argument("--brain", default=None)
    p.add_argument("--namespace", default="default",
                   help="default | claude-sessions | codex (default: default)")
    args = p.parse_args(argv)

    brain = _resolve_brain(args.brain)
    candidates_dir = _candidate_dir(brain, args.namespace)

    # CRITICAL: refuse to run if stdin isn't interactive. This is the
    # structural enforcement of "user decides per candidate" — without
    # a TTY, there's no user to prompt. If Claude is calling this via
    # the Bash tool without a PTY, we exit with a clear message rather
    # than blocking on input() (which would either hang or read an
    # empty line and treat it as "skip", neither of which is right).
    if not sys.stdin.isatty():
        sys.stderr.write(
            "triage_candidates: stdin is not a TTY. This tool requires an "
            "interactive terminal so the user can decide per candidate.\n"
            "\n"
            "If you're an AI assistant: tell the user to open their terminal\n"
            "and run `recall pending --review` themselves. Do NOT call\n"
            "graduate.py or reject.py on their behalf without per-candidate\n"
            "explicit consent.\n"
        )
        return 2

    candidates = _list_candidates(candidates_dir)
    if not candidates:
        print(f"triage: no staged candidates in {candidates_dir}")
        return 0

    print(f"triage: {len(candidates)} staged candidate(s) in namespace={args.namespace}")
    print(f"        commands: g=graduate r=reject s=skip e=evidence q=quit")
    print(f"        each decision requires an explicit keyboard input.")

    decisions = {"graduated": 0, "rejected": 0, "skipped": 0}
    quit_requested = False

    for idx, path in enumerate(candidates):
        if quit_requested:
            break
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN can't parse {path}: {e}")
            continue

        cid = data.get("id", path.stem)
        # Loop until a terminal decision (g/r/s/q) — `e` re-prompts
        while True:
            _print_candidate(data, idx, len(candidates))
            try:
                choice = input("\n  [g]raduate  [r]eject  [s]kip  [e]vidence  [q]uit: ").strip().lower()
            except EOFError:
                print("\n(eof — quitting)")
                quit_requested = True
                break

            if choice in ("q", "quit", "exit"):
                quit_requested = True
                break

            if choice in ("s", "skip", ""):
                decisions["skipped"] += 1
                break

            if choice in ("e", "evidence"):
                _print_full_evidence(data)
                # Re-prompt — `e` is non-terminal
                continue

            if choice in ("g", "graduate"):
                rationale = _prompt_for_text("  rationale (required): ")
                if _apply_graduate(brain, args.namespace, cid, rationale):
                    decisions["graduated"] += 1
                break

            if choice in ("r", "reject"):
                reason = _prompt_for_text("  reason (required): ")
                if _apply_reject(brain, args.namespace, cid, reason):
                    decisions["rejected"] += 1
                break

            print(f"  unknown choice: {choice!r}. Try g / r / s / e / q.")

    print()
    print(f"triage: graduated={decisions['graduated']} "
          f"rejected={decisions['rejected']} skipped={decisions['skipped']}"
          + (" (quit early)" if quit_requested else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
