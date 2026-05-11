"""Tests for claims.py — event-sourced append + replay.

Pins the storage primitive contracts:
  - Deterministic IDs (claim_id, claim_value_fingerprint, transition_id).
  - assert/supersede/retract events round-trip through append + iter_events.
  - materialize_state derives current/superseded/tombstone correctly.
  - Re-running consolidation never appends a duplicate transition.
  - Schema policy: missing schema_version accepted; > KNOWN_MAX_SCHEMA dropped.
  - Sentinel locking: lock file is `.lock`, not the data file.
  - No producer-specific branches in the module.
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import claims  # noqa: E402


# --- Deterministic IDs ----------------------------------------------

def test_claim_id_includes_source_event_id_so_two_sources_get_distinct_ids():
    """Two events from different producers asserting the same fact get
    DIFFERENT claim_ids — so tombstoning one does not retract the other.

    This was the critical codex-v2-NEW.E bug in v2: collapsing two
    events into one claim broke tombstone provenance.
    """
    cid_slack = claims.compute_claim_id(
        "project:ps2", "release-date", "slack:C0X:1700000000.000001",
    )
    cid_gmail = claims.compute_claim_id(
        "project:ps2", "release-date", "gmail:abc",
    )
    assert cid_slack != cid_gmail


def test_value_fingerprint_collapses_same_fact_from_different_sources():
    """Two events asserting the same normalized value share a
    claim_value_fingerprint (they're members of one fact group), even
    though their claim_ids differ."""
    fp_a = claims.compute_value_fingerprint(
        "project:ps2", "release-date", "2026-05-20",
    )
    fp_b = claims.compute_value_fingerprint(
        "project:ps2", "release-date", "2026-05-20",
    )
    assert fp_a == fp_b


def test_value_fingerprint_differs_for_different_values():
    fp_a = claims.compute_value_fingerprint(
        "project:ps2", "release-date", "2026-05-20",
    )
    fp_b = claims.compute_value_fingerprint(
        "project:ps2", "release-date", "2026-05-21",
    )
    assert fp_a != fp_b


def test_transition_id_is_deterministic_and_distinct_per_reason():
    """Same pair, different reasons → distinct transition_ids. This lets
    a single (old, new) pair carry multiple legitimate transitions
    (e.g. tombstone-edited vs newer-source-ts) without one masking the
    other.
    """
    t1 = claims.compute_transition_id("A", "B", claims.EVENT_SUPERSEDE,
                                       claims.REASON_NEWER_SOURCE_TS)
    t2 = claims.compute_transition_id("A", "B", claims.EVENT_SUPERSEDE,
                                       claims.REASON_TOMBSTONE_EDITED)
    t3 = claims.compute_transition_id("A", "B", claims.EVENT_SUPERSEDE,
                                       claims.REASON_NEWER_SOURCE_TS)
    assert t1 != t2
    assert t1 == t3  # determinism


# --- Append + replay round-trip ------------------------------------

def _path(tmp_path):
    return claims._claims_path(str(tmp_path / ".agent"))


def _assert_event(tmp_path, source_event_id, value, ts):
    p = _path(tmp_path)
    cid = claims.compute_claim_id("project:ps2", "release-date", source_event_id)
    fp = claims.compute_value_fingerprint("project:ps2", "release-date", value)
    claims.append_assert(
        p,
        claim_id=cid,
        claim_value_fingerprint=fp,
        topic_key="project:ps2",
        claim_subject="release-date",
        value_normalized=value,
        value_raw=value,
        source_event_id=source_event_id,
        source="research-notes",   # deliberately a non-agentry producer
        source_ts_epoch=ts,
    )
    return cid


def test_assert_event_persists_and_replays(tmp_path):
    cid = _assert_event(tmp_path, "note:1", "2026-05-20", 1700000000.0)
    state = claims.materialize_state(_path(tmp_path))
    assert cid in state.claims_by_id
    rec = state.claims_by_id[cid]
    assert rec.topic_key == "project:ps2"
    assert rec.claim_subject == "release-date"
    assert rec.value_normalized == "2026-05-20"
    assert rec.source == "research-notes"
    assert rec.stance == claims.STANCE_CURRENT


def test_single_slot_with_two_different_values_one_supersedes_other(tmp_path):
    cid_old = _assert_event(tmp_path, "note:1", "2026-05-18", 1700000000.0)
    cid_new = _assert_event(tmp_path, "note:2", "2026-05-20", 1700000100.0)
    claims.append_supersede(
        _path(tmp_path),
        old_claim_id=cid_old,
        new_claim_id=cid_new,
        reason=claims.REASON_NEWER_SOURCE_TS,
    )
    state = claims.materialize_state(_path(tmp_path))
    assert state.current[("project:ps2", "release-date")] == cid_new
    assert state.claims_by_id[cid_old].stance == claims.STANCE_SUPERSEDED
    assert state.claims_by_id[cid_old].superseded_by == cid_new
    assert state.claims_by_id[cid_new].stance == claims.STANCE_CURRENT


def test_retract_marks_tombstone(tmp_path):
    cid = _assert_event(tmp_path, "note:1", "2026-05-20", 1700000000.0)
    claims.append_retract(
        _path(tmp_path), claim_id=cid, reason=claims.REASON_TOMBSTONE_DELETED,
    )
    state = claims.materialize_state(_path(tmp_path))
    assert state.claims_by_id[cid].stance == claims.STANCE_TOMBSTONE
    # The slot has no current claim once the sole asserter is retracted.
    assert ("project:ps2", "release-date") not in state.current


def test_two_sources_same_fact_form_group_distinct_claims(tmp_path):
    """Two events asserting the same value form a group; both keep
    distinct claim_ids; tombstoning one does NOT retract the other."""
    cid_slack = _assert_event(tmp_path, "slack:1", "2026-05-20", 1700000000.0)
    cid_email = _assert_event(tmp_path, "gmail:1", "2026-05-20", 1700000100.0)
    assert cid_slack != cid_email
    state = claims.materialize_state(_path(tmp_path))
    assert state.claims_by_id[cid_slack].claim_value_fingerprint == \
           state.claims_by_id[cid_email].claim_value_fingerprint

    # Tombstone one event — the other survives as current.
    claims.append_retract(
        _path(tmp_path), claim_id=cid_slack,
        reason=claims.REASON_TOMBSTONE_DELETED,
    )
    state2 = claims.materialize_state(_path(tmp_path))
    assert state2.claims_by_id[cid_slack].stance == claims.STANCE_TOMBSTONE
    assert state2.claims_by_id[cid_email].stance == claims.STANCE_CURRENT
    assert state2.current[("project:ps2", "release-date")] == cid_email


def test_supersede_idempotent_on_rerun(tmp_path):
    """Re-running consolidation produces an IDENTICAL log (modulo
    ingested_at on assert events) because every transition_id is
    deterministic. This is the AC-7 guarantee at the log layer.
    """
    cid_old = _assert_event(tmp_path, "note:1", "v1", 1700000000.0)
    cid_new = _assert_event(tmp_path, "note:2", "v2", 1700000100.0)
    claims.append_supersede(
        _path(tmp_path), old_claim_id=cid_old, new_claim_id=cid_new,
        reason=claims.REASON_NEWER_SOURCE_TS,
    )
    state_a = claims.materialize_state(_path(tmp_path))

    # Re-append the same supersede transition. The log will gain a
    # second line, BUT the transition_id is identical so a higher-level
    # dedup (consolidator) can filter. Verify the determinism here.
    claims.append_supersede(
        _path(tmp_path), old_claim_id=cid_old, new_claim_id=cid_new,
        reason=claims.REASON_NEWER_SOURCE_TS,
    )
    seen = [e for e in claims.iter_events(_path(tmp_path))
            if e.get("event_type") == claims.EVENT_SUPERSEDE]
    assert len(seen) == 2
    assert seen[0]["transition_id"] == seen[1]["transition_id"]

    # Materialized state is unchanged.
    state_b = claims.materialize_state(_path(tmp_path))
    assert state_a.current == state_b.current
    assert state_a.superseded_by == state_b.superseded_by


def test_current_election_picks_newest_source_ts(tmp_path):
    """Without any supersede transition, materialize_state still elects
    one current claim per slot using source_ts_epoch."""
    _assert_event(tmp_path, "note:1", "2026-05-18", 1700000000.0)
    cid_mid = _assert_event(tmp_path, "note:2", "2026-05-19", 1700000100.0)
    cid_new = _assert_event(tmp_path, "note:3", "2026-05-20", 1700000200.0)
    state = claims.materialize_state(_path(tmp_path))
    assert state.current[("project:ps2", "release-date")] == cid_new


# --- Schema policy --------------------------------------------------

def test_missing_schema_version_accepted_as_1(tmp_path):
    """Per agent/memory/sdk.py:184 permissive policy: missing field == 1."""
    p = _path(tmp_path)
    import os
    os.makedirs(Path(p).parent, exist_ok=True)
    line = json.dumps({"event_type": "assert", "claim_id": "x",
                       "topic_key": "t", "claim_subject": "s",
                       "value_normalized": "v",
                       "source_event_id": "e", "source": "research-notes",
                       "source_ts_epoch": 1700000000.0})
    Path(p).write_text(line + "\n")
    events = list(claims.iter_events(p))
    assert len(events) == 1


def test_future_schema_version_dropped_silently(tmp_path):
    """schema_version > KNOWN_MAX_SCHEMA → row dropped on read."""
    p = _path(tmp_path)
    import os
    os.makedirs(Path(p).parent, exist_ok=True)
    rows = [
        json.dumps({"schema_version": 1, "event_type": "assert", "claim_id": "ok",
                    "topic_key": "t", "claim_subject": "s",
                    "value_normalized": "v",
                    "source_event_id": "e", "source": "research-notes",
                    "source_ts_epoch": 1700000000.0}),
        json.dumps({"schema_version": 99, "event_type": "assert", "claim_id": "drop",
                    "topic_key": "t", "claim_subject": "s",
                    "value_normalized": "future-shape", "value_raw": "",
                    "source_event_id": "e2", "source": "research-notes",
                    "source_ts_epoch": 1700000000.0}),
    ]
    Path(p).write_text("\n".join(rows) + "\n")
    events = list(claims.iter_events(p))
    cids = [e["claim_id"] for e in events]
    assert "ok" in cids
    assert "drop" not in cids


# --- Locking + atomic semantics ------------------------------------

def test_append_creates_sentinel_lock_file_not_data_file(tmp_path):
    """The lock must live on `<path>.lock`, mirroring `_episodic_io.py`.
    A direct flock on the data file inode breaks under os.replace
    (compaction)."""
    p = _path(tmp_path)
    _assert_event(tmp_path, "note:1", "v", 1700000000.0)
    assert Path(p).exists()
    assert Path(p + ".lock").exists()


def test_no_producer_branching_in_claims_module():
    """The storage layer must NEVER inspect `source`. Producer-agnostic."""
    src = (REPO_ROOT / "agent" / "memory" / "claims.py").read_text()
    for name in ("slack", "gmail", "agentry", "discord", "calendar", "teams"):
        for bad in (f'"{name}" ==', f'== "{name}"',
                    f"'{name}' ==", f"== '{name}'",
                    f'in ["{name}"', f'in ("{name}"',
                    f"in ['{name}'", f"in ('{name}'"):
            assert bad not in src, (
                f"claims.py contains producer-name branch: {bad!r}"
            )


def test_assert_event_stamps_ingested_at_if_missing(tmp_path):
    cid = _assert_event(tmp_path, "note:1", "v", 1700000000.0)
    state = claims.materialize_state(_path(tmp_path))
    assert state.claims_by_id[cid].ingested_at  # non-empty ISO string


def test_supersede_event_stamps_at_field_if_missing(tmp_path):
    cid_a = _assert_event(tmp_path, "n:a", "va", 1700000000.0)
    cid_b = _assert_event(tmp_path, "n:b", "vb", 1700000100.0)
    claims.append_supersede(_path(tmp_path), old_claim_id=cid_a,
                             new_claim_id=cid_b,
                             reason=claims.REASON_NEWER_SOURCE_TS)
    events = list(claims.iter_events(_path(tmp_path)))
    supersedes = [e for e in events if e.get("event_type") == claims.EVENT_SUPERSEDE]
    assert supersedes[0].get("at")


def test_path_resolution_per_namespace(tmp_path):
    """Per-namespace path matches auto_dream._ns_paths shape."""
    root = str(tmp_path / ".agent")
    assert claims._claims_path(root, "default").endswith(
        "memory/semantic/claims.jsonl"
    )
    assert claims._claims_path(root, "inbox").endswith(
        "memory/semantic/inbox/claims.jsonl"
    )


# --- Election semantics (post-codex-PR1 fixes) ----------------------

def test_retract_newer_claim_re_elects_older(tmp_path):
    """A assert → B assert → supersede A→B → retract B → A becomes
    current. Codex PR1 P1.2: election is authoritative and must
    consider all non-retracted claims, not just non-superseded."""
    cid_a = _assert_event(tmp_path, "n:a", "v-old", 1700000000.0)
    cid_b = _assert_event(tmp_path, "n:b", "v-new", 1700000100.0)
    claims.append_supersede(
        _path(tmp_path), old_claim_id=cid_a, new_claim_id=cid_b,
        reason=claims.REASON_NEWER_SOURCE_TS,
    )
    claims.append_retract(
        _path(tmp_path), claim_id=cid_b,
        reason=claims.REASON_TOMBSTONE_DELETED,
    )
    state = claims.materialize_state(_path(tmp_path))
    assert state.current[("project:ps2", "release-date")] == cid_a
    assert state.claims_by_id[cid_a].stance == claims.STANCE_CURRENT
    assert state.claims_by_id[cid_b].stance == claims.STANCE_TOMBSTONE


def test_election_stable_across_reruns_with_equal_source_ts(tmp_path):
    """When two claims share a source_ts_epoch, the election tie-break
    must be deterministic across re-runs (i.e. independent of
    ingested_at, which is stamped with now() at append time)."""
    cid_a = _assert_event(tmp_path, "n:a", "vA", 1700000000.0)
    cid_b = _assert_event(tmp_path, "n:b", "vB", 1700000000.0)  # same ts
    state_first = claims.materialize_state(_path(tmp_path))
    elected_first = state_first.current[("project:ps2", "release-date")]
    # Append the same assert lines again (simulating an idempotent re-run
    # with a fresh ingested_at stamped).
    _assert_event(tmp_path, "n:a", "vA", 1700000000.0)
    _assert_event(tmp_path, "n:b", "vB", 1700000000.0)
    state_second = claims.materialize_state(_path(tmp_path))
    elected_second = state_second.current[("project:ps2", "release-date")]
    assert elected_first == elected_second, (
        "election drifted across re-runs — tie-break must be deterministic"
    )


def test_only_one_claim_per_slot_marked_current(tmp_path):
    """Codex PR1 P1.1: stance materialization must reflect election.
    Two non-superseded claims in the same slot should NOT both have
    stance=current."""
    cid_a = _assert_event(tmp_path, "n:a", "v-a", 1700000000.0)
    cid_b = _assert_event(tmp_path, "n:b", "v-b", 1700000100.0)
    state = claims.materialize_state(_path(tmp_path))
    elected = state.current[("project:ps2", "release-date")]
    assert elected == cid_b  # newer source_ts wins
    assert state.claims_by_id[cid_a].stance == claims.STANCE_SUPERSEDED
    assert state.claims_by_id[cid_b].stance == claims.STANCE_CURRENT
    assert state.claims_by_id[cid_a].superseded_by == cid_b


def test_malformed_json_line_skipped_silently(tmp_path):
    """A torn or hand-edited row should not kill replay."""
    cid = _assert_event(tmp_path, "n:1", "v1", 1700000000.0)
    # Inject a malformed line.
    p = Path(_path(tmp_path))
    p.write_text(p.read_text() + "this is not json\n")
    _assert_event(tmp_path, "n:2", "v2", 1700000100.0)
    state = claims.materialize_state(_path(tmp_path))
    assert cid in state.claims_by_id


def test_malformed_assert_field_skipped_not_fatal(tmp_path):
    """A row with a non-numeric source_ts_epoch should be skipped,
    not crash replay."""
    cid_ok = _assert_event(tmp_path, "n:1", "v1", 1700000000.0)
    # Hand-write a malformed assert row.
    p = Path(_path(tmp_path))
    bad = {
        "schema_version": 1, "event_type": "assert",
        "claim_id": "bad-id", "topic_key": "project:ps2",
        "claim_subject": "release-date",
        "value_normalized": "x", "value_raw": "x",
        "source_event_id": "n:bad", "source": "research-notes",
        "source_ts_epoch": "not-a-number",
    }
    p.write_text(p.read_text() + json.dumps(bad) + "\n")
    state = claims.materialize_state(_path(tmp_path))
    assert cid_ok in state.claims_by_id
    assert "bad-id" not in state.claims_by_id


def test_schema_version_non_int_passes_through(tmp_path):
    """Verbatim sdk.py:184 policy: only `isinstance(v, int) and v > MAX`
    is dropped. String '2', float 2.0, etc. pass through."""
    p = Path(_path(tmp_path))
    import os
    os.makedirs(p.parent, exist_ok=True)
    rows = [
        # string "2" — accepted per SDK
        '{"schema_version": "2", "event_type": "assert", "claim_id": "s1",'
        ' "topic_key": "t", "claim_subject": "s", "value_normalized": "v",'
        ' "source_event_id": "e1", "source": "research-notes",'
        ' "source_ts_epoch": 1700000000.0}',
        # float 99.0 — accepted per SDK (isinstance(99.0, int) is False)
        '{"schema_version": 99.0, "event_type": "assert", "claim_id": "f1",'
        ' "topic_key": "t", "claim_subject": "s", "value_normalized": "v",'
        ' "source_event_id": "e2", "source": "research-notes",'
        ' "source_ts_epoch": 1700000000.0}',
        # int 99 — dropped per SDK
        '{"schema_version": 99, "event_type": "assert", "claim_id": "drop",'
        ' "topic_key": "t", "claim_subject": "s", "value_normalized": "v",'
        ' "source_event_id": "e3", "source": "research-notes",'
        ' "source_ts_epoch": 1700000000.0}',
    ]
    p.write_text("\n".join(rows) + "\n")
    cids = {e.get("claim_id") for e in claims.iter_events(str(p))}
    assert "s1" in cids
    assert "f1" in cids
    assert "drop" not in cids


def test_namespace_validation_rejects_path_traversal(tmp_path):
    """Hardcoded namespace check mirrors auto_dream._NAMESPACE_RE so
    `--namespace ../etc` can't escape the brain root."""
    root = str(tmp_path / ".agent")
    with pytest.raises(ValueError):
        claims._claims_path(root, "../etc")
    with pytest.raises(ValueError):
        claims._claims_path(root, "")
    with pytest.raises(ValueError):
        claims._claims_path(root, "UPPER")
    # "default" is allowed without going through the regex.
    claims._claims_path(root, "default")
    # Valid namespace passes.
    claims._claims_path(root, "inbox")


def test_corrected_value_overrides_record_but_keeps_ingested_at(tmp_path):
    """A producer re-emits the same source_event_id with a CORRECTED
    value. The claim_id is the same (it doesn't include value), but
    the fingerprint differs. The corrected value must take effect, and
    the claim must move out of the old fingerprint group.

    Tie-break stability (`ingested_at`) is preserved across re-runs.
    """
    # First assert: value v1, fingerprint A.
    cid = _assert_event(tmp_path, "n:1", "v1", 1700000000.0)
    state1 = claims.materialize_state(_path(tmp_path))
    ingested_first = state1.claims_by_id[cid].ingested_at
    fp_first = state1.claims_by_id[cid].claim_value_fingerprint

    # Corrected assert: same source_event_id, new value v2, new fingerprint B.
    import time
    time.sleep(0.01)
    _assert_event(tmp_path, "n:1", "v2", 1700000000.0)
    state2 = claims.materialize_state(_path(tmp_path))

    # Same claim_id, corrected value.
    assert cid in state2.claims_by_id
    assert state2.claims_by_id[cid].value_normalized == "v2"
    fp_second = state2.claims_by_id[cid].claim_value_fingerprint
    assert fp_second != fp_first

    # Ingested_at is stable across re-runs.
    assert state2.claims_by_id[cid].ingested_at == ingested_first

    # The claim is in the new fingerprint group, not the old one.
    assert cid in state2.groups[fp_second]
    assert fp_first not in state2.groups or cid not in state2.groups[fp_first]


def test_duplicate_assert_does_not_overwrite_ingested_at(tmp_path):
    """A re-run that re-asserts the same claim_id must NOT advance the
    stored ingested_at (which is stamped with now() at append time).
    First-seen wins so re-runs are deterministic."""
    import time
    cid = _assert_event(tmp_path, "n:1", "v1", 1700000000.0)
    state_first = claims.materialize_state(_path(tmp_path))
    first_ingested = state_first.claims_by_id[cid].ingested_at

    time.sleep(0.01)
    _assert_event(tmp_path, "n:1", "v1", 1700000000.0)
    state_second = claims.materialize_state(_path(tmp_path))
    second_ingested = state_second.claims_by_id[cid].ingested_at

    assert first_ingested == second_ingested
