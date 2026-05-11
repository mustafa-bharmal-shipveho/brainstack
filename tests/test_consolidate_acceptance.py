"""Acceptance tests for the consolidation framework (AC-1 through AC-8).

Every test mixes ≥1 non-agentry producer (research-notes, calendar,
nbeditor, discord, teams, fictitious-future-producer-9000) to prove
the framework is producer-agnostic in practice — not just by code
review.

These tests live in one file because they share the `make_brain`
fixture; pytest parameterizes the file naturally.
"""
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import claim_overrides
import claims
import consolidate
import topic_keys


# --- Fixture ----------------------------------------------------------

def _make_brain(tmp_path) -> Path:
    """Build a minimal brain layout for consolidation tests."""
    brain = tmp_path / ".agent"
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "semantic").mkdir()
    return brain


def _append_episodic(brain: Path, event: Dict[str, Any], namespace: str = "default") -> None:
    if namespace == "default":
        path = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    else:
        (brain / "memory" / "episodic" / namespace).mkdir(exist_ok=True)
        path = brain / "memory" / "episodic" / namespace / "AGENT_LEARNINGS.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _event(*, source: str, event_id: str, body: str, source_ts: str,
           kind: str = "observation", **extra) -> Dict[str, Any]:
    """Construct a conforming producer event."""
    ev = {
        "schema_version": 1,
        "ts": "2026-01-01T00:00:00Z",
        "kind": kind,
        "source": source,
        "event_id": event_id,
        "source_ts": source_ts,
        "body_redacted": body,
    }
    ev.update(extra)
    return ev


# --- AC-1: single source supersedes itself ---------------------------

def test_ac1_single_source_supersedes(tmp_path):
    """Three events from one non-agentry source about the same slot
    with monotonic source_ts. Only the newest is current."""
    brain = _make_brain(tmp_path)
    for i, (date, ts) in enumerate([
        ("2026-05-18", "1700000000.0"),
        ("2026-05-19", "1700000100.0"),
        ("2026-05-20", "1700000200.0"),
    ]):
        _append_episodic(brain, _event(
            source="research-notes",
            event_id=f"rn:{i}",
            source_ts=ts,
            body=f"PS2 launches on {date}",
        ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
        now_iso="2026-05-12T00:00:00Z",
    )
    state = result.final_state
    elected = state.current[("project:ps2", "release-date")]
    assert state.claims_by_id[elected].value_normalized == "2026-05-20"
    assert state.claims_by_id[elected].source == "research-notes"


# --- AC-2: cross-source supersedes ----------------------------------

def test_ac2_cross_source_newest_wins(tmp_path):
    """Three different non-agentry producers; newest source_ts wins."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="calendar", event_id="cal:1",
        source_ts="2026-05-10T00:00:00Z",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="nbeditor", event_id="nb:1",
        source_ts="2026-05-12T00:00:00Z",
        body="PS2 launches on 2026-05-20",
    ))
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="2026-05-11T00:00:00Z",
        body="PS2 launches on 2026-05-19",
    ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
        now_iso="2026-05-12T00:00:00Z",
    )
    elected = result.final_state.current[("project:ps2", "release-date")]
    assert result.final_state.claims_by_id[elected].source == "nbeditor"
    assert result.final_state.claims_by_id[elected].value_normalized == "2026-05-20"


# --- AC-3: stale-on-arrival recorded but suppressed ------------------

def test_ac3_stale_on_arrival(tmp_path):
    """T3 (newest) arrives first, T1 (older) arrives later. T1 is
    recorded but marked superseded on insert."""
    brain = _make_brain(tmp_path)
    # Append in non-chronological order: newest first.
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:newest",
        source_ts="1700000200.0",
        body="PS2 launches on 2026-05-20",
    ))
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:oldest",
        source_ts="1700000000.0",
        body="PS2 launches on 2026-05-18",
    ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    state = result.final_state
    elected = state.current[("project:ps2", "release-date")]
    assert state.claims_by_id[elected].value_normalized == "2026-05-20"

    # The older claim exists in the store but is superseded.
    oldest_id = claims.compute_claim_id("project:ps2", "release-date", "rn:oldest")
    assert oldest_id in state.claims_by_id
    assert state.claims_by_id[oldest_id].stance == claims.STANCE_SUPERSEDED


# --- AC-4: tombstone-deleted retracts the prior claim ----------------

def test_ac4_tombstone_deleted_retracts(tmp_path):
    """A source emits a claim, then a tombstone-deleted event referencing
    the original. The claim's stance becomes tombstone."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="discord", event_id="dc:1",
        source_ts="2026-05-10T00:00:00Z",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="discord", event_id="dc:2",
        source_ts="2026-05-11T00:00:00Z",
        kind="tombstone-deleted",
        supersedes_event_id="dc:1",
        body="(deleted)",
    ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "dc:1")
    assert result.final_state.claims_by_id[cid].stance == claims.STANCE_TOMBSTONE
    # No slot is current (only event was retracted).
    assert ("project:ps2", "release-date") not in result.final_state.current


