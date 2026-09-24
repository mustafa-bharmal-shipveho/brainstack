"""Deterministic contradiction detection → STAGED supersession proposals.

The detector (agent/memory/contradictions.py) never mutates durable memory:
it turns "this staged candidate contradicts that accepted lesson" into a
kind="supersession" candidate on the existing review queue. Only a human
running graduate.py applies the supersession.
"""
import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

# agent/tools also ships a promote.py; if another test module imported that
# one first, drop it so `import promote` below binds the memory-side module.
_stale = sys.modules.get("promote")
if _stale is not None and "agent/tools" in (getattr(_stale, "__file__", "") or ""):
    del sys.modules["promote"]

import claims  # noqa: E402
import contradictions  # noqa: E402
import promote  # noqa: E402
import render_lessons  # noqa: E402


def _lesson(lid, claim, status="accepted", conditions=None, evidence_ids=None,
            **extra):
    row = {
        "id": lid,
        "claim": claim,
        "conditions": conditions or [],
        "evidence_ids": evidence_ids or [],
        "status": status,
        "accepted_at": "2026-01-01T00:00:00Z",
        "reviewer": "host-agent",
        "rationale": "seeded",
        "supersedes": None,
    }
    row.update(extra)
    return row


def _candidate(cid, claim, conditions=None, evidence_ids=None, **extra):
    cand = {
        "id": cid,
        "key": cid,
        "name": cid,
        "claim": claim,
        "conditions": conditions or [],
        "evidence_ids": evidence_ids or [],
        "cluster_size": 2,
        "canonical_salience": 7.0,
        "staged_at": "2026-06-01T00:00:00+00:00",
        "status": "staged",
        "decisions": [],
        "rejection_count": 0,
    }
    cand.update(extra)
    return cand


def _rec(topic, subject, value, event_id, fingerprint=None):
    return claims.ClaimRecord(
        claim_id=claims.compute_claim_id(topic, subject, event_id),
        claim_value_fingerprint=fingerprint or f"fp:{value}",
        topic_key=topic,
        claim_subject=subject,
        value_normalized=value,
        value_raw=value,
        source_event_id=event_id,
        source="unit-test",
        source_ts_epoch=1700000000.0,
        ingested_at="2026-01-01T00:00:00Z",
    )


def _state(*recs):
    return claims.ClaimState(
        current={(r.topic_key, r.claim_subject): r.claim_id for r in recs},
        groups={},
        claims_by_id={r.claim_id: r for r in recs},
        superseded_by={},
    )


# --- Heuristic 1: slot conflict via materialized claim state ----------


def test_slot_conflict_emits_proposal_with_provenance():
    old = _rec("project:ps2", "release-date", "2026-05-18", "ev_old")
    new = _rec("project:ps2", "release-date", "2026-05-20", "ev_new")
    state = _state(old, new)
    lesson = _lesson("lesson_old", "PS2 launches on 2026-05-18 per the plan",
                     evidence_ids=["ev_old"])
    cand = _candidate("cand_new", "PS2 launches on 2026-05-20 per the update",
                      evidence_ids=["ev_new"])

    proposals = contradictions.detect_contradictions(
        [cand], [lesson], claim_state=state, cycle_id="2026-06-01T00:00:00Z")

    assert len(proposals) == 1
    p = proposals[0]
    assert p.kind == "supersession"
    assert p.supersedes == "lesson_old"
    assert p.claim == cand["claim"]
    assert p.detection_method == "slot-conflict"
    prov = p.provenance
    assert prov["old_id"] == "lesson_old"
    assert prov["old_claim"] == lesson["claim"]
    assert prov["new_claim"] == cand["claim"]
    assert prov["cycle_id"] == "2026-06-01T00:00:00Z"
    assert set(prov["claim_ids"]) == {old.claim_id, new.claim_id}
    assert prov["detector"]


def test_same_fingerprint_is_not_a_contradiction():
    """Same slot + same value fingerprint = members of one fact group,
    not a conflict (claims.is_conflict semantics)."""
    old = _rec("project:ps2", "release-date", "2026-05-20", "ev_old")
    new = _rec("project:ps2", "release-date", "2026-05-20", "ev_new")
    state = _state(old, new)
    assert not claims.is_conflict(old, new)

    lesson = _lesson("lesson_old", "PS2 launches on 2026-05-20",
                     evidence_ids=["ev_old"])
    cand = _candidate("cand_new", "PS2 launches on 2026-05-20",
                      evidence_ids=["ev_new"])
    proposals = contradictions.detect_contradictions(
        [cand], [lesson], claim_state=state, cycle_id="c")
    assert proposals == []


# --- Heuristic 2: predicate-value clash (no claim state needed) -------


