"""Demote a previously-graduated lesson back to rejected.

Inverse of graduate.py. Use when a lesson promoted in retrospect turns
out to be noise — e.g. it was graduated before the upstream activity-log
filter shipped (PR #28, 2026-05-06), or a manual graduation needs to be
walked back.

What it does:

  1. Locate the matching lesson row in semantic/lessons.jsonl (by
     `id == lesson_<candidate_id>`).
  2. Remove that row from lessons.jsonl and re-render LESSONS.md.
  3. Move candidates/graduated/<cid>.json to candidates/rejected/<cid>.json
     with a `demoted` decision attached.

Atomicity: semantic write runs FIRST. If the script crashes between
removing the lesson and moving the candidate, the lesson is gone from
LESSONS.md but the candidate is still in graduated/. The reviewer can
re-run; `remove_lesson` is idempotent (returns None when the row is
already gone) and the move step proceeds anyway.

Rationale is REQUIRED — same contract as graduate.py / reject.py. A
silent demote that loses the "why" is the failure mode this layer is
designed to prevent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE, "memory"))

from render_lessons import remove_lesson, render_lessons  # noqa: E402
from review_state import mark_demoted  # noqa: E402


CANDIDATES = os.path.join(BASE, "memory/candidates")
SEMANTIC = os.path.join(BASE, "memory/semantic")


def _resolve_paths(namespace):
    """Resolve (CANDIDATES, SEMANTIC) for the given namespace. Honors
    BRAIN_ROOT just like graduate.py / reject.py."""
    brain_root = os.environ.get("BRAIN_ROOT")
    if brain_root:
        root = os.path.abspath(os.path.expanduser(brain_root))
        memory = os.path.join(root, "memory")
    else:
        memory = os.path.join(BASE, "memory")
    if namespace == "default":
        return (os.path.join(memory, "candidates"),
                os.path.join(memory, "semantic"))
    return (os.path.join(memory, "candidates", namespace),
            os.path.join(memory, "semantic", namespace))


def _lesson_id_for(candidate):
    """Mirror graduate._lesson_id: lesson id is `lesson_<candidate_id>`.
    Fallback to md5(claim) is only for pre-id legacy candidates."""
    cid = candidate.get("id") or ""
    if cid:
        return f"lesson_{cid}"
    import hashlib
    claim = (candidate.get("claim") or "").strip().lower()
    return "lesson_" + hashlib.md5(claim.encode()).hexdigest()[:12]


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Demote a graduated lesson back to rejected.")
    p.add_argument("candidate_id")
    p.add_argument("--reason", required=True,
                   help="Why this lesson is being walked back. Required.")
    p.add_argument("--reviewer", default="host-agent")
    p.add_argument("--namespace", default="default",
                   help="Brain namespace (default: 'default' = v0.1 layout).")
    args = p.parse_args(argv)

    candidates, semantic = _resolve_paths(args.namespace)

    graduated_path = os.path.join(
        candidates, "graduated", f"{args.candidate_id}.json")
    if not os.path.exists(graduated_path):
        print(f"ERROR: graduated candidate not found: {args.candidate_id}",
              file=sys.stderr)
        sys.exit(1)
    with open(graduated_path) as f:
        cand = json.load(f)

    lesson_id = _lesson_id_for(cand)
    removed = remove_lesson(lesson_id, semantic)
    if removed is None:
        # Could be a legitimate retry (semantic already cleaned, candidate
        # still in graduated/) or a divergence (lesson never existed under
        # this id). Both resolve by continuing to the candidate move —
        # mark_demoted is the ground truth for status.
        print(f"note: lesson {lesson_id} not present in lessons.jsonl "
              f"(idempotent retry or pre-existing divergence); proceeding "
              f"with candidate move", file=sys.stderr)
    # skip_migrate=True: we just deleted a row from lessons.jsonl, and the
    # corresponding bullet is still in LESSONS.md. Without this flag, the
    # render's migrate_legacy_bullets pass would notice the bullet-without-
    # jsonl-row and reimport it as a new `lesson_legacy_*` entry — exactly
    # what demote is unwinding.
    md_path = render_lessons(semantic, skip_migrate=True)

    mark_demoted(args.candidate_id, args.reviewer, args.reason, candidates)

    print(f"demoted {args.candidate_id}")
    if removed is not None:
        claim_preview = (removed.get("claim") or "")[:80]
        print(f"  removed lesson: {lesson_id} — {claim_preview!r}")
    print(f"  re-rendered:    {md_path}")
    print(f"  moved to:       {os.path.join(candidates, 'rejected', args.candidate_id)}.json")

    # Mirror graduate.py: best-effort refresh of PENDING_REVIEW.md so the
    # user's surfaces reflect the new state. Never fail the demote.
    try:
        import render_pending_summary
        from pathlib import Path as _Path
        brain = _Path(os.path.dirname(os.path.dirname(candidates)))
        if brain.name == "memory":
            brain = brain.parent
        render_pending_summary.render(brain)
    except Exception:
        pass


if __name__ == "__main__":
    main()
