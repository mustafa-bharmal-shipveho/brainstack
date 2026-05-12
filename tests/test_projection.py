"""Tests for projection.py — the claim → markdown reconciler.

The projection is what makes the recall query path see claims. Each
ClaimRecord in materialized state becomes one .md file under
<brain>/memory/semantic/claims/<claim_id>.md with frontmatter encoding
the stance (current/stale/tombstone).
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import claims
import claim_overrides
import consolidate
import projection
import topic_keys


# --- Fixtures ---------------------------------------------------------

def _make_brain(tmp_path) -> Path:
    brain = tmp_path / ".agent"
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "semantic").mkdir()
    return brain


def _append_episodic(brain: Path, event: dict) -> None:
    p = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    with p.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _event(*, source, event_id, body, source_ts, kind="manual-note", **extra):
    ev = {
        "schema_version": 1, "ts": "2026-01-01T00:00:00Z",
        "kind": kind, "source": source, "event_id": event_id,
        "source_ts": source_ts, "body_redacted": body,
    }
    ev.update(extra)
    return ev


def _read_frontmatter(md_path: Path) -> dict:
    """Parse YAML frontmatter — strict ---/---/body shape."""
    import yaml
    text = md_path.read_text()
    assert text.startswith("---\n"), f"missing frontmatter: {md_path}"
    end = text.index("---\n", 4)
    fm_text = text[4:end]
    return yaml.safe_load(fm_text)


# --- Basics -----------------------------------------------------------

def test_current_claim_projects_with_type_claim_current(tmp_path):
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-20",
    ))
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "rn:1")
    md_path = Path(projection._claims_dir(str(brain))) / f"{cid}.md"
    assert md_path.exists()
    fm = _read_frontmatter(md_path)
    assert fm["type"] == "claim-current"
    assert fm["topic_key"] == "project:ps2"
    assert fm["claim_subject"] == "release-date"
    assert fm["stance"] == "current"
    assert fm["source"] == "research-notes"
    assert fm["value_normalized"] == "2026-05-20"


def test_default_projection_excludes_stale_claims(tmp_path):
    """Codex PR4 P1 fix: default projection only writes CURRENT claims.
    Stale claims are audit-only and would otherwise pollute default
    `recall query` results."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:2",
        source_ts="1700000100.0", body="PS2 launches on 2026-05-20",
    ))
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    old = claims.compute_claim_id("project:ps2", "release-date", "rn:1")
    new = claims.compute_claim_id("project:ps2", "release-date", "rn:2")
    out = Path(projection._claims_dir(str(brain)))
    # Only the current claim's .md should be on disk.
    assert (out / f"{new}.md").exists()
    assert not (out / f"{old}.md").exists()


def test_default_projection_excludes_tombstone_claims(tmp_path):
    """Tombstone claims are not projected by default — the slot has no
    current claim, so nothing surfaces in recall."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="discord", event_id="dc:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="discord", event_id="dc:tomb",
        source_ts="1700000100.0",
        kind="tombstone-deleted",
        supersedes_event_id="dc:1",
        body="(deleted)",
    ))
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "dc:1")
    out = Path(projection._claims_dir(str(brain)))
    assert not (out / f"{cid}.md").exists()


def test_include_stale_opt_in_projects_stale_and_tombstone(tmp_path):
    """Calling project_to_markdown_reconcile with include_stale=True
    writes stale + tombstone .md files (used by future --include-
    superseded recall flag)."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-18",
    ))
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:2",
        source_ts="1700000100.0", body="PS2 launches on 2026-05-20",
    ))
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    state = claims.materialize_state(claims._claims_path(str(brain)))
    projection.project_to_markdown_reconcile(
        state, str(brain), include_stale=True,
    )
    old = claims.compute_claim_id("project:ps2", "release-date", "rn:1")
    out = Path(projection._claims_dir(str(brain))) / f"{old}.md"
    fm = _read_frontmatter(out)
    assert fm["type"] == "claim-stale"
    assert fm["stance"] == "superseded"
    assert fm["superseded_by"] is not None


def test_projection_writes_value_raw_as_body(tmp_path):
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0",
        body="MYPROJ launches on 2026-05-20 with some context around it",
    ))
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    cid = claims.compute_claim_id("project:myproj", "release-date", "rn:1")
    md = Path(projection._claims_dir(str(brain))) / f"{cid}.md"
    text = md.read_text()
    body_start = text.index("---\n", 4) + 4
    body = text[body_start:].strip()
    assert "2026-05-20" in body


# --- Reconciliation semantics ---------------------------------------

