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
  4. On g/r: invoke graduate.py / reject.py with an auto-generated
     rationale ("graduated via interactive triage"). The keypress IS
     the decision; the TTY check at startup is what enforces that a
     human (not an AI agent) made it.
  5. Loop until queue is empty or user quits.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
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


# Canonical outcome buckets the theme digester emits. Anything else in
# data["source"]["outcomes"] is a long-form LLM description of a single
# session's wrap-up: treated as a successful-completion narrative for
# the purposes of the recommend heuristic (it counts toward `completed`).
_CATEGORICAL_OUTCOMES = ("completed", "in-progress", "abandoned", "blocked")

# Imperative-rule markers in feedback claims. Their presence is a strong
# signal the claim is a behavioral rule worth keeping, not just a
# narrative summary.
# Must stay a superset of theme_cluster.py:_IMPERATIVE_MARKERS so any rule
# the synth accepts ALSO classifies as a rule in the REPL. Asymmetric lists
# were flagged in code review 2026-05-11 when a "When X, do Y" v2 rule
# got recommended for [r] reject because the REPL list omitted "when ".
_IMPERATIVE_MARKERS = (
    "don't", "do not", "never", "always", "when ", "must ", "should ",
    "prefer", "use ", "stop ", "avoid ", "only ",
)


_LESSON_PREVIEW_CHARS = 240  # length of the claim shown in the preview;
                              # chosen so a typical terminal width (80-100 cols)
                              # shows ~3 wrapped lines, enough to recognize
                              # whether the content is a behavioral rule.


def _lesson_preview(data: dict, max_chars: int = _LESSON_PREVIEW_CHARS) -> tuple[str, int]:
    """Return (truncated_claim, full_claim_length)."""
    claim = (data.get("claim") or "").strip().replace("\n", " ")
    if len(claim) <= max_chars:
        return (claim, len(claim))
    return (claim[:max_chars].rstrip() + "...", len(claim))


def _behavioral_value(data: dict) -> tuple[str, str]:
    """Return (kind, one_line_explanation).

    Answers the user's actual question: "if I graduate this, what
    behavior will the next LLM session do differently?" Mustafa 2026-05-11:
    "i care about why i should save it... why its going to be helpful".
    By his standard, a graduated lesson must change future LLM sessions'
    actions; cluster summaries and pointers don't qualify.

    kind is one of:
      "rule"     - claim contains an imperative; graduating CAN change
                   future behavior. Worth a [g].
      "data"     - claim is auto-generated cluster summary or outcome
                   list. Graduating adds noise; future LLM sessions get no
                   instruction.
      "marker"   - claim is the theme.digest meta-prompt template
                   ("Recurring topic across N sessions... Review and
                   graduate the durable insight"). Graduating just
                   bookmarks the cluster; no behavior changes.
      "unknown"  - can't classify; user should read the full claim.
    """
    claim = (data.get("claim") or "").strip()
    origin = (data.get("origin") or "").strip()
    head = claim.lower()[:200]

    if claim.lower().startswith("recurring topic across"):
        return (
            "marker",
            "auto-generated meta-prompt ('Recurring topic across N sessions...'); future LLM sessions read it as a pointer, not a rule, and nothing changes",
        )

    if any(m in head for m in _IMPERATIVE_MARKERS):
        return (
            "rule",
            "imperative rule in claim; future LLM sessions will see this and apply it when the conditions match",
        )

    if origin.startswith("theme.digest") or len(claim) > 800:
        return (
            "data",
            "claim is cluster/outcome data, not a rule; future LLM sessions get context noise but no instruction",
        )

    return (
        "unknown",
        "can't tell from the claim head; read full claim with [e] before deciding",
    )