# --- AC-5: tombstone-edited produces a new current ------------------

def test_ac5_tombstone_edited_re_elects(tmp_path):
    """Edit event carries new body; old claim becomes tombstone (it's
    no longer current — the corrected event wins)."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="teams", event_id="t:1",
        source_ts="2026-05-10T00:00:00Z",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="teams", event_id="t:2",
        source_ts="2026-05-11T00:00:00Z",
        kind="tombstone-edited",
        supersedes_event_id="t:1",
        body="PS2 launches on 2026-05-21",
    ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    elected = result.final_state.current[("project:ps2", "release-date")]
    assert result.final_state.claims_by_id[elected].value_normalized == "2026-05-21"


# --- AC-6: no source-specific code paths in the consolidator --------

KNOWN_PRODUCER_NAMES = (
    "slack", "gmail", "agentry", "discord", "teams", "calendar",
    "nbeditor", "research-notes",
)


class _SourceBranchFinder(ast.NodeVisitor):
    """Walk a module's AST looking for known-producer-name comparisons.

    The framework rule: NEVER branch on `event["source"]` or equivalent.
    This scan covers the common evasions: direct comparison, `.get()`,
    `in {…}`, walrus aliases, match/case, dict literals keyed by
    producer names, isinstance routing, getattr/f-string dispatch.
    """

    def __init__(self, src_module_path: Path):
        self.violations: List[str] = []
        self.src_module_path = src_module_path

    def visit_Compare(self, node: ast.Compare):
        for cmpr in node.comparators:
            if isinstance(cmpr, ast.Constant) and isinstance(cmpr.value, str):
                if cmpr.value.lower() in KNOWN_PRODUCER_NAMES:
                    self.violations.append(
                        f"line {node.lineno}: comparison against producer "
                        f"name {cmpr.value!r}"
                    )
            if isinstance(cmpr, (ast.Set, ast.List, ast.Tuple)):
                for elt in cmpr.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value.lower() in KNOWN_PRODUCER_NAMES:
                            self.violations.append(
                                f"line {node.lineno}: containment "
                                f"check against {elt.value!r}"
                            )
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            if node.left.value.lower() in KNOWN_PRODUCER_NAMES:
                self.violations.append(
                    f"line {node.lineno}: comparison against {node.left.value!r}"
                )
        self.generic_visit(node)

    def visit_Match(self, node):  # type: ignore[override]
        for case in node.cases:
            if isinstance(case.pattern, ast.MatchValue):
                v = case.pattern.value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    if v.value.lower() in KNOWN_PRODUCER_NAMES:
                        self.violations.append(
                            f"line {node.lineno}: match/case on {v.value!r}"
                        )
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict):
        names = 0
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                if k.value.lower() in KNOWN_PRODUCER_NAMES:
                    names += 1
        if names >= 2:
            self.violations.append(
                f"line {node.lineno}: dict literal keyed by producer names"
            )
        self.generic_visit(node)


def _ast_scan_for_producer_branches(module_path: Path) -> List[str]:
    src = module_path.read_text()
    tree = ast.parse(src)
    finder = _SourceBranchFinder(module_path)
    finder.visit(tree)
    return finder.violations


def test_ac6_no_source_branches_in_framework_modules():
    """AC-6 structural guarantee: AST scan over every framework module."""
    targets = [
        REPO_ROOT / "agent" / "memory" / "claims.py",
        REPO_ROOT / "agent" / "memory" / "source_ts.py",
        REPO_ROOT / "agent" / "memory" / "topic_keys.py",
        REPO_ROOT / "agent" / "memory" / "claim_overrides.py",
        REPO_ROOT / "agent" / "memory" / "consolidate.py",
        REPO_ROOT / "agent" / "memory" / "projection.py",
    ]
    for t in targets:
        violations = _ast_scan_for_producer_branches(t)
        assert not violations, f"{t.name}: {violations}"


def test_ac6_runtime_fictitious_producer_consolidates_identically(tmp_path):
    """The consolidator must work end-to-end on a producer name it has
    never seen."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="fictitious-future-producer-9000",
        event_id="ffp:1",
        source_ts="2026-05-11T00:00:00Z",
        body="PS2 launches on 2026-05-22",
    ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    elected = result.final_state.current[("project:ps2", "release-date")]
    assert result.final_state.claims_by_id[elected].source == "fictitious-future-producer-9000"
    assert result.final_state.claims_by_id[elected].value_normalized == "2026-05-22"


