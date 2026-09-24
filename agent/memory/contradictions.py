"""Dream-cycle contradiction detection — STAGING ONLY.

Detects "this staged candidate contradicts that accepted lesson" and turns
it into a `kind="supersession"` proposal for the existing review queue.
The unattended cycle NEVER mutates lessons.jsonl: proposals are staged via
promote.write_supersession_candidates, and only a human running
graduate.py applies the supersession.

Framework rules honored here:
  - No LLM calls. Heuristics reuse claims.is_conflict and the topic_keys
    predicate library (pure functions); ClaimState is only READ.
  - No producer/source branching. Matching keys on topic_key /
    claim_subject / conditions / claim text only.

Heuristic order (first match wins per candidate×lesson pair):
  1. slot-conflict      — both sides resolve to claim records in this
                          cycle's ClaimState and claims.is_conflict is true.
  2. predicate-value    — both claims fire the same built-in predicate
                          (release-date / status / deadline / owner) with
                          different normalized values.
  3. condition-overlap  — Jaccard(conditions) >= 0.5, not an exact
                          duplicate, claim-token Jaccard in (0.15, 0.85).
"""
import hashlib
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import claims
import topic_keys
import validate

# harness/text.py provides word_set / jaccard (same import dance as
# validate.py — the harness dir is a sibling of agent/memory).
_HARNESS = os.path.join(os.path.dirname(__file__), "..", "harness")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)
from text import jaccard, word_set  # noqa: E402

# Predicates eligible for the value-clash heuristic. `decision` is excluded:
# its freeform-2k normalizer makes "different values" almost guaranteed,
# which would stage noise, not contradictions.
_CLASH_PREDICATES = ("release-date", "status", "deadline", "owner")

# Lesson statuses eligible to BE superseded. Anything else (provisional,
# rejected, legacy, already-superseded) is skipped.
_ACTIVE_LESSON_STATUSES = frozenset({"accepted", "current"})

_CONDITION_JACCARD_MIN = 0.5
# Same topic, different wording: below the floor the claims are unrelated;
# at/above the ceiling they're paraphrases (prefilter's exact-dup territory).
_CLAIM_TOKEN_JACCARD_RANGE = (0.15, 0.85)


@dataclass(frozen=True)
class SupersessionProposal:
    """A staged proposal that `claim` should replace lesson `supersedes`."""

    id: str  # sha256(old_id, new_claim_norm, detector)[:12]
    kind: str  # always "supersession"
    claim: str  # the NEWER claim text
    supersedes: str  # old lesson id
    detection_method: str  # slot-conflict | predicate-value | condition-overlap
    evidence_ids: List[str]
    provenance: Dict[str, Any]  # detector, old_id, old_claim, new_claim, cycle_id, claim_ids
    conditions: List[str]
    cluster_size: int
    canonical_salience: float


def compute_proposal_id(old_id: str, new_claim: str, method: str) -> str:
    """Deterministic proposal id. Claim text is normalized so reworded
    re-detections of the same contradiction collide to one id."""
    norm = validate._normalize(new_claim)
    h = hashlib.sha256()
    for part in (old_id, norm, method):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:12]


def _claims_by_event(state: "claims.ClaimState") -> Dict[str, "claims.ClaimRecord"]:
    return {rec.source_event_id: rec for rec in state.claims_by_id.values()}


def _records_for(item: Dict[str, Any], by_event) -> List["claims.ClaimRecord"]:
    out = []
    for eid in item.get("evidence_ids") or []:
        rec = by_event.get(eid)
        if rec is not None:
            out.append(rec)
    return out


def _predicate_values(text: str) -> Dict[str, set]:
    """predicate name → set of normalized values the text asserts.

    Pure: reuses topic_keys' matcher + normalizers, never the LLM extractor.
    """
    out: Dict[str, set] = {}
    for name in _CLASH_PREDICATES:
        pred = topic_keys.DEFAULT_PREDICATES.get(name)
        if not pred:
            continue
        values = set()
        for start, end, _phrase in topic_keys._find_predicate_matches(text, pred):
            if topic_keys._is_negated(text, start):
                continue
            norm_name = pred.get("normalizer")
            norm_fn = topic_keys._NORMALIZERS.get(norm_name)
            if norm_fn is None:
                continue
            if norm_name == "enum":
                value = norm_fn(topic_keys._window_around(text, start, end),
                                enum_values=pred.get("enum_values", []))
            elif norm_name == "person":
                # The name follows the role label — post-match slice only,
                # mirroring HeuristicExtractor.
                value = norm_fn(text[end:end + 200])
            else:
                value = norm_fn(topic_keys._window_around(text, start, end))
            if value:
                values.add(value)
        if values:
            out[name] = values
    return out