def _recommend(data: dict) -> tuple[str, str]:
    """Return (action_letter, short_reason) advisory.

    Decision rule (Mustafa's standard, 2026-05-11): a graduated lesson
    must change what future LLM sessions DO. Cluster summaries, session
    pointers, and outcome lists don't meet that bar even when they
    carry friction signal; only imperative behavioral rules do.

    Heuristics, in order:
      1. Already rejected before -> reject again.
      2. Claim has rule-shape (imperative marker like don't / always /
         must / use X) -> graduate; this is the only path to [g].
      3. Claim is a theme.digest meta-prompt or cluster summary ->
         reject; no rule, no behavior change.
      4. Anything else -> skip; let the user read it manually.
    """
    rejs = data.get("rejection_count", 0) or 0
    if rejs > 0:
        return ("r", f"rejected {rejs} prior time(s); pattern unchanged")

    kind, _ = _behavioral_value(data)
    # Defensive: legacy / hand-written candidates may have `source`
    # as a bare string instead of a dict. Tolerate both shapes.
    _src = data.get("source")
    outcomes = (_src.get("outcomes") if isinstance(_src, dict) else None) or {}

    if kind == "rule":
        return ("g", "claim is an imperative rule; graduating changes how future LLM sessions act when the condition matches")

    if kind == "marker":
        # Add the friction signal context if present; user may still want
        # to use the cluster as a PROMPT for writing a real lesson via
        # `recall remember`. Recommendation stays [r] because the claim
        # itself doesn't change behavior.
        friction = sum(
            int(outcomes.get(k, 0) or 0)
            for k in ("abandoned", "blocked", "in-progress")
        )
        total = sum(int(v) for v in outcomes.values() if isinstance(v, int))
        if total > 0 and friction / total >= 0.30:
            return (
                "r",
                f"meta-prompt claim, no rule; {int(friction/total*100)}% friction in cluster though, so consider `recall remember \"...\"` to write the real lesson manually",
            )
        return ("r", "meta-prompt claim, no rule; future LLM sessions read it as a pointer and nothing changes")

    if kind == "data":
        return ("r", "claim is cluster/outcome data, not a rule; graduating adds noise to future context")

    return ("s", "claim shape unclear; read the full claim with [e] before deciding")


def _print_candidate(data: dict, idx: int, total: int) -> None:
    cid = data.get("id", "?")
    cs = data.get("cluster_size", "?")
    sal = data.get("canonical_salience", 0)
    eids = data.get("evidence_ids") or []
    rejs = data.get("rejection_count", 0)
    staged = data.get("staged_at", "?")[:10]  # date only, not full timestamp
    origin = data.get("origin") or "?"
    conditions = data.get("conditions") or []
    # Defensive: legacy / hand-written candidates may have `source`
    # as a bare string instead of a dict. Tolerate both shapes.
    _src = data.get("source")
    outcomes = (_src.get("outcomes") if isinstance(_src, dict) else None) or {}

    prio = cs * sal if isinstance(cs, (int, float)) and isinstance(sal, (int, float)) else 0
    tag_str = ", ".join(str(c) for c in conditions) if conditions else "(no tag)"

    print()
    print(f"=== Candidate {idx + 1} of {total} ===")
    print(f"  id:        {cid}")
    print(f"  type:      {origin}")
    print(f"  cluster:   {cs} sessions tagged \"{tag_str}\"  |  priority {prio:.0f}  |  staged {staged}")

    # Outcome breakdown (theme.digest only).
    if origin.startswith("theme.digest") and outcomes:
        parts = []
        for k in _CATEGORICAL_OUTCOMES:
            v = outcomes.get(k)
            if isinstance(v, int) and v > 0:
                parts.append(f"{v} {k}")
        narrative = sum(
            1 for k, v in outcomes.items()
            if isinstance(v, int) and k not in _CATEGORICAL_OUTCOMES
        )
        if narrative:
            parts.append(f"{narrative} other")
        if parts:
            print(f"  outcomes:  {', '.join(parts)}")

    if rejs:
        print(f"  rejected:  {rejs} prior time(s)")

    # Headline block: what BEHAVIOR this lesson would drive in future
    # LLM sessions. Mustafa 2026-05-11: "i care about why i should save
    # it... why its going to be helpful". So lead with the value
    # question, not the storage location.
    kind, why = _behavioral_value(data)
    preview, full_len = _lesson_preview(data)

    kind_label = {
        "rule":    "RULE that would guide future LLM sessions:",
        "marker":  "NO RULE; just a cluster marker. Future LLM sessions would see:",
        "data":    "NO RULE; just cluster/outcome data. Future LLM sessions would see:",
        "unknown": "claim shape unclear. The text is:",
    }.get(kind, "the claim is:")

    print()
    print(f"  {kind_label}")
    wrapped = textwrap.fill(
        f'"{preview}"',
        width=78,
        initial_indent="    ",
        subsequent_indent="    ",
        break_long_words=False,
        break_on_hyphens=False,
    )
    print(wrapped)
    if full_len > _LESSON_PREVIEW_CHARS:
        print(f"    (full claim is {full_len} chars; press [e] to read it all)")

    print()
    print(f"  why this {'helps' if kind == 'rule' else 'does NOT help'}:")
    why_wrapped = textwrap.fill(
        why,
        width=78,
        initial_indent="    ",
        subsequent_indent="    ",
        break_long_words=False,
        break_on_hyphens=False,
    )
    print(why_wrapped)

    rec_action, rec_reason = _recommend(data)
    rec_label = {"g": "graduate", "r": "reject", "s": "skip"}.get(rec_action, "skip")
    print()
    print(f"  recommend: [{rec_action}] {rec_label}")
    rec_wrapped = textwrap.fill(
        rec_reason,
        width=78,
        initial_indent="    ",
        subsequent_indent="    ",
        break_long_words=False,
        break_on_hyphens=False,
    )
    print(rec_wrapped)
    if eids:
        print(f"  (press [e] for all {len(eids)} evidence ids + full claim)")


