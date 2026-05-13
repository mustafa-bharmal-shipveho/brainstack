"""Consolidation engine — the orchestrator that turns producer episodic
JSONL into claim records.

Pipeline (one run = one `run_consolidation` call):
  1. Load the watermark (last processed event_id + epoch).
  2. Walk every `<brain>/memory/episodic/**/AGENT_LEARNINGS.jsonl`
     file for the chosen namespace, in deterministic order. Skip
     events at or before the watermark.
  3. For each conforming event:
     a. Normalize source_ts.
     b. Detect tombstone kinds (`tombstone-deleted`, `tombstone-edited`)
        and route them as transition triggers — append `retract` or
        `supersede` log events referring to the prior claim.
     c. Otherwise: call extractors → emit Claim tuples.
     d. For each Claim: append an `assert` event to the claim log.
        Conflict detection vs current state of the same slot triggers
        a `supersede` transition with `REASON_NEWER_SOURCE_TS`.
  4. Apply operator overrides (3 stages + restore overlay) and re-elect.
     Synthetic `supersede` transitions for re-elected current claims
     use the canonical `REASON_OVERRIDE_RE_ELECTION` reason.
  5. Advance the watermark and return a `ConsolidationResult`.

Dry-run mode: same pipeline, but NO appends to disk. Returns the same
result for inspection.

Locking: this module does NOT take the brain-wide lock. Callers
(`auto_dream.run()`, future `recall consolidate` CLI) are responsible
for acquiring it. The claims-log sentinel-lock is taken per append.

Framework rule: NEVER inspect `event["source"]`. The only fields read
from each episodic row are the documented schema fields (kind, ts,
source_ts, body_redacted, event_id, supersedes_event_id, plus optional
counterparty/channel_id forwarded to extractors).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import claim_overrides
import claims
import projection
import source_ts
import topic_keys


_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

DEFAULT_BATCH_SIZE = 10000

# Tombstone kinds are part of the documented producer contract.
KIND_TOMBSTONE_DELETED = "tombstone-deleted"
KIND_TOMBSTONE_EDITED = "tombstone-edited"


# --- Watermark --------------------------------------------------------

def _watermark_path(brain_root: str, namespace: str = "default") -> str:
    """Resolve the per-namespace watermark path."""
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = os.path.abspath(brain_root)
    if namespace == "default":
        return os.path.join(root, "memory", "semantic",
                            ".consolidation_watermark.json")
    return os.path.join(root, "memory", "semantic", namespace,
                        ".consolidation_watermark.json")


def _read_watermark(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_watermark(path: str, last_event_id: str,
                     last_source_ts_epoch: float, run_at: str,
                     last_is_tombstone: int = 0) -> None:
    """Atomic watermark write via _atomic.atomic_write_bytes."""
    from _atomic import atomic_write_bytes  # local import — avoid cycles
    payload = json.dumps({
        "schema_version": 1,
        "last_processed_event_id": last_event_id,
        "last_processed_source_ts_epoch": last_source_ts_epoch,
        "last_processed_is_tombstone": last_is_tombstone,
        "last_run_at": run_at,
    }, sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, payload)


# --- Episodic walker -------------------------------------------------

def _episodic_paths(brain_root: str, namespace: str = "default") -> List[str]:
    """List every AGENT_LEARNINGS.jsonl for a namespace.

    Default namespace walks the top-level `memory/episodic/AGENT_LEARNINGS.jsonl`
    plus every sub-namespace directory containing its own
    `AGENT_LEARNINGS.jsonl` (so we see ALL producer streams from one
    consolidation run).
    """
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = os.path.abspath(brain_root)
    ep_root = os.path.join(root, "memory", "episodic")
    if not os.path.isdir(ep_root):
        return []
    paths: List[str] = []
    if namespace == "default":
        top = os.path.join(ep_root, "AGENT_LEARNINGS.jsonl")
        if os.path.isfile(top):
            paths.append(top)
        # Also include sub-namespaces — producers may write under
        # `episodic/<their-namespace>/AGENT_LEARNINGS.jsonl`.
        for name in sorted(os.listdir(ep_root)):
            sub = os.path.join(ep_root, name, "AGENT_LEARNINGS.jsonl")
            if os.path.isfile(sub) and name not in ("snapshots",):
                paths.append(sub)
    else:
        sub = os.path.join(ep_root, namespace, "AGENT_LEARNINGS.jsonl")
        if os.path.isfile(sub):
            paths.append(sub)
    return paths


def _iter_episodic_events(paths: List[str]) -> Iterable[Dict[str, Any]]:
    """Yield events from every path in stable order. Bad lines are
    skipped (matches `_load_entries_locked` posture)."""
    for p in paths:
        try:
            with open(p) as f:
                text = f.read()
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


# --- Result -----------------------------------------------------------

@dataclass
class ConsolidationResult:
    events_total: int = 0
    events_conforming: int = 0
    events_skipped_future_schema: int = 0
    events_skipped_missing_required: int = 0
    events_skipped_unparseable_ts: int = 0
    claims_asserted: int = 0
    supersedes_appended: int = 0
    retracts_appended: int = 0
    overrides_applied: int = 0
    re_election_supersedes: int = 0
    projection_written: int = 0
    projection_unchanged: int = 0
    projection_orphans_deleted: int = 0
    dry_run: bool = False
    final_state: Optional[claims.ClaimState] = None


# --- Conformance helpers ---------------------------------------------

_REQUIRED_FIELDS = ("kind", "event_id", "source_ts", "body_redacted")


def _is_conforming(event: Dict[str, Any]) -> bool:
    """An event is conforming if it has every required field present
    and non-empty (kind/event_id/source_ts/body_redacted). The
    `source` field is required by the contract but never branched on,
    so we tolerate it being absent (treat as empty string) without
    rejecting the event."""
    for fld in _REQUIRED_FIELDS:
        v = event.get(fld)
        if v is None or (isinstance(v, str) and not v.strip()):
            return False
    return True


# --- Tombstone routing -----------------------------------------------

def _find_claims_for_event(state: claims.ClaimState,
                          event_id: str) -> List[str]:
    """Return claim_ids that were asserted from `event_id`. There is
    one claim_id per (topic, subject, event_id) triple, so an event
    can yield multiple claims; we find them all."""
    return [c.claim_id for c in state.claims_by_id.values()
            if c.source_event_id == event_id]


# --- Main entry ------------------------------------------------------

def run_consolidation(brain_root: str, namespace: str = "default",
                      *, dry_run: bool = False,
                      batch_size: int = DEFAULT_BATCH_SIZE,
                      extractors: Optional[List[topic_keys.TopicKeyExtractor]] = None,
                      now_iso: Optional[str] = None,
                      schema_warner=None) -> ConsolidationResult:
    """Run one consolidation pass for `namespace`.

    Parameters:
      brain_root  Absolute path to the brain root (e.g. ~/.agent).
      namespace   Per-namespace consolidation. "default" walks the top-
                  level episodic file plus every sub-namespace dir.
      dry_run     If True, no disk writes. Result reflects what WOULD
                  happen.
      batch_size  Max events to process this run (bounded latency).
                  After hitting the cap, the watermark advances to the
                  last processed event so the next run picks up.
      extractors  Optional list. Defaults to
                  `topic_keys.default_extractors()` (which loads
                  operator config from disk).
      now_iso     Optional override for "now" — for deterministic tests.
                  Used in the watermark `last_run_at` field only.
      schema_warner  Optional callable(int) -> None; called with the
                  count of future-schema rows seen this run. Default:
                  print one warning to stderr.

    Returns a ConsolidationResult; the consumer (CLI) can render it.
    """
    if extractors is None:
        # Pass brain_root + namespace so the LLM extractor (opt-in via
        # extractors.toml [extractor] mode = "llm" | "hybrid") can
        # find its per-event cache dir.
        extractors = topic_keys.default_extractors(
            brain_root=brain_root, namespace=namespace,
        )
    if now_iso is None:
        import datetime
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
    if schema_warner is None:
        def _default_warner(count):  # pragma: no cover
            sys.stderr.write(
                f"[brainstack] dropped {count} episodic row(s) with "
                "future schema_version; upgrade may be needed\n"
            )
        schema_warner = _default_warner

    result = ConsolidationResult(dry_run=dry_run)

    wm_path = _watermark_path(brain_root, namespace)
    wm = _read_watermark(wm_path)
    last_seen_event_id: Optional[str] = wm.get("last_processed_event_id")
    last_seen_ts_epoch: float = float(wm.get("last_processed_source_ts_epoch", 0.0))

    log_path = claims._claims_path(brain_root, namespace)
    overrides_path = claim_overrides._overrides_path(brain_root, namespace)

    # Resolve overrides up front; the restore set feeds materialize_state
    # so a previously-retracted-then-restored claim resurfaces.
    overrides_pre = claim_overrides.resolve_overrides(overrides_path)

    # Replay any existing claim state — we need it to detect conflicts
    # against in-progress consolidation.
    state_before = claims.materialize_state(
        log_path, restored_claim_ids=overrides_pre.restored_claim_ids,
    )

    # Walk every episodic file for this namespace.
    paths = _episodic_paths(brain_root, namespace)
    processed = 0
    schema_drop_count = 0
    new_watermark_event_id = last_seen_event_id
    new_watermark_ts = last_seen_ts_epoch

    # Stage A/B/C overrides are resolved AFTER all events are processed
    # (post-extraction marking), so we just collect new claims here.
    new_asserts: List[Tuple[str, Dict[str, Any]]] = []  # (claim_id, event_dict)
    new_supersedes: List[Tuple[str, str, str]] = []  # (old, new, reason)
    new_retracts: List[Tuple[str, str]] = []  # (claim_id, reason)

    # Track current-per-slot during this run so successive events
    # produce supersession chains correctly even before we persist them.
    running_current: Dict[Tuple[str, str], Tuple[str, float]] = {}
    for slot, cid in state_before.current.items():
        rec = state_before.claims_by_id.get(cid)
        if rec is not None:
            running_current[slot] = (cid, rec.source_ts_epoch)

    # Build a lookup of source_event_id → list of existing claim_ids
    # for tombstone routing.
    existing_claims_by_event: Dict[str, List[str]] = {}
    for rec in state_before.claims_by_id.values():
        existing_claims_by_event.setdefault(rec.source_event_id, []).append(rec.claim_id)

    # ------------------------------------------------------------------
    # Pre-pass: load all conforming events from disk, normalize their
    # timestamps, filter by watermark, then SORT by (ts, event_id)
    # before batching. [codex PR3 P1.4 fix] — without sorting, raw
    # file order leaked into watermark semantics, causing same-ts
    # events to be skipped forever if processed out of lexicographic
    # order, and earlier-ts events to be re-processed when a later-ts
    # event was seen first.
    # ------------------------------------------------------------------
    candidates: List[Tuple[float, int, str, Dict[str, Any]]] = []
    for ev in _iter_episodic_events(paths):
        result.events_total += 1
        sv = ev.get("schema_version", 1)
        if isinstance(sv, int) and sv > 1:
            schema_drop_count += 1
            continue
        if not _is_conforming(ev):
            result.events_skipped_missing_required += 1
            continue
        try:
            ts_epoch, _label = source_ts.normalize_source_ts(
                ev.get("source_ts"), fallback_iso=ev.get("ts"),
            )
        except source_ts.SourceTsRangeError:
            result.events_skipped_unparseable_ts += 1
            continue
        ev_id = ev["event_id"]
        # Watermark filter — strictly-after the watermark's
        # (source_ts, is_tombstone, event_id) tuple (matches the sort
        # key below; see [codex PR3 new-edge fix] comment).
        kind_v = ev.get("kind", "")
        is_tombstone = (1 if kind_v in (KIND_TOMBSTONE_DELETED, KIND_TOMBSTONE_EDITED)
                        else 0)
        if last_seen_event_id is not None:
            last_kind_marker = wm.get("last_processed_is_tombstone", 0)
            if ts_epoch < last_seen_ts_epoch:
                continue
            if ts_epoch == last_seen_ts_epoch:
                if is_tombstone < last_kind_marker:
                    continue
                if (is_tombstone == last_kind_marker
                        and ev_id <= last_seen_event_id):
                    continue
        candidates.append((ts_epoch, is_tombstone, ev_id, ev))
    # Sort: (ts, is_tombstone, event_id). At the same ts, non-
    # tombstones (0) sort BEFORE tombstones (1). [codex PR3 new-edge
    # fix.] This guarantees that across batch boundaries the target
    # event is processed strictly before its tombstone, so a tombstone
    # can never advance the watermark past its un-asserted target.
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))

    # Tombstone events are deferred to a post-pass so they can see ALL
    # asserts processed in this run regardless of sort order
    # [codex PR3 new-edge fix]. Without this, a tombstone-deleted that
    # sorts BEFORE its target (same ts, lex-smaller event_id) would
    # never find the target to retract.
    deferred_tombstone_deleted: List[Tuple[str, float, Dict[str, Any]]] = []
    # Tombstone-edited: extract body in the main loop (so running_current
    # stays correct for in-batch conflict detection), but defer the
    # target-supersede lookup so it can see late-arriving asserts.
    deferred_tombstone_edited: List[Tuple[str, str, str, str]] = []
    # Tuple: (target_event_id, new_claim_id, topic_key, claim_subject)

    # Preserve any prior tombstone-marker so a no-op rerun does NOT
    # downgrade the watermark from 1 → 0 [codex PR3 fifth-pass fix].
    last_is_tombstone_marker = int(wm.get("last_processed_is_tombstone", 0))
    for ts_epoch, is_tombstone, ev_id, ev in candidates:
        result.events_conforming += 1
        processed += 1
        last_is_tombstone_marker = is_tombstone

        kind = ev.get("kind", "")

        if kind == KIND_TOMBSTONE_DELETED:
            # Defer to a post-pass; cannot resolve targets until all
            # asserts in this batch are queued.
            target = ev.get("supersedes_event_id")
            if not target:
                result.events_skipped_missing_required += 1
                continue
            deferred_tombstone_deleted.append((ev_id, ts_epoch, ev))

        elif kind == KIND_TOMBSTONE_EDITED:
            target = ev.get("supersedes_event_id")
            if not target:
                # Malformed tombstone-edited: count + skip routing.
                # The body is still extracted as a regular event below.
                result.events_skipped_missing_required += 1
                target = None
            # An edited tombstone re-emits the body with the corrected
            # content as a NEW event. We extract on the new body AND we
            # supersede every prior claim derived from the target.
            for extractor in extractors:
                extracted = extractor.extract(ev)
                for c in extracted:
                    cid = claims.compute_claim_id(
                        c.topic_key, c.claim_subject, ev_id,
                    )
                    fp = claims.compute_value_fingerprint(
                        c.topic_key, c.claim_subject, c.value_normalized,
                    )
                    new_asserts.append((cid, {
                        "claim_id": cid,
                        "claim_value_fingerprint": fp,
                        "topic_key": c.topic_key,
                        "claim_subject": c.claim_subject,
                        "value_normalized": c.value_normalized,
                        "value_raw": c.value_raw,
                        "source_event_id": ev_id,
                        "source": ev.get("source", ""),
                        "source_ts_epoch": ts_epoch,
                    }))
                    if target:
                        # Defer target lookup to post-pass so we see
                        # late-arriving asserts [codex PR3 new-edge fix].
                        deferred_tombstone_edited.append(
                            (target, cid, c.topic_key, c.claim_subject)
                        )
                    # Update running_current.
                    slot = (c.topic_key, c.claim_subject)
                    cur = running_current.get(slot)
                    if cur is None or ts_epoch > cur[1]:
                        running_current[slot] = (cid, ts_epoch)
        else:
            # Normal event — run all extractors, append claims, route
            # conflicts.
            for extractor in extractors:
                extracted = extractor.extract(ev)
                for c in extracted:
                    cid = claims.compute_claim_id(
                        c.topic_key, c.claim_subject, ev_id,
                    )
                    fp = claims.compute_value_fingerprint(
                        c.topic_key, c.claim_subject, c.value_normalized,
                    )
                    new_asserts.append((cid, {
                        "claim_id": cid,
                        "claim_value_fingerprint": fp,
                        "topic_key": c.topic_key,
                        "claim_subject": c.claim_subject,
                        "value_normalized": c.value_normalized,
                        "value_raw": c.value_raw,
                        "source_event_id": ev_id,
                        "source": ev.get("source", ""),
                        "source_ts_epoch": ts_epoch,
                    }))

                    slot = (c.topic_key, c.claim_subject)
                    cur = running_current.get(slot)
                    if cur is None:
                        running_current[slot] = (cid, ts_epoch)
                        continue
                    cur_cid, cur_ts = cur
                    # Conflict detection: same fingerprint → same fact;
                    # no supersession. Different fingerprint → supersede
                    # the older value.
                    cur_rec = state_before.claims_by_id.get(cur_cid)
                    cur_fp = (cur_rec.claim_value_fingerprint
                              if cur_rec else None)
                    # New asserts may also be the cur; look there too.
                    if cur_fp is None:
                        for aid, payload in new_asserts:
                            if aid == cur_cid:
                                cur_fp = payload["claim_value_fingerprint"]
                                break
                    if cur_fp == fp:
                        # Same fingerprint — no supersession event;
                        # whichever is newer becomes running current
                        # (election decides at the end).
                        if ts_epoch > cur_ts:
                            running_current[slot] = (cid, ts_epoch)
                        continue
                    # Different fingerprint = conflict. The newer
                    # source_ts wins.
                    if ts_epoch > cur_ts:
                        new_supersedes.append(
                            (cur_cid, cid, claims.REASON_NEWER_SOURCE_TS)
                        )
                        running_current[slot] = (cid, ts_epoch)
                    else:
                        # New event is older than current — it's
                        # stale-on-arrival. Record a supersede
                        # transition with the EXISTING current as the
                        # newer side so this claim is marked
                        # superseded on insert.
                        new_supersedes.append(
                            (cid, cur_cid, claims.REASON_NEWER_SOURCE_TS)
                        )

        # Advance watermark candidates.
        new_watermark_event_id = ev_id
        new_watermark_ts = ts_epoch

        if processed >= batch_size:
            break

    # Post-pass: resolve deferred tombstone routing now that every
    # assert from this batch is queued. Sort-order independence for
    # tombstone targets [codex PR3 new-edge fix].
    for ev_id, ts_epoch, ev in deferred_tombstone_deleted:
        target = ev["supersedes_event_id"]
        all_targets = list(existing_claims_by_event.get(target, []))
        all_targets.extend(
            cid for cid, payload in new_asserts
            if payload["source_event_id"] == target
        )
        for tcid in all_targets:
            new_retracts.append((tcid, claims.REASON_TOMBSTONE_DELETED))

    for target, new_cid, topic_key, claim_subject in deferred_tombstone_edited:
        # Prior claims for the target event in the SAME slot.
        prior_from_state = [
            cid for cid in existing_claims_by_event.get(target, [])
            if (state_before.claims_by_id.get(cid)
                and state_before.claims_by_id[cid].topic_key == topic_key
                and state_before.claims_by_id[cid].claim_subject == claim_subject)
        ]
        prior_from_run = [
            aid for aid, payload in new_asserts
            if (payload["source_event_id"] == target
                and payload["topic_key"] == topic_key
                and payload["claim_subject"] == claim_subject
                and aid != new_cid)
        ]
        for tcid in prior_from_state + prior_from_run:
            new_supersedes.append((tcid, new_cid, claims.REASON_TOMBSTONE_EDITED))

    if schema_drop_count > 0:
        try:
            schema_warner(schema_drop_count)
        except Exception:  # pragma: no cover
            pass
    result.events_skipped_future_schema = schema_drop_count

    # Apply overrides (3 stages + restore overlay). Re-read the override
    # log AFTER processing episodic events — operators may have issued
    # overrides during the run; we want the latest snapshot.
    overrides = claim_overrides.resolve_overrides(overrides_path)
    overrides_applied = 0

    # Stage A: mark claims whose source_event_id is retracted.
    pending_retracts: List[Tuple[str, str]] = []
    for cid, payload in new_asserts:
        if payload["source_event_id"] in overrides.retracted_event_ids:
            if cid not in overrides.restored_claim_ids:
                pending_retracts.append((cid, claims.REASON_OPERATOR))
                overrides_applied += 1

    # Existing claim_ids whose source_event_id is in retracted set:
    for cid, payload in existing_claims_by_event.items():
        if cid in overrides.retracted_event_ids:
            for tcid in payload:
                if tcid not in overrides.restored_claim_ids:
                    # Only retract if not already.
                    if tcid not in {r for r, _ in pending_retracts}:
                        pending_retracts.append((tcid, claims.REASON_OPERATOR))
                        overrides_applied += 1

    # Stage B: explicit claim_id retracts.
    for ridc in overrides.retracted_claim_ids:
        if ridc in overrides.restored_claim_ids:
            continue
        if any(r == ridc for r, _ in pending_retracts):
            continue
        pending_retracts.append((ridc, claims.REASON_OPERATOR))
        overrides_applied += 1

    # Stage C: predicate retracts evaluated against materialized state
    # AFTER we know which claims would otherwise be current. We
    # collect them then evaluate after the dry-run write below.

    if not dry_run:
        # Persist asserts.
        for _, payload in new_asserts:
            claims.append_assert(log_path, **payload)
            result.claims_asserted += 1
        # Persist supersedes.
        for old, new, reason in new_supersedes:
            claims.append_supersede(log_path, old_claim_id=old,
                                     new_claim_id=new, reason=reason)
            result.supersedes_appended += 1
        # Persist explicit retracts from tombstones.
        for cid, reason in new_retracts:
            claims.append_retract(log_path, claim_id=cid, reason=reason)
            result.retracts_appended += 1
        # Persist override-driven retracts (skip the restored set).
        for cid, reason in pending_retracts:
            if cid in overrides.restored_claim_ids:
                continue
            claims.append_retract(log_path, claim_id=cid, reason=reason)
            result.retracts_appended += 1
        # Re-materialize and apply predicate retracts.
        state_mid = claims.materialize_state(
            log_path, restored_claim_ids=overrides.restored_claim_ids,
        )
        for pred in overrides.retracted_predicates:
            for cid, rec in state_mid.claims_by_id.items():
                if cid in overrides.restored_claim_ids:
                    continue
                if (rec.stance != claims.STANCE_TOMBSTONE
                        and pred.matches(rec.topic_key, rec.claim_subject,
                                          rec.value_normalized)):
                    claims.append_retract(
                        log_path, claim_id=cid, reason=claims.REASON_OPERATOR,
                    )
                    result.retracts_appended += 1
                    overrides_applied += 1

        # Re-election after override application: for each slot whose
        # current changed, append a synthetic supersede event with the
        # canonical reason so the materialized state advances on the
        # next replay.
        state_after_overrides = claims.materialize_state(
            log_path, restored_claim_ids=overrides.restored_claim_ids,
        )
        for slot, new_cur in state_after_overrides.current.items():
            prior_cur = state_before.current.get(slot)
            if prior_cur and prior_cur != new_cur:
                # An override may have flipped the current; ensure the
                # transition is in the log.
                claims.append_supersede(
                    log_path, old_claim_id=prior_cur,
                    new_claim_id=new_cur,
                    reason=claims.REASON_OVERRIDE_RE_ELECTION,
                )
                result.re_election_supersedes += 1

        # Watermark — write only when we actually processed at least
        # one event. A no-op rerun must not rewrite the watermark; it
        # would risk downgrading the is_tombstone marker (codex fifth-
        # pass edge) and creates pointless I/O.
        if processed > 0 and new_watermark_event_id is not None:
            _write_watermark(wm_path, new_watermark_event_id,
                             new_watermark_ts, now_iso,
                             last_is_tombstone=last_is_tombstone_marker)

        result.final_state = claims.materialize_state(
            log_path, restored_claim_ids=overrides.restored_claim_ids,
        )

        # Project to markdown so `recall query` can discover the claims.
        # Reconciliation is idempotent — running it on a no-op re-run
        # writes nothing.
        proj = projection.project_to_markdown_reconcile(
            result.final_state, brain_root, namespace,
        )
        result.projection_written = proj.written
        result.projection_unchanged = proj.skipped_unchanged
        result.projection_orphans_deleted = proj.deleted_orphans
    else:
        # Dry-run: report what would happen without writing.
        result.claims_asserted = len(new_asserts)
        result.supersedes_appended = len(new_supersedes)
        result.retracts_appended = len(new_retracts) + len(pending_retracts)
        result.final_state = state_before

    result.overrides_applied = overrides_applied
    return result