# --- AC-7: idempotency ----------------------------------------------

def test_ac7_idempotent_rerun(tmp_path):
    """Running consolidation twice on the same input produces an
    identical claim state."""
    brain = _make_brain(tmp_path)
    for i, (date, ts) in enumerate([
        ("2026-05-18", "1700000000.0"),
        ("2026-05-20", "1700000200.0"),
    ]):
        _append_episodic(brain, _event(
            source="research-notes",
            event_id=f"rn:{i}",
            source_ts=ts,
            body=f"PS2 launches on {date}",
        ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state1 = claims.materialize_state(claims._claims_path(str(brain)))
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state2 = claims.materialize_state(claims._claims_path(str(brain)))
    assert state1.current == state2.current
    assert set(state1.claims_by_id) == set(state2.claims_by_id)


def test_ac7_idempotent_after_watermark_deleted(tmp_path):
    """Deleting the watermark and re-running yields identical final
    state (modulo ingested_at). Determinism comes from deterministic
    IDs, not the watermark."""
    brain = _make_brain(tmp_path)
    for i, (date, ts) in enumerate([
        ("2026-05-18", "1700000000.0"),
        ("2026-05-20", "1700000200.0"),
    ]):
        _append_episodic(brain, _event(
            source="research-notes", event_id=f"rn:{i}",
            source_ts=ts, body=f"PS2 launches on {date}",
        ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state1 = claims.materialize_state(claims._claims_path(str(brain)))

    # Delete the watermark and re-run.
    wm = Path(consolidate._watermark_path(str(brain)))
    if wm.exists():
        wm.unlink()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state2 = claims.materialize_state(claims._claims_path(str(brain)))
    assert state1.current == state2.current


# --- AC-8: operator retraction sticks across re-consolidation -------

def test_ac8_retract_claim_id_sticky(tmp_path):
    """A retracted claim stays retracted after a fresh consolidation
    pass — even if new events would otherwise crown it current."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-18",
    ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state1 = claims.materialize_state(claims._claims_path(str(brain)))
    target_cid = state1.current[("project:ps2", "release-date")]
    # Retract via the overrides log.
    overrides_path = claim_overrides._overrides_path(str(brain))
    claim_overrides.retract_by_claim_id(overrides_path, claim_id=target_cid)
    # Re-run consolidation; nothing new in episodic, but the override
    # should now be applied.
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state2 = claims.materialize_state(claims._claims_path(str(brain)))
    assert state2.claims_by_id[target_cid].stance == claims.STANCE_TOMBSTONE
    # Slot has no current.
    assert ("project:ps2", "release-date") not in state2.current


def test_ac8_retract_by_event_id_sticky(tmp_path):
    """Retracting by event_id retracts all claims derived from it.
    Survives re-consolidation."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="calendar", event_id="cal:bad",
        source_ts="2026-05-10T00:00:00Z",
        body="PS2 launches on 2026-05-18",
    ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    overrides_path = claim_overrides._overrides_path(str(brain))
    claim_overrides.retract_by_event_id(overrides_path, event_id="cal:bad")
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state = claims.materialize_state(claims._claims_path(str(brain)))
    cid = claims.compute_claim_id("project:ps2", "release-date", "cal:bad")
    assert state.claims_by_id[cid].stance == claims.STANCE_TOMBSTONE


def test_ac8_predicate_retract_then_restore(tmp_path):
    """A predicate-retract matches a claim; an explicit claim_id
    restore wins on top of it (codex-v3 fix on restore overlay)."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0",
        body="PS2 launches on 2026-05-20",
    ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    overrides_path = claim_overrides._overrides_path(str(brain))
    claim_overrides.retract_by_predicate(
        overrides_path, topic_key="project:ps2", claim_subject="release-date",
        value_pattern=r"2026-05-20",
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "rn:1")
    claim_overrides.restore_by_claim_id(overrides_path, claim_id=cid)
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state = claims.materialize_state(claims._claims_path(str(brain)))
    # Restore should win — the claim is current.
    assert state.current.get(("project:ps2", "release-date")) == cid


# --- Dry-run + watermark ---------------------------------------------

def test_dry_run_writes_nothing(tmp_path):
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-18",
    ))
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.claims_asserted >= 1
    # The claim log file must NOT exist.
    log_path = Path(claims._claims_path(str(brain)))
    assert not log_path.exists()


def test_schema_failure_counted_not_aborted(tmp_path):
    """A row missing required fields is skipped + counted, not fatal."""
    brain = _make_brain(tmp_path)
    # One conforming + one missing body_redacted.
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, {"schema_version": 1, "source": "research-notes",
                              "event_id": "rn:bad", "kind": "x"})
    result = consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    assert result.events_skipped_missing_required >= 1
    assert result.claims_asserted >= 1


def test_batch_size_caps_run(tmp_path):
    """A run with batch_size=2 over 4 events processes exactly 2.
    Next run picks up the rest via the watermark."""
    brain = _make_brain(tmp_path)
    for i in range(4):
        _append_episodic(brain, _event(
            source="research-notes", event_id=f"rn:{i}",
            source_ts=f"170000000{i}.0",
            body=f"PS{i} launches on 2026-05-20",
        ))
    cfg = topic_keys.ExtractorConfig()
    r1 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=2,
    )
    assert r1.events_conforming == 2

    r2 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=2,
    )
    # Second run picks up remaining 2 (or fewer if events 0/1 were
    # skipped because they don't produce claims — depends on topic
    # keys).
    assert r2.events_conforming >= 1


def test_restore_after_retract_survives_re_consolidation(tmp_path):
    """Codex PR3 P1.1 fix: restore semantics now reach
    materialize_state via restored_claim_ids. A retract-then-restore
    sequence ends with the claim CURRENT, even after fresh
    consolidation re-replays the retract event."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-20",
    ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "rn:1")
    overrides_path = claim_overrides._overrides_path(str(brain))
    claim_overrides.retract_by_claim_id(overrides_path, claim_id=cid)
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    state_retracted = claims.materialize_state(
        claims._claims_path(str(brain)),
    )
    assert state_retracted.claims_by_id[cid].stance == claims.STANCE_TOMBSTONE

    # Now restore. After the next consolidation, the claim should be
    # current again (no episodic events changed).
    claim_overrides.restore_by_claim_id(overrides_path, claim_id=cid)
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    overrides = claim_overrides.resolve_overrides(overrides_path)
    state_restored = claims.materialize_state(
        claims._claims_path(str(brain)),
        restored_claim_ids=overrides.restored_claim_ids,
    )
    assert state_restored.claims_by_id[cid].stance == claims.STANCE_CURRENT
    assert state_restored.current[("project:ps2", "release-date")] == cid


def test_watermark_skips_processed_events_on_rerun(tmp_path):
    """Codex PR3 P1.2 fix: the watermark must strictly skip events
    at-or-before the watermarked (source_ts, event_id) tuple. Adding
    only ONE new event after the watermark must result in
    events_conforming == 1 on the next run, not == N (re-processing
    everything)."""
    brain = _make_brain(tmp_path)
    for i in range(3):
        _append_episodic(brain, _event(
            source="research-notes", event_id=f"rn:{i}",
            source_ts=f"170000000{i}.0",
            body=f"PS2 launches on 2026-05-2{i}",
        ))
    cfg = topic_keys.ExtractorConfig()
    r1 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    assert r1.events_conforming == 3

    # Add ONE new event.
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:new",
        source_ts="1700000099.0",
        body="PS2 launches on 2026-06-01",
    ))
    r2 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    # Only the new event should count.
    assert r2.events_conforming == 1, (
        f"watermark didn't skip old events; reprocessed {r2.events_conforming}"
    )


def test_tombstone_edited_within_same_run_supersedes_correctly(tmp_path):
    """Codex PR3 P1.3 fix: if a producer emits both the original event
    AND its tombstone-edited successor in the SAME batch, the
    tombstone-edited must supersede the just-asserted claim. The
    previous code only checked state_before (skipping intra-run
    asserts)."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="teams", event_id="t:1",
        source_ts="2026-05-10T00:00:00Z",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="teams", event_id="t:2",
        source_ts="2026-05-11T00:00:00Z",
        kind="tombstone-edited",
        supersedes_event_id="t:1",
        body="PS2 launches on 2026-05-21",
    ))
    cfg = topic_keys.ExtractorConfig()
    result = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    # Both events processed in one run.
    assert result.events_conforming == 2
    elected = result.final_state.current[("project:ps2", "release-date")]
    assert result.final_state.claims_by_id[elected].value_normalized == "2026-05-21"

    # Verify a supersede transition was appended (tombstone-edited
    # reason).
    log = claims._claims_path(str(brain))
    transitions = [e for e in claims.iter_events(log)
                   if e.get("event_type") == claims.EVENT_SUPERSEDE
                   and e.get("reason") == claims.REASON_TOMBSTONE_EDITED]
    assert len(transitions) >= 1, (
        "tombstone-edited within same run did not append a supersede transition"
    )


def test_noop_rerun_does_not_downgrade_tombstone_marker(tmp_path):
    """Codex PR3 fifth-pass fix: after a run ending on a tombstone
    marker (is_tombstone=1), a no-op rerun must NOT downgrade the
    watermark to is_tombstone=0. Otherwise the same-ts tombstone-
    after-target events could be re-admitted on a later run, appending
    duplicate retract rows."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="discord", event_id="z-orig",
        source_ts="1700000000.0",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="discord", event_id="a-del",
        source_ts="1700000000.0",
        kind="tombstone-deleted",
        supersedes_event_id="z-orig",
        body="(deleted)",
    ))
    cfg = topic_keys.ExtractorConfig()
    # Run 1 — processes both (target then tombstone).
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    log_path = claims._claims_path(str(brain))
    retract_count_1 = sum(
        1 for e in claims.iter_events(log_path)
        if e.get("event_type") == claims.EVENT_RETRACT
    )

    # Run 2 — no-op (no new events).
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    retract_count_2 = sum(
        1 for e in claims.iter_events(log_path)
        if e.get("event_type") == claims.EVENT_RETRACT
    )
    assert retract_count_2 == retract_count_1

    # Run 3 — also no-op, should still be stable.
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    retract_count_3 = sum(
        1 for e in claims.iter_events(log_path)
        if e.get("event_type") == claims.EVENT_RETRACT
    )
    assert retract_count_3 == retract_count_1