def _print_full_evidence(data: dict) -> None:
    print()
    print("--- full candidate JSON ---")
    print(json.dumps(data, indent=2))
    print("--- end ---")


def _prompt_for_text(prompt: str) -> Optional[str]:
    """Read a non-empty single-line input. Loop until non-empty.

    Returns None on EOF (Ctrl-D, closed stdin). Caller MUST treat None
    as cancel — do NOT pass an empty string to graduate.py / reject.py.
    Codex 2026-05-05 P2: previously returned "" on EOF, caller invoked
    graduate.py with --rationale "" (argparse accepts it), candidate
    moved without required rationale → defeated the no-auto-decide
    contract by another route. Now EOF here = no decision applied."""
    while True:
        try:
            text = input(prompt).strip()
        except EOFError:
            return None
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


_NAMESPACES = ("default", "claude-sessions", "codex")


def _triage_one_namespace(brain: Path, namespace: str) -> tuple[dict, bool]:
    """Run the interactive REPL over one namespace's candidates queue.
    Returns (decisions_counts_dict, quit_requested_bool)."""
    candidates = _list_candidates(_candidate_dir(brain, namespace))
    decisions = {"graduated": 0, "rejected": 0, "skipped": 0}
    if not candidates:
        return decisions, False

    print()
    print(f"triage: {len(candidates)} staged candidate(s) in namespace={namespace}")
    print(f"        commands: g=graduate r=reject s=skip e=evidence q=quit")
    print(f"        each decision requires an explicit keyboard input.")

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
                continue  # re-prompt

            # The keypress IS the user's decision (Mustafa 2026-05-11
            # "remove rationale (required)"). The no-auto-decide
            # contract is still enforced by the TTY check at startup,
            # which refuses to run without an interactive terminal, so
            # an AI agent calling this loop via Bash cannot rubber-stamp
            # candidates regardless of what rationale string is passed.
            # graduate.py / reject.py still record a rationale field;
            # we fill it with an auto-string that captures the source
            # of the decision rather than prompting the user.
            if choice in ("g", "graduate"):
                if _apply_graduate(brain, namespace, cid,
                                    "graduated via interactive triage"):
                    decisions["graduated"] += 1
                break

            if choice in ("r", "reject"):
                if _apply_reject(brain, namespace, cid,
                                  "rejected via interactive triage"):
                    decisions["rejected"] += 1
                break

            print(f"  unknown choice: {choice!r}. Try g / r / s / e / q.")

    return decisions, quit_requested


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="triage_candidates",
                                 description=__doc__.split("\n")[0])
    p.add_argument("--brain", default=None)
    p.add_argument(
        "--namespace", default=None,
        help=("default | claude-sessions | codex. If omitted, triage walks "
              "ALL namespaces with pending candidates in turn."),
    )
    args = p.parse_args(argv)

    brain = _resolve_brain(args.brain)

    # CRITICAL: refuse to run if stdin isn't interactive. This is the
    # structural enforcement of "user decides per candidate". Codex
    # 2026-05-04 bug: previously, an AI assistant calling this via Bash
    # tool without a PTY would either hang or treat the empty stdin as
    # "skip" — both wrong. Exit 2 with explicit instructions instead.
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

    # Determine which namespaces to walk. With no --namespace, iterate
    # default + claude-sessions + codex (Codex 2026-05-05 P2: previously
    # only default was scanned, so claude-sessions/codex pending counts
    # in the summary couldn't be triaged through the advertised command).
    if args.namespace:
        namespaces_to_walk = [args.namespace]
    else:
        # Auto: walk every namespace that has pending candidates
        namespaces_to_walk = [
            ns for ns in _NAMESPACES
            if _list_candidates(_candidate_dir(brain, ns))
        ]

    if not namespaces_to_walk:
        print(f"triage: no staged candidates in any namespace under {brain}")
        return 0

    totals = {"graduated": 0, "rejected": 0, "skipped": 0}
    early_quit = False
    for ns in namespaces_to_walk:
        if early_quit:
            print(f"triage: skipping namespace={ns} (user quit earlier)")
            continue
        decisions, early_quit = _triage_one_namespace(brain, ns)
        for k, v in decisions.items():
            totals[k] += v

    print()
    print(f"triage: total graduated={totals['graduated']} "
          f"rejected={totals['rejected']} skipped={totals['skipped']}"
          + (" (quit early)" if early_quit else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