def test_predicate_value_clash():
    lesson = _lesson("lesson_old", "The deadline is 2026-05-01 for the migration",
                     conditions=["migration"])
    cand = _candidate("cand_new", "The deadline is 2026-07-15 for the migration",
                      conditions=["rollout"])
    proposals = contradictions.detect_contradictions([cand], [lesson], cycle_id="c")
    assert len(proposals) == 1
    assert proposals[0].detection_method == "predicate-value"
    assert proposals[0].supersedes == "lesson_old"


def test_same_predicate_value_is_not_a_clash():
    lesson = _lesson("lesson_old", "The deadline is 2026-05-01 for the migration",
                     conditions=["migration"])
    cand = _candidate("cand_new", "Reminder: deadline is 2026-05-01, migration",
                      conditions=["rollout"])
    proposals = contradictions.detect_contradictions([cand], [lesson], cycle_id="c")
    assert proposals == []


# --- Heuristic 3: condition overlap ------------------------------------


def test_condition_overlap_stages_proposal():
    conditions = ["deploy", "database", "prod"]
    lesson = _lesson("lesson_old",
                     "Always run database migrations before the deploy to prod",
                     conditions=conditions)
    cand = _candidate("cand_new",
                      "Never run database migrations before the deploy to prod",
                      conditions=conditions)
    proposals = contradictions.detect_contradictions([cand], [lesson], cycle_id="c")
    assert len(proposals) == 1
    assert proposals[0].detection_method == "condition-overlap"


def test_disjoint_conditions_do_not_match():
    lesson = _lesson("lesson_old",
                     "Always run database migrations before the deploy to prod",
                     conditions=["deploy", "database"])
    cand = _candidate("cand_new",
                      "Never run database migrations before the deploy to prod",
                      conditions=["frontend", "css"])
    proposals = contradictions.detect_contradictions([cand], [lesson], cycle_id="c")
    assert proposals == []


def test_exact_duplicate_is_not_staged():
    """Exact duplicates are the heuristic prefilter's job, not ours."""
    claim = "Always serialize timestamps in UTC across service boundaries"
    lesson = _lesson("lesson_old", claim, conditions=["timestamp", "utc"])
    cand = _candidate("cand_new", claim, conditions=["timestamp", "utc"])
    proposals = contradictions.detect_contradictions([cand], [lesson], cycle_id="c")
    assert proposals == []


# --- Skip rules ---------------------------------------------------------


def test_non_active_lessons_are_skipped():
    cand = _candidate("cand_new", "The deadline is 2026-07-15 for the migration",
                      conditions=["migration"])
    for status in ("superseded", "provisional", "rejected", "legacy"):
        lesson = _lesson("lesson_old",
                         "The deadline is 2026-05-01 for the migration",
                         status=status, conditions=["migration"])
        assert contradictions.detect_contradictions([cand], [lesson]) == [], status
    # A lesson with a persisted superseded_by pointer is terminal too.
    lesson = _lesson("lesson_old", "The deadline is 2026-05-01 for the migration",
                     conditions=["migration"], superseded_by="lesson_newer")
    assert contradictions.detect_contradictions([cand], [lesson]) == []


def test_missing_fields_do_not_raise():
    cand = {"id": "c", "claim": "x"}
    lesson = {"id": "l", "status": "accepted"}
    assert contradictions.detect_contradictions([cand], [lesson]) == []
    assert contradictions.detect_contradictions([], []) == []


# --- Determinism --------------------------------------------------------


def test_proposal_id_is_stable_across_runs():
    a = contradictions.compute_proposal_id("lesson_old", "New claim text", "slot-conflict")
    b = contradictions.compute_proposal_id("lesson_old", "New claim text", "slot-conflict")
    assert a == b and len(a) == 12
    # Different inputs → different ids.
    assert a != contradictions.compute_proposal_id("lesson_old", "Other text", "slot-conflict")
    assert a != contradictions.compute_proposal_id("lesson_old", "New claim text", "condition-overlap")
    # Claim-text normalization: case/punctuation differences collapse.
    c = contradictions.compute_proposal_id("lesson_old", "new claim text!", "slot-conflict")
    assert c == a


# --- Structural guards ---------------------------------------------------


KNOWN_PRODUCER_NAMES = (
    "slack", "gmail", "agentry", "discord", "teams", "calendar",
    "nbeditor", "research-notes",
)