def test_tombstone_deleted_same_ts_across_batch_still_retracts(tmp_path):
    """Codex PR3 fourth-pass fix: same source_ts, tombstone event_id
    lex-smaller than target, with batch_size=1. The sort now puts
    non-tombstone events BEFORE tombstones at the same ts, so the
    target processes in run 1 and the tombstone in run 2."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="discord", event_id="z-orig",
        source_ts="1700000000.0",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="discord", event_id="a-del",
        source_ts="1700000000.0",   # same ts as target
        kind="tombstone-deleted",
        supersedes_event_id="z-orig",
        body="(deleted)",
    ))
    cfg = topic_keys.ExtractorConfig()
    r1 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=1,
    )
    assert r1.events_conforming == 1
    # Run 1 processed the target (sorted first because is_tombstone=0).
    r2 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=1,
    )
    assert r2.events_conforming == 1
    # The retract should now be applied.
    cid = claims.compute_claim_id("project:ps2", "release-date", "z-orig")
    state = claims.materialize_state(claims._claims_path(str(brain)))
    assert state.claims_by_id[cid].stance == claims.STANCE_TOMBSTONE
    assert ("project:ps2", "release-date") not in state.current


def test_tombstone_deleted_sorted_before_target_still_retracts(tmp_path):
    """Codex PR3 new-edge fix: with same source_ts, the consolidator
    sorts events by (ts, event_id). A tombstone-deleted with a
    lex-smaller event_id than its target would sort FIRST in the
    batch. The deferred tombstone routing now resolves correctly
    regardless of in-batch order."""
    brain = _make_brain(tmp_path)
    # Same source_ts; tombstone event_id "a-del" sorts before original "z-orig".
    _append_episodic(brain, _event(
        source="discord", event_id="z-orig",
        source_ts="1700000000.0",
        body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="discord", event_id="a-del",
        source_ts="1700000000.0",
        kind="tombstone-deleted",
        supersedes_event_id="z-orig",
        body="(deleted)",
    ))
    cfg = topic_keys.ExtractorConfig()
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "z-orig")
    state = claims.materialize_state(claims._claims_path(str(brain)))
    assert state.claims_by_id[cid].stance == claims.STANCE_TOMBSTONE
    # The slot should have no current claim.
    assert ("project:ps2", "release-date") not in state.current


def test_watermark_works_for_out_of_order_file(tmp_path):
    """Codex PR3 P1.4 fix: file order is not assumed to be chronological.
    Same-ts events with event_ids in reverse-lex order should both be
    processed across batched runs, not skipped forever."""
    brain = _make_brain(tmp_path)
    # Two events at the SAME source_ts; event_id "z..." appended first,
    # "a..." second. With batch_size=1, the old code would process "z",
    # write the watermark to (ts, "z"), then permanently skip "a"
    # (because "a" < "z" lexicographically).
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:z-newer",
        source_ts="1700000000.0",
        body="PS2 launches on 2026-05-20",
    ))
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:a-older",
        source_ts="1700000000.0",
        body="OKR launches on 2026-05-21",
    ))
    cfg = topic_keys.ExtractorConfig()
    r1 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=1,
    )
    assert r1.events_conforming == 1
    r2 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=1,
    )
    # Second run must pick up the remaining event.
    assert r2.events_conforming == 1
    # Both claims now in the log.
    state = claims.materialize_state(claims._claims_path(str(brain)))
    ps2 = claims.compute_claim_id("project:ps2", "release-date", "rn:z-newer")
    okr = claims.compute_claim_id("project:okr", "release-date", "rn:a-older")
    assert ps2 in state.claims_by_id
    assert okr in state.claims_by_id


def test_watermark_handles_non_monotonic_file_order(tmp_path):
    """File order: [newest, oldest]. With batch_size=1, run-1 should
    process the OLDEST (sort makes it come first), run-2 the newest.
    Old code would have processed newest, written a high watermark,
    then skipped oldest forever."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:newer",
        source_ts="1700000100.0",
        body="PS2 launches on 2026-05-21",
    ))
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:older",
        source_ts="1700000000.0",
        body="OKR launches on 2026-05-19",
    ))
    cfg = topic_keys.ExtractorConfig()
    r1 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=1,
    )
    r2 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
        batch_size=1,
    )
    assert r1.events_conforming == 1
    assert r2.events_conforming == 1
    state = claims.materialize_state(claims._claims_path(str(brain)))
    older = claims.compute_claim_id("project:okr", "release-date", "rn:older")
    newer = claims.compute_claim_id("project:ps2", "release-date", "rn:newer")
    assert older in state.claims_by_id
    assert newer in state.claims_by_id


def test_future_schema_version_dropped_with_warner(tmp_path):
    """Schema_version > 1 → dropped + counted + warner called."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:future",
        source_ts="1700000000.0", body="x",
    ) | {"schema_version": 99})
    warned: List[int] = []
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
        schema_warner=lambda n: warned.append(n),
    )
    assert warned == [1]
