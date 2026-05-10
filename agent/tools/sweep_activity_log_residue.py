#!/usr/bin/env python3
"""One-shot sweep: reject already-staged candidates whose claim is verbatim
per-tool narration (`Edited <path>: replaced...`, `Tool X completed...`,
`Wrote <path> (N lines)`, etc).

The upstream filter in `promote.cluster_and_extract` blocks these from
staging on every dream run AFTER PR #28 (2026-05-06). But candidates that
were staged BEFORE the filter shipped — or by a forensic run with
DREAM_ACTIVITY_LOG_DISABLED=1 — sit pending forever. Future dream cycles
just re-stamp their `staged_at`, never re-evaluating the regex.

This tool runs the same sweep `auto_dream` now performs on every cycle,
but immediately and across all namespaces, so the user doesn't have to
wait for the next dream tick.

Deterministic and safe: rejects ONLY the exact claim shapes that the
existing filter would have blocked. A legitimate lesson is never touched.

Usage
-----

    python -m sweep_activity_log_residue [--brain DIR] [--dry-run]

Exit code is the number of items swept (0 = clean), capped at 255.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional


def _resolve_brain(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser()
    return Path(os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")))


_NAMESPACES = ("default", "claude-sessions", "codex")


def _candidate_dir(brain: Path, namespace: str) -> Path:
    if namespace == "default":
        return brain / "memory" / "candidates"
    return brain / "memory" / "candidates" / namespace


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="sweep_activity_log_residue",
                                 description=__doc__.split("\n")[0])
    p.add_argument("--brain", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be swept without writing.")
    args = p.parse_args(argv)

    brain = _resolve_brain(args.brain)
    sys.path.insert(0, str(brain / "memory"))

    from cluster import _is_activity_log_claim  # type: ignore

    if args.dry_run:
        import json
        total = 0
        for ns in _NAMESPACES:
            d = _candidate_dir(brain, ns)
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.json")):
                if not path.is_file():
                    continue
                try:
                    cand = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if cand.get("status") != "staged":
                    continue
                is_log, reason = _is_activity_log_claim(cand.get("claim"))
                if not is_log:
                    continue
                shape = reason.split(":", 1)[1] if ":" in reason else reason
                print(f"would-reject [{ns}] id={cand.get('id')} "
                      f"reason=activity_log_sweep:{shape} "
                      f"claim={(cand.get('claim') or '')[:80]!r}")
                total += 1
        print(f"dry-run: {total} candidate(s) would be swept")
        return min(total, 255)

    from auto_dream import _sweep_activity_log_residue  # type: ignore

    grand_total = 0
    for ns in _NAMESPACES:
        d = _candidate_dir(brain, ns)
        if not d.is_dir():
            continue
        swept = _sweep_activity_log_residue(str(d))
        if not swept:
            continue
        print(f"namespace={ns}: swept {len(swept)} candidate(s)")
        for item in swept:
            print(f"  id={item['id']} reason={item['reason']} "
                  f"claim={item['claim_prefix']!r}")
        grand_total += len(swept)
    print(f"total: {grand_total} candidate(s) swept")
    return min(grand_total, 255)


if __name__ == "__main__":
    sys.exit(main())