def test_no_source_branch_in_module():
    """AST scan: the detector must never branch on a producer name
    (same rule as test_consolidate_acceptance AC-6)."""
    src = (REPO_ROOT / "agent" / "memory" / "contradictions.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            consts = [node.left, *node.comparators]
            for c in consts:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    assert c.value.lower() not in KNOWN_PRODUCER_NAMES, (
                        f"line {node.lineno}: comparison against producer "
                        f"name {c.value!r}")


def test_does_not_import_an_llm_client():
    """Detection is deterministic — no LLM provider imports allowed."""
    src = (REPO_ROOT / "agent" / "memory" / "contradictions.py").read_text()
    tree = ast.parse(src)
    banned_roots = {"llm_extractor", "openai", "anthropic", "ollama", "litellm"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for n in names:
            assert n.split(".")[0] not in banned_roots, f"imports {n!r}"


# --- Staging through the review queue (step 7) --------------------------


def _proposal(pid="prop1", claim="Never reboot the frobnicate before deploys",
              supersedes="lesson_old", evidence=None):
    return contradictions.SupersessionProposal(
        id=pid,
        kind="supersession",
        claim=claim,
        supersedes=supersedes,
        detection_method="condition-overlap",
        evidence_ids=evidence or ["2026-06-01T00:00:00Z"],
        provenance={"detector": "contradictions", "old_id": supersedes,
                    "old_claim": "old", "new_claim": claim,
                    "cycle_id": "c", "claim_ids": []},
        conditions=["deploy"],
        cluster_size=2,
        canonical_salience=7.0,
    )


def _seed_brain(tmp_path):
    semantic = tmp_path / "memory" / "semantic"
    candidates = tmp_path / "memory" / "candidates"
    semantic.mkdir(parents=True)
    candidates.mkdir(parents=True)
    render_lessons.append_lesson(_lesson(
        "lesson_old", "Always reboot the frobnicate service before deploying",
        conditions=["deploy"]), str(semantic))
    render_lessons.render_lessons(str(semantic))
    return str(candidates), str(semantic)


def test_candidate_json_shape(tmp_path):
    candidates_dir, _ = _seed_brain(tmp_path)
    n = promote.write_supersession_candidates([_proposal()], candidates_dir)
    assert n == 1
    data = json.loads((Path(candidates_dir) / "prop1.json").read_text())
    assert data["kind"] == "supersession"
    assert data["claim"] == _proposal().claim
    assert data["supersedes"] == "lesson_old"
    assert data["detection_method"] == "condition-overlap"
    assert data["provenance"]["old_id"] == "lesson_old"
    assert data["evidence_ids"] == ["2026-06-01T00:00:00Z"]
    assert data["status"] == "staged"
    assert data["decisions"][-1]["action"] == "staged"
    assert data["decisions"][-1]["reviewer"] == "auto_dream"


def test_write_supersession_candidates_does_not_touch_lessons_jsonl(tmp_path):
    candidates_dir, semantic = _seed_brain(tmp_path)
    before = (Path(semantic) / "lessons.jsonl").read_bytes()
    before_md = (Path(semantic) / "LESSONS.md").read_bytes()
    promote.write_supersession_candidates([_proposal()], candidates_dir)
    assert (Path(semantic) / "lessons.jsonl").read_bytes() == before
    assert (Path(semantic) / "LESSONS.md").read_bytes() == before_md


def test_restage_skipped_when_already_rejected_and_evidence_unchanged(tmp_path):
    candidates_dir, _ = _seed_brain(tmp_path)
    promote.write_supersession_candidates([_proposal()], candidates_dir)
    from review_state import mark_rejected
    mark_rejected("prop1", "host-agent", "not a real contradiction", candidates_dir)
    # Same proposal, same evidence → NOT re-staged.
    n = promote.write_supersession_candidates([_proposal()], candidates_dir)
    assert n == 0
    assert not (Path(candidates_dir) / "prop1.json").exists()
    assert (Path(candidates_dir) / "rejected" / "prop1.json").exists()
    # New evidence → re-staged (recurring signal worth a second look).
    n = promote.write_supersession_candidates(
        [_proposal(evidence=["2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"])],
        candidates_dir)
    assert n == 1
    assert (Path(candidates_dir) / "prop1.json").exists()


def test_restage_skipped_when_already_staged(tmp_path):
    candidates_dir, _ = _seed_brain(tmp_path)
    assert promote.write_supersession_candidates([_proposal()], candidates_dir) == 1
    assert promote.write_supersession_candidates([_proposal()], candidates_dir) == 0
    data = json.loads((Path(candidates_dir) / "prop1.json").read_text())
    # History preserved: a repeat staging is recorded, not duplicated.
    assert data["status"] == "staged"


def test_graduated_proposal_is_not_restaged(tmp_path):
    candidates_dir, _ = _seed_brain(tmp_path)
    promote.write_supersession_candidates([_proposal()], candidates_dir)
    from review_state import mark_graduated
    mark_graduated("prop1", "host-agent", "accepted", candidates_dir)
    assert promote.write_supersession_candidates([_proposal()], candidates_dir) == 0
    assert not (Path(candidates_dir) / "prop1.json").exists()