def test_idempotent_rerun_writes_no_new_files(tmp_path):
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="research-notes", event_id="rn:1",
        source_ts="1700000000.0", body="PS2 launches on 2026-05-20",
    ))
    cfg = topic_keys.ExtractorConfig()
    r1 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    assert r1.projection_written >= 1
    r2 = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    # No new events processed → no new projection writes.
    assert r2.projection_written == 0
    assert r2.projection_orphans_deleted == 0


def test_byte_stable_rewrite_does_not_change_mtime(tmp_path):
    """Re-running the projection on identical state must not touch
    existing files (recall index uses mtime for refresh)."""
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
    md_path = Path(projection._claims_dir(str(brain))) / f"{cid}.md"
    mtime_first = md_path.stat().st_mtime
    time.sleep(0.05)
    # Directly re-project from current state.
    state = claims.materialize_state(claims._claims_path(str(brain)))
    projection.project_to_markdown_reconcile(state, str(brain))
    mtime_second = md_path.stat().st_mtime
    assert mtime_first == mtime_second, (
        "byte-identical write should not bump mtime"
    )


def test_orphan_claim_file_deleted_on_next_run(tmp_path):
    """If a claim_id disappears from materialized state (e.g. log
    re-built from scratch), its old projection file is removed."""
    brain = _make_brain(tmp_path)
    out_dir = Path(projection._claims_dir(str(brain)))
    out_dir.mkdir(parents=True, exist_ok=True)
    # Plant a stray .md file that no claim_id will produce.
    stray = out_dir / "stray-orphan.md"
    stray.write_text("---\ntype: claim-current\n---\nfoo\n")
    # Run consolidation on an empty episodic file — produces zero claims.
    cfg = topic_keys.ExtractorConfig()
    r = consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    # Orphan must be deleted.
    assert not stray.exists()
    assert r.projection_orphans_deleted >= 1


def test_restore_after_retract_resurrects_projection_file(tmp_path):
    """End-to-end with default projection (current-only): retract
    → file disappears; restore → file reappears with type=claim-current.
    Stale/tombstone .md files would pollute default recall query, so
    they're not projected at all by default."""
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
    md_path = Path(projection._claims_dir(str(brain))) / f"{cid}.md"
    assert md_path.exists()
    # Retract.
    ov_path = claim_overrides._overrides_path(str(brain))
    claim_overrides.retract_by_claim_id(ov_path, claim_id=cid)
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    assert not md_path.exists(), "tombstone'd claim must not be in default projection"
    # Restore.
    claim_overrides.restore_by_claim_id(ov_path, claim_id=cid)
    consolidate.run_consolidation(
        str(brain), extractors=[topic_keys.HeuristicExtractor(cfg)],
    )
    assert md_path.exists()
    fm = _read_frontmatter(md_path)
    assert fm["type"] == "claim-current"


# --- Namespace handling ---------------------------------------------

def test_per_namespace_projection_path():
    """Default namespace → memory/semantic/claims; named namespace →
    memory/semantic/<ns>/claims."""
    assert projection._claims_dir("/x", "default").endswith(
        "memory/semantic/claims"
    )
    assert projection._claims_dir("/x", "inbox").endswith(
        "memory/semantic/inbox/claims"
    )


def test_invalid_namespace_rejected():
    with pytest.raises(ValueError):
        projection._claims_dir("/x", "../etc")
    with pytest.raises(ValueError):
        projection._claims_dir("/x", "UPPER")


# --- Framework shape -------------------------------------------------

def test_no_producer_branching_in_projection_module():
    src = (REPO_ROOT / "agent" / "memory" / "projection.py").read_text()
    for name in ("slack", "gmail", "agentry", "discord", "calendar",
                 "teams", "research-notes", "nbeditor"):
        for bad in (f'"{name}" ==', f'== "{name}"',
                    f"'{name}' ==", f"== '{name}'",
                    f'in ["{name}"', f'in ("{name}"',
                    f"in ['{name}'", f"in ('{name}'"):
            assert bad not in src, (
                f"projection.py has producer-name branch: {bad!r}"
            )


def test_projection_works_with_synthetic_unknown_producer(tmp_path):
    """End-to-end with a producer the framework has never seen."""
    brain = _make_brain(tmp_path)
    _append_episodic(brain, _event(
        source="fictitious-future-producer-9000",
        event_id="ffp:1",
        source_ts="2026-05-12T10:00:00Z",
        body="PS2 launches on 2026-05-22",
    ))
    consolidate.run_consolidation(
        str(brain),
        extractors=[topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())],
    )
    cid = claims.compute_claim_id("project:ps2", "release-date", "ffp:1")
    md = Path(projection._claims_dir(str(brain))) / f"{cid}.md"
    fm = _read_frontmatter(md)
    assert fm["source"] == "fictitious-future-producer-9000"
    assert fm["type"] == "claim-current"
