"""Event-sourced claim log for the consolidation framework.

Storage: `<brainRoot>/memory/semantic/claims.jsonl` (per-namespace under
`semantic/<ns>/claims.jsonl` for non-default namespaces). Append-only;
materialized state is derived on every consolidation pass.

Three event types live in the log:

  assert     A claim derived from a producer event. Keyed by `claim_id`
             (unique per `source_event_id`) and grouped by
             `claim_value_fingerprint` (one fingerprint == one fact).
  supersede  A transition from one current claim_id to a newer one.
             Has a deterministic `transition_id` so re-runs never
             duplicate it.
  retract   A claim is no longer current (operator-initiated, tombstone-
             deleted, etc). Also has a deterministic `transition_id`.

Determinism is the source of truth:
  - `claim_id = sha256(topic_key || claim_subject || source_event_id)`
    Each source event produces a unique claim. Two events asserting the
    same value (same `claim_value_fingerprint`) share a fingerprint but
    NOT a claim_id, so tombstoning one event does not retract the others.
  - `claim_value_fingerprint = sha256(topic_key || claim_subject ||
    value_normalized)` — the de-dup grouping key.
  - `transition_id = sha256(old_claim_id || new_claim_id || event_type
    || reason)` — multiple `supersede` transitions between the same
    pair (different reasons) are intentionally distinct.

Locking: sentinel-pattern same as `_episodic_io.append_jsonl`. Lock file
is `claims.jsonl.lock` (sibling); the data file is `claims.jsonl`. An
atomic rewrite (compaction) swaps the inode without invalidating
in-flight appenders' lock acquisitions because the lock identity lives
on the sentinel, not the data file.

This module is the storage primitive only — extraction, supersession
logic, and projection live elsewhere. NO producer-specific code paths
here; the module never inspects `source`.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import fcntl  # POSIX
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover — Windows
    _HAVE_FLOCK = False


# --- Schema versioning ------------------------------------------------

CURRENT_SCHEMA = 1
# Matches the permissive policy in agent/memory/sdk.py:184 — rows with
# schema_version > KNOWN_MAX_SCHEMA are dropped on read (with a single
# warning per consolidation run, emitted by the caller). Rows missing
# the field are accepted as schema_version=1 (forward-compat).
KNOWN_MAX_SCHEMA = 1


# --- Event types & records --------------------------------------------

EVENT_ASSERT = "assert"
EVENT_SUPERSEDE = "supersede"
EVENT_RETRACT = "retract"

REASON_NEWER_SOURCE_TS = "newer-source-ts"
REASON_TOMBSTONE_EDITED = "tombstone-edited"
REASON_TOMBSTONE_DELETED = "tombstone-deleted"
REASON_OPERATOR = "operator"
REASON_OVERRIDE_RE_ELECTION = "override-re-election"

STANCE_CURRENT = "current"
STANCE_SUPERSEDED = "superseded"
STANCE_TOMBSTONE = "tombstone"


@dataclass(frozen=True)
class ClaimRecord:
    """The materialized view of a single claim_id, derived from log replay.

    This dataclass is the snapshot a consumer (projection, recall) sees.
    The log on disk stores `assert` events with these fields plus the
    separate transition events that update `stance` / `superseded_by`.
    """
    claim_id: str
    claim_value_fingerprint: str
    topic_key: str
    claim_subject: str
    value_normalized: str
    value_raw: str
    source_event_id: str
    source: str
    source_ts_epoch: float
    ingested_at: str
    stance: str = STANCE_CURRENT
    superseded_by: Optional[str] = None


@dataclass(frozen=True)
class ClaimState:
    """Materialized state after replaying a claim log."""
    current: Dict[Tuple[str, str], str]  # (topic, subject) → claim_id
    groups: Dict[str, List[str]]         # fingerprint → [claim_id]
    claims_by_id: Dict[str, ClaimRecord]
    superseded_by: Dict[str, str]        # old_id → new_id (most recent)


# --- Deterministic IDs ------------------------------------------------

def _sha256_hex(*parts: str) -> str:
    h = hashlib.sha256()
    for i, p in enumerate(parts):
        if i:
            h.update(b"\0")
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def compute_claim_id(topic_key: str, claim_subject: str, source_event_id: str) -> str:
    """Deterministic claim ID. Same triple → same id."""
    return _sha256_hex(topic_key, claim_subject, source_event_id)


def compute_value_fingerprint(topic_key: str, claim_subject: str,
                              value_normalized: str) -> str:
    """Deterministic de-dup grouping key. Two events with the same
    normalized value share a fingerprint (one fact), but they keep
    distinct claim_ids so tombstoning one does not retract the others."""
    return _sha256_hex(topic_key, claim_subject, value_normalized)


def compute_transition_id(old_claim_id: str, new_claim_id: str,
                          event_type: str, reason: str) -> str:
    """Deterministic transition ID. Re-runs producing the same transition
    write the same ID — append-once semantics for supersede/retract."""
    return _sha256_hex(old_claim_id, new_claim_id, event_type, reason)


# --- Sentinel-locked append (mirrors _episodic_io.append_jsonl) -------

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _claims_path(brain_root: str, namespace: str = "default") -> str:
    """Resolve the per-namespace claims log path.

    Namespace validation mirrors `agent/memory/auto_dream.py:_NAMESPACE_RE`
    so an attacker-controlled CLI flag (e.g. ``--namespace ../etc``) can't
    escape the intended `<brain_root>/memory/semantic/` tree.
    """
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = os.path.abspath(brain_root)
    if namespace == "default":
        return os.path.join(root, "memory", "semantic", "claims.jsonl")
    return os.path.join(root, "memory", "semantic", namespace, "claims.jsonl")


def _sentinel_path(data_path: str) -> str:
    return data_path + ".lock"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def append_event(path: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically append one event line to the claims log.

    Lock identity lives on `path + ".lock"`. Mirrors `_episodic_io.append_jsonl`
    so concurrent compaction (which uses os.replace) does not invalidate
    in-flight appenders' locks.

    Stamps `schema_version` if missing. Stamps `at` (or `ingested_at` on
    `assert` events) if missing. Returns the stamped event.
    """
    if "schema_version" not in event:
        event["schema_version"] = CURRENT_SCHEMA
    if event.get("event_type") == EVENT_ASSERT:
        if "ingested_at" not in event:
            event["ingested_at"] = _now_iso()
    else:
        if "at" not in event:
            event["at"] = _now_iso()

    payload = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return event
    sentinel = _sentinel_path(path)

    if _HAVE_FLOCK:
        try:
            lock_fd = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return event
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                with open(path, "ab") as f:
                    f.write(payload)
                    f.flush()
            except OSError:
                pass
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    else:  # pragma: no cover — Windows
        try:
            with open(path, "ab") as f:
                f.write(payload)
                f.flush()
        except OSError:
            pass
    return event


