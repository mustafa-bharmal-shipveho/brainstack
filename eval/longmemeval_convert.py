"""Convert public long-term-memory benchmarks into the native bench dataset format.

Native format (consumed by `bench_recall_ab.py`):

    {
      "corpus":    [{"slug": str, "title": str, "body": str}, ...],
      "questions": [{"id": str, "q": str, "question_type": str,
                     "supports": [slug, ...],        # answer-bearing docs (recall@k)
                     "answer_substring": str,        # coverage check in top-k text
                     "docs": [slug, ...],            # optional per-question haystack
                     "stale": [slug, ...],           # optional contradicted docs
                     "stale_answer_substring": str}, # optional outdated-answer probe
                    ...]
    }

Supported inputs:
  - LongMemEval v1 (`longmemeval_*_cleaned.json`): a JSON list of instances,
    each with its own haystack of chat sessions. Every session becomes one
    doc; a question's `docs` restricts retrieval to its own haystack, matching
    the benchmark's per-instance evaluation.
  - LongMemEval-V2 (a prepared data root directory with `questions.jsonl`,
    `trajectories.jsonl`, `haystacks/lme_v2_<tier>.json`): every trajectory
    becomes one doc (goal + action/thought/accessibility-tree text). V2 has no
    answer-bearing-trajectory labels, so its questions are coverage-only
    (`supports` empty); answer presence in the retrieved context is the
    retrieval-sufficiency metric.
"""
from __future__ import annotations

import json
from pathlib import Path


def _session_body(session: list[dict]) -> str:
    return "\n".join(f"{t.get('role', '?')}: {t.get('content', '')}" for t in session)


def convert_v1(instances: list[dict]) -> dict:
    """LongMemEval v1 instances -> native dataset."""
    corpus: list[dict] = []
    questions: list[dict] = []
    emitted: set[str] = set()
    for inst in instances:
        qid = inst["question_id"]
        sids = inst["haystack_session_ids"]
        sessions = inst["haystack_sessions"]
        dates = list(inst.get("haystack_dates") or [])
        if len(dates) < len(sids):  # tolerate truncated date lists
            dates += [""] * (len(sids) - len(dates))
        answer_sids = set(inst["answer_session_ids"])
        my_slugs: list[str] = []
        for sid, session, date in zip(sids, sessions, dates):
            slug = f"{qid}__{sid}"
            my_slugs.append(slug)
            if slug in emitted:
                continue
            emitted.add(slug)
            corpus.append(
                {
                    "slug": slug,
                    "title": f"{sid} ({date})" if date else sid,
                    "body": _session_body(session),
                }
            )
        questions.append(
            {
                "id": qid,
                "q": str(inst["question"]),
                "question_type": inst.get("question_type", ""),
                "supports": [f"{qid}__{sid}" for sid in sids if sid in answer_sids],
                # Answers are not always strings in the real dataset (numeric
                # answers arrive as int) — coerce at the boundary.
                "answer_substring": str(inst["answer"]),
                "docs": my_slugs,
            }
        )
    return {"corpus": corpus, "questions": questions}


def _trajectory_body(traj: dict) -> str:
    parts = [f"goal: {traj.get('goal', '')}", f"outcome: {traj.get('outcome', '')}"]
    for state in traj.get("states", []):
        if state.get("action"):
            parts.append(f"action: {state['action']}")
        if state.get("thought"):
            parts.append(f"thought: {state['thought']}")
        tree = state.get("accessibility_tree")
        if isinstance(tree, str) and tree:
            parts.append(tree)
        elif tree:
            # Structured observation: serialize as JSON, not Python repr.
            parts.append(json.dumps(tree, ensure_ascii=False))
    return "\n".join(parts)


def convert_v2(data_root: Path, tier: str = "small") -> dict:
    """LongMemEval-V2 prepared data root -> native dataset (coverage-only)."""
    questions_path = data_root / "questions.jsonl"
    trajectories_path = data_root / "trajectories.jsonl"
    haystack_path = data_root / "haystacks" / f"lme_v2_{tier}.json"
    for path in (questions_path, trajectories_path, haystack_path):
        if not path.exists():
            raise FileNotFoundError(f"missing LongMemEval-V2 file: {path}")

    question_rows = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    trajectories = {}
    for line in trajectories_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            trajectories[row["id"]] = row
    haystack = json.loads(haystack_path.read_text(encoding="utf-8"))

    corpus: list[dict] = []
    questions: list[dict] = []
    emitted: set[str] = set()
    missing = [tid for tids in haystack.values() for tid in tids if tid not in trajectories]
    if missing:
        raise ValueError(
            f"haystack references {len(missing)} trajectory id(s) absent from "
            f"trajectories.jsonl (first: {missing[0]})"
        )
    for q in question_rows:
        qid = q["id"]
        tids = haystack.get(qid, [])
        for tid in tids:
            if tid in emitted:
                continue
            emitted.add(tid)
            traj = trajectories[tid]
            corpus.append(
                {
                    "slug": tid,
                    "title": f"{tid} ({traj.get('domain', '')})",
                    "body": _trajectory_body(traj),
                }
            )
        questions.append(
            {
                "id": qid,
                "q": q["question"],
                "question_type": q.get("question_type", ""),
                "supports": [],
                "answer_substring": q.get("answer", ""),
                "docs": list(tids),
            }
        )
    return {"corpus": corpus, "questions": questions}
