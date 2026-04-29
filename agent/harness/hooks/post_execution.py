"""Runs after every action. Appends a structured entry to episodic memory."""
import datetime, os
from ._provenance import build_source
from ._episodic_io import append_jsonl

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
EPISODIC = os.path.join(ROOT, "memory/episodic/AGENT_LEARNINGS.jsonl")

# PR1 schema unification: every episode carries an `origin` discriminator
# (`coding.tool_call`, `agentry.<agent>.<event>`, etc.) so the dream cycle
# can cluster within-origin and lessons stay scoped to the right stream.
DEFAULT_ORIGIN = "coding.tool_call"
SUMMARY_MAX = 120


def _derive_summary(reflection, action) -> str:
    """Return a 1-line cluster-relevant snippet when the caller didn't
    pass an explicit summary. Prefer reflection (richer narrative) over
    action (mechanical label).

    Defensive against non-string callers: SDK consumers can pass any
    truthy value here, and `int.strip()` would otherwise crash the
    post-tool hook. Coerce to str before truncating.
    """
    text = str(reflection).strip() if reflection else ""
    if not text:
        text = str(action).strip() if action else ""
    return text[:SUMMARY_MAX]


def log_execution(skill_name, action, result, success, reflection="",
                  importance=5, confidence=0.5, evidence_ids=None,
                  pain_score=None, origin=DEFAULT_ORIGIN, summary=None):
    """Log a structured episodic entry.

    pain_score: override the default (2 for success, 7 for failure). Pass
    a higher value (e.g. 5) for high-importance successful operations so
    recurring patterns cross the dream-cycle promotion threshold (7.0).

    origin: identifies the writer stream. `coding.tool_call` for Claude
    Code post-tool hooks; `agentry.<agent>.<event>` for personal-agent
    actions; etc. The dream cycle clusters WITHIN origin.

    summary: 1-line cluster feature. Auto-derives from
    `(reflection or action)[:120]` when None. Explicit overrides win.
    """
    if pain_score is None:
        pain_score = 2 if success else 7
    if summary is None:
        summary = _derive_summary(reflection, action)
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "skill": skill_name,
        "action": action[:200],
        "result": "success" if success else "failure",
        "detail": str(result)[:500],
        "pain_score": pain_score,
        "importance": importance,
        "reflection": reflection,
        "confidence": confidence,
        "source": build_source(skill_name),
        "evidence_ids": list(evidence_ids) if evidence_ids else [],
        "origin": origin,
        "summary": summary,
    }
    return append_jsonl(EPISODIC, entry)