def _slot_conflict(new_item, old_item, by_event) -> Optional[List[str]]:
    """claim_ids of the conflicting (new, old) records, or None."""
    new_recs = _records_for(new_item, by_event)
    old_recs = _records_for(old_item, by_event)
    for new_rec in new_recs:
        for old_rec in old_recs:
            if claims.is_conflict(old_rec, new_rec):
                return [new_rec.claim_id, old_rec.claim_id]
    return None


def _predicate_clash(new_claim: str, old_claim: str) -> bool:
    new_values = _predicate_values(new_claim)
    old_values = _predicate_values(old_claim)
    for name in _CLASH_PREDICATES:
        if name in new_values and name in old_values:
            if new_values[name] != old_values[name]:
                return True
    return False


def _condition_overlap(new_item, old_item) -> bool:
    new_conds = set(new_item.get("conditions") or [])
    old_conds = set(old_item.get("conditions") or [])
    if not new_conds or not old_conds:
        return False
    if jaccard(new_conds, old_conds) < _CONDITION_JACCARD_MIN:
        return False
    new_claim = (new_item.get("claim") or "").strip()
    old_claim = (old_item.get("claim") or "").strip()
    # Exact dups are the prefilter's job, not a contradiction.
    if validate._normalize(new_claim) == validate._normalize(old_claim):
        return False
    lo, hi = _CLAIM_TOKEN_JACCARD_RANGE
    token_j = jaccard(word_set(new_claim), word_set(old_claim))
    return lo < token_j < hi


def detect_contradictions(
    candidates: List[Dict[str, Any]],
    lessons: List[Dict[str, Any]],
    *,
    claim_state: "Optional[claims.ClaimState]" = None,
    cycle_id: str = "",
) -> List[SupersessionProposal]:
    """Pair staged candidates against active lessons; return proposals.

    Never raises on malformed rows, never touches disk, never calls an LLM.
    Re-staging policy (already staged/rejected/graduated) is enforced by
    promote.write_supersession_candidates, which owns the candidates dir.
    """
    by_event = _claims_by_event(claim_state) if claim_state is not None else {}

    active_lessons = [
        l for l in lessons
        if l.get("status") in _ACTIVE_LESSON_STATUSES
        and not l.get("superseded_by")
        and (l.get("claim") or "").strip()
    ]

    proposals: Dict[str, SupersessionProposal] = {}
    for cand in candidates:
        new_claim = (cand.get("claim") or "").strip()
        if not new_claim:
            continue
        for lesson in active_lessons:
            old_claim = (lesson.get("claim") or "").strip()
            method = None
            claim_ids: List[str] = []
            if by_event:
                hit = _slot_conflict(cand, lesson, by_event)
                if hit:
                    method = "slot-conflict"
                    claim_ids = hit
            if method is None and _predicate_clash(new_claim, old_claim):
                method = "predicate-value"
            if method is None and _condition_overlap(cand, lesson):
                method = "condition-overlap"
            if method is None:
                continue
            pid = compute_proposal_id(lesson["id"], new_claim, method)
            if pid in proposals:
                continue
            proposals[pid] = SupersessionProposal(
                id=pid,
                kind="supersession",
                claim=new_claim,
                supersedes=lesson["id"],
                detection_method=method,
                evidence_ids=list(cand.get("evidence_ids") or []),
                provenance={
                    "detector": "contradictions",
                    "old_id": lesson["id"],
                    "old_claim": old_claim,
                    "new_claim": new_claim,
                    "cycle_id": cycle_id,
                    "claim_ids": claim_ids,
                },
                conditions=list(cand.get("conditions") or []),
                cluster_size=int(cand.get("cluster_size", 1) or 1),
                canonical_salience=float(cand.get("canonical_salience", 0.0) or 0.0),
            )
    # Deterministic output order.
    return [proposals[k] for k in sorted(proposals.keys())]