def append_assert(path: str, *, claim_id: str, claim_value_fingerprint: str,
                  topic_key: str, claim_subject: str, value_normalized: str,
                  value_raw: str, source_event_id: str, source: str,
                  source_ts_epoch: float) -> Dict[str, Any]:
    """Append an `assert` event. Caller is responsible for ID determinism
    (use `compute_claim_id` / `compute_value_fingerprint`)."""
    event = {
        "event_type": EVENT_ASSERT,
        "claim_id": claim_id,
        "claim_value_fingerprint": claim_value_fingerprint,
        "topic_key": topic_key,
        "claim_subject": claim_subject,
        "value_normalized": value_normalized,
        "value_raw": value_raw,
        "source_event_id": source_event_id,
        "source": source,
        "source_ts_epoch": source_ts_epoch,
    }
    return append_event(path, event)


def append_supersede(path: str, *, old_claim_id: str, new_claim_id: str,
                     reason: str) -> Dict[str, Any]:
    """Append a `supersede` event with a deterministic transition_id."""
    transition_id = compute_transition_id(old_claim_id, new_claim_id,
                                          EVENT_SUPERSEDE, reason)
    event = {
        "event_type": EVENT_SUPERSEDE,
        "transition_id": transition_id,
        "old_claim_id": old_claim_id,
        "new_claim_id": new_claim_id,
        "reason": reason,
    }
    return append_event(path, event)


