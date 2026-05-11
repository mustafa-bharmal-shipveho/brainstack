"""Operator-override log for the claim store.

Lives at `<brainRoot>/memory/semantic/claim_overrides.jsonl` (per-namespace
under `semantic/<ns>/claim_overrides.jsonl`). Append-only; consolidation
re-reads the log on every run.

Three retraction keying modes:
  claim_id   Specific claim. Highest precedence (resurrects via restore).
  event_id   All claims derived from this producer event. Useful for
             pre-claim retractions (operator marks the event bad even
             before consolidation has run).
  predicate  Topic + subject + value-regex. Useful for "any claim about
             project:PS2/release-date with a value matching .* Monday .*
             is wrong." Applied post-materialization.

A `restore` op on a specific `claim_id` ALWAYS wins over any retraction
that targets that claim. Restores are a final overlay applied after the
three retraction stages, so a `restore` even on a previously
event_id-retracted claim resurrects it.

The override file lives outside the materializable claim state — it
survives `rm claims.jsonl` deliberately, so deleting the claim log
doesn't accidentally un-retract anything (AC-8 stickiness).

Framework rule: this module NEVER inspects `event["source"]` or
hardcodes producer names. Override keys are content-addressed by
claim_id / event_id / predicate.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover
    _HAVE_FLOCK = False


CURRENT_SCHEMA = 1
KNOWN_MAX_SCHEMA = 1

OP_RETRACT = "retract"
OP_RESTORE = "restore"

KEY_CLAIM_ID = "claim_id"
KEY_EVENT_ID = "event_id"
KEY_PREDICATE = "predicate"


_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _overrides_path(brain_root: str, namespace: str = "default") -> str:
    """Resolve the per-namespace override log path."""
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = os.path.abspath(brain_root)
    if namespace == "default":
        return os.path.join(root, "memory", "semantic", "claim_overrides.jsonl")
    return os.path.join(root, "memory", "semantic", namespace, "claim_overrides.jsonl")


def _sentinel_path(data_path: str) -> str:
    return data_path + ".lock"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Append (sentinel-locked, mirrors claims.append_event) -----------

def _append_event(path: str, event: Dict[str, Any]) -> Dict[str, Any]:
    if "schema_version" not in event:
        event["schema_version"] = CURRENT_SCHEMA
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


def retract_by_claim_id(path: str, *, claim_id: str, actor: str = "operator",
                        note: str = "") -> Dict[str, Any]:
    return _append_event(path, {
        "op": OP_RETRACT, "key_type": KEY_CLAIM_ID,
        "claim_id": claim_id, "actor": actor, "note": note,
    })


def retract_by_event_id(path: str, *, event_id: str, actor: str = "operator",
                        note: str = "") -> Dict[str, Any]:
    return _append_event(path, {
        "op": OP_RETRACT, "key_type": KEY_EVENT_ID,
        "event_id": event_id, "actor": actor, "note": note,
    })


def retract_by_predicate(path: str, *, topic_key: str, claim_subject: str,
                         value_pattern: str, actor: str = "operator",
                         note: str = "") -> Dict[str, Any]:
    return _append_event(path, {
        "op": OP_RETRACT, "key_type": KEY_PREDICATE,
        "topic_key": topic_key, "claim_subject": claim_subject,
        "value_pattern": value_pattern,
        "actor": actor, "note": note,
    })


def restore_by_claim_id(path: str, *, claim_id: str, actor: str = "operator",
                        note: str = "") -> Dict[str, Any]:
    return _append_event(path, {
        "op": OP_RESTORE, "key_type": KEY_CLAIM_ID,
        "claim_id": claim_id, "actor": actor, "note": note,
    })


# --- Resolve (replay log, last-write-wins per key) ------------------

@dataclass(frozen=True)
class PredicateOverride:
    topic_key: str
    claim_subject: str
    value_pattern: str

    def matches(self, topic_key: str, claim_subject: str,
                value_normalized: str) -> bool:
        if topic_key != self.topic_key:
            return False
        if claim_subject != self.claim_subject:
            return False
        try:
            return bool(re.search(self.value_pattern, value_normalized or ""))
        except re.error:
            return False


@dataclass
class Overrides:
    retracted_claim_ids: Set[str] = field(default_factory=set)
    retracted_event_ids: Set[str] = field(default_factory=set)
    retracted_predicates: List[PredicateOverride] = field(default_factory=list)
    restored_claim_ids: Set[str] = field(default_factory=set)


def resolve_overrides(path: str) -> Overrides:
    """Replay the override log. Last-write-wins per key.

    For a given `claim_id`, the last `op` (retract or restore) wins.
    For a given `event_id`, the last retract/restore op wins.
    For predicates, retract events accumulate (a restore op on the same
    predicate signature cancels). Practically: each predicate retract
    contributes a `PredicateOverride` to the list; restore on a
    predicate signature drops earlier matches with the same signature.

    A `restore` keyed on a specific `claim_id` is applied on top of
    ALL retraction stages (consolidator overlay), so it can resurrect
    an event_id-retracted claim. See consolidate.py.
    """
    if not os.path.exists(path):
        return Overrides()
    claim_ops: Dict[str, str] = {}        # claim_id → last op
    event_ops: Dict[str, str] = {}        # event_id → last op
    predicate_log: List[Tuple[str, PredicateOverride]] = []  # (op, predicate)

    with open(path) as f:
        text = f.read()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        sv = ev.get("schema_version", 1)
        if isinstance(sv, int) and sv > KNOWN_MAX_SCHEMA:
            continue
        op = ev.get("op")
        kt = ev.get("key_type")
        if op not in (OP_RETRACT, OP_RESTORE):
            continue
        if kt == KEY_CLAIM_ID:
            cid = ev.get("claim_id")
            if cid:
                claim_ops[cid] = op
        elif kt == KEY_EVENT_ID:
            eid = ev.get("event_id")
            if eid:
                event_ops[eid] = op
        elif kt == KEY_PREDICATE:
            tk = ev.get("topic_key", "")
            cs = ev.get("claim_subject", "")
            vp = ev.get("value_pattern", "")
            if tk and cs and vp:
                predicate_log.append(
                    (op, PredicateOverride(topic_key=tk, claim_subject=cs,
                                           value_pattern=vp))
                )

    retracted_claim_ids = {c for c, o in claim_ops.items() if o == OP_RETRACT}
    restored_claim_ids = {c for c, o in claim_ops.items() if o == OP_RESTORE}
    retracted_event_ids = {e for e, o in event_ops.items() if o == OP_RETRACT}

    # Predicates: each retract adds to the active set. A restore on the
    # same signature (topic+subject+pattern) cancels matching retracts.
    active_predicates: List[PredicateOverride] = []
    for op, pred in predicate_log:
        if op == OP_RETRACT:
            active_predicates.append(pred)
        elif op == OP_RESTORE:
            active_predicates = [
                p for p in active_predicates
                if (p.topic_key, p.claim_subject, p.value_pattern)
                != (pred.topic_key, pred.claim_subject, pred.value_pattern)
            ]

    return Overrides(
        retracted_claim_ids=retracted_claim_ids,
        retracted_event_ids=retracted_event_ids,
        retracted_predicates=active_predicates,
        restored_claim_ids=restored_claim_ids,
    )