def append_retract(path: str, *, claim_id: str, reason: str) -> Dict[str, Any]:
    """Append a `retract` event with a deterministic transition_id.

    The `new_claim_id` slot in the transition_id seed is the empty string
    so retracts have a distinct hash space from supersedes targeting the
    same claim_id.
    """
    transition_id = compute_transition_id(claim_id, "", EVENT_RETRACT, reason)
    event = {
        "event_type": EVENT_RETRACT,
        "transition_id": transition_id,
        "claim_id": claim_id,
        "reason": reason,
    }
    return append_event(path, event)


# --- Replay -----------------------------------------------------------

def iter_events(path: str) -> Iterable[Dict[str, Any]]:
    """Yield events from the log in file order. Bad lines are skipped silently
    (consistent with `_load_entries_locked_path` posture in auto_dream.py).
    A row whose `schema_version` exceeds KNOWN_MAX_SCHEMA is also skipped;
    callers may count + warn (handled at the consolidator boundary).
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        text = f.read()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Permissive schema policy — verbatim match for `agent/memory/sdk.py:184`:
        # missing → 1 (accepted). Non-int values (string, float, None)
        # pass through unchanged (accepted). Only `isinstance(v, int) and
        # v > KNOWN_MAX_SCHEMA` is dropped.
        sv = obj.get("schema_version", 1)
        if isinstance(sv, int) and sv > KNOWN_MAX_SCHEMA:
            continue
        yield obj


def materialize_state(path: str,
                      restored_claim_ids: Optional[set] = None,
                      ) -> ClaimState:
    """Replay the log and produce a `ClaimState`.

    Replay rules:
      1. assert: register the claim_id (or refresh metadata if it appeared
         before — last assert wins, since assert is content-addressed by
         claim_id and re-asserts are no-ops with the same value).
      2. supersede: mark old_claim_id as superseded; record the pointer
         (last write wins per old_claim_id).
      3. retract: mark claim_id as retracted (tombstone stance) UNLESS
         the claim_id is in `restored_claim_ids`, in which case the
         retract is ignored. Restoration must be passed in by the
         consolidator (it lives in `claim_overrides.jsonl`, NOT the
         claim log). [codex PR3 P1.1 fix — restore was previously a
         no-op because materialize_state didn't see the override log.]
      4. After all events: for each (topic_key, claim_subject) slot, the
         "current" claim is the highest-source_ts_epoch claim_id that
         isn't superseded and isn't tombstoned. Group siblings within a
         value-fingerprint do NOT supersede each other; they share the
         current slot (consumer chooses one for projection — newest
         ingested wins as a tie-break).
    """
    restored: set = restored_claim_ids or set()
    claims: Dict[str, ClaimRecord] = {}
    first_ingested_at: Dict[str, str] = {}
    retracted: set = set()
    groups: Dict[str, List[str]] = {}

    for ev in iter_events(path):
        etype = ev.get("event_type")
        if etype == EVENT_ASSERT:
            cid = ev.get("claim_id")
            if not cid:
                continue
            # Skip malformed rows (e.g. non-numeric source_ts_epoch) rather
            # than letting them kill replay.
            try:
                source_ts_epoch = float(ev.get("source_ts_epoch", 0.0))
            except (TypeError, ValueError):
                continue
            fp = ev.get("claim_value_fingerprint") or ""
            # Determinism vs. correctness trade-off: `claim_id` does NOT
            # include `value_normalized`, so a producer that re-emits the
            # same source_event_id with a corrected value yields the same
            # claim_id but a different fingerprint. We accept the corrected
            # value (last-write-wins on content) BUT preserve the original
            # `ingested_at` so the tie-break order in election stays
            # stable across re-runs (re-stamped `now()` would drift it).
            stable_ingested = first_ingested_at.setdefault(
                cid, ev.get("ingested_at", ""),
            )
            rec = ClaimRecord(
                claim_id=cid,
                claim_value_fingerprint=fp,
                topic_key=ev.get("topic_key", ""),
                claim_subject=ev.get("claim_subject", ""),
                value_normalized=ev.get("value_normalized", ""),
                value_raw=ev.get("value_raw", ""),
                source_event_id=ev.get("source_event_id", ""),
                source=ev.get("source", ""),
                source_ts_epoch=source_ts_epoch,
                ingested_at=stable_ingested,
            )
            claims[cid] = rec
            if fp:
                # A claim_id can move between fingerprint groups across
                # asserts (corrected value). Remove from any stale bucket
                # before adding to the current one.
                for other_fp, bucket in list(groups.items()):
                    if other_fp != fp and cid in bucket:
                        bucket.remove(cid)
                        if not bucket:
                            del groups[other_fp]
                bucket = groups.setdefault(fp, [])
                if cid not in bucket:
                    bucket.append(cid)
        elif etype == EVENT_SUPERSEDE:
            # Transition events are informational provenance. Election
            # (below) decides current state; explicit `supersede` records
            # are kept on disk for audit but do not gate election.
            pass
        elif etype == EVENT_RETRACT:
            cid = ev.get("claim_id")
            if cid and cid not in restored:
                retracted.add(cid)

    # Election is authoritative. For each (topic, subject) slot, the
    # current claim is the highest-`source_ts_epoch` non-retracted
    # claim. This means a `retract` on the newest claim correctly
    # re-elects the next-newest non-retracted sibling (codex PR1 P1.2).
    # Explicit `supersede` events are *evidence* of past transitions;
    # they don't shadow re-election.
    #
    # Tie-break: claim_id ascending (deterministic across re-runs;
    # ingested_at would drift because it's stamped at write-time).
    candidates: Dict[Tuple[str, str], List[ClaimRecord]] = {}
    for rec in claims.values():
        if rec.claim_id in retracted:
            continue
        slot = (rec.topic_key, rec.claim_subject)
        candidates.setdefault(slot, []).append(rec)

    current: Dict[Tuple[str, str], str] = {}
    elected_by_slot: Dict[Tuple[str, str], str] = {}
    for slot, recs in candidates.items():
        recs.sort(key=lambda r: (-r.source_ts_epoch, r.claim_id))
        current[slot] = recs[0].claim_id
        elected_by_slot[slot] = recs[0].claim_id

    # Stance materialization derived from election + retraction.
    # - retracted → STANCE_TOMBSTONE
    # - elected current for its slot → STANCE_CURRENT
    # - else → STANCE_SUPERSEDED (and `superseded_by` points at the
    #         elected current for that slot)
    finalized: Dict[str, ClaimRecord] = {}
    materialized_superseded_by: Dict[str, str] = {}
    for cid, rec in claims.items():
        if cid in retracted:
            stance = STANCE_TOMBSTONE
            sb = None
        else:
            slot = (rec.topic_key, rec.claim_subject)
            if elected_by_slot.get(slot) == cid:
                stance = STANCE_CURRENT
                sb = None
            else:
                stance = STANCE_SUPERSEDED
                sb = elected_by_slot.get(slot)
                if sb is not None:
                    materialized_superseded_by[cid] = sb
        finalized[cid] = replace(rec, stance=stance, superseded_by=sb)

    return ClaimState(
        current=current,
        groups=groups,
        claims_by_id=finalized,
        superseded_by=materialized_superseded_by,
    )


# --- Helpers used by the consolidator (no producer branching) ---------

def is_conflict(existing: ClaimRecord, candidate: ClaimRecord) -> bool:
    """Return True iff the two claims occupy the same slot with different
    normalized values. Same slot + same fingerprint → not a conflict
    (they're members of the same fact group)."""
    if existing.topic_key != candidate.topic_key:
        return False
    if existing.claim_subject != candidate.claim_subject:
        return False
    return existing.claim_value_fingerprint != candidate.claim_value_fingerprint


def record_to_dict(rec: ClaimRecord) -> Dict[str, Any]:
    """Plain dict (e.g. for JSON serialization or projection frontmatter)."""
    return asdict(rec)
