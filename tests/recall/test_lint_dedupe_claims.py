"""Tests for `recall lint --dedupe-claims` (recall/lint_claims.py).

Why deleting a claim file is not enough
---------------------------------------
The `.md` files under `memory/semantic/claims/` are a PROJECTION of
`claims.jsonl`: every consolidation run rebuilds the directory and deletes
orphans. Move a claim file to `archived/` without retracting the claim and
the next dream cycle recreates it. The sticky mechanism is the operator
override log — `memory/semantic/claim_overrides.jsonl` — which survives
even `rm claims.jsonl`.

So an archive here is three writes that must all succeed before the
original is unlinked:

  1. an archived copy carrying a tombstone note and `needs_review: true`
  2. a `retract` row in `claim_overrides.jsonl`, byte-identical to what
     `claim_overrides.retract_by_claim_id` produces
  3. only then, `os.unlink` of the original

If any step fails, the original stays. Losing a claim is worse than
keeping a duplicate.

Rules pinned here
-----------------
  - duplicates: files sharing a `source_event_id`, keeping the oldest
  - `stub_slack_point`: a body that is nothing but a Slack permalink
  - `stub_unknown_value`: `value_normalized: unknown` — a claim with no
    content
  - `stub_short_body`: OFF by default (`STUB_MIN_CHARS == 0`). Claims are
    one-liners by construction (median body 65 chars), so length alone is
    not a content-free signal; the rule exists only behind an explicit
    `--stub-min-chars N`.

`recall.lint_claims` does not exist yet; the `lc` fixture imports it
lazily so this module still COLLECTS in the red phase.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall import lint
from recall.cli import app
from recall.frontmatter import parse_path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# agent/memory modules import each other by bare top-level name (projection
# does `import claims`), so the dir has to be ON sys.path — a
# spec_from_file_location load of any one of them fails on its siblings.
# Move-to-front, not skip-if-present: agent/tools is prepended by 17 other
# test modules and ships a different `promote.py`, so merely being present
# is not enough for the agent/memory tree to resolve consistently.
_AGENT_MEMORY = str(REPO_ROOT / "agent" / "memory")
while _AGENT_MEMORY in sys.path:
    sys.path.remove(_AGENT_MEMORY)
sys.path.insert(0, _AGENT_MEMORY)

import claim_overrides  # noqa: E402


@pytest.fixture
def lc():
    """The module under test. Imported lazily — see module docstring."""
    import recall.lint_claims

    return recall.lint_claims


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Fixture — claim files in the exact shape agent/memory/projection.py writes
# ---------------------------------------------------------------------------

E1 = "slack:D0AEUU2CX7E:1781028431.557709"
E2 = "slack:D0AEUU2CX7E:1781028900.111111"
SLACK_POINT = ("point: <https://example.slack.com/archives/C0AEUU2CX7E/"
               "p1781028431557709>")
LONG = ("The team agreed to hold the release until the migration backfill "
        "finishes, and to re-run the capacity model afterwards so the "
        "downstream forecast reflects the new baseline rather than the "
        "pre-migration numbers everyone has been quoting all quarter.")

assert len(LONG) > 200  # keeps the opt-in short-body rule from firing on it


def _claim_text(*, claim_id: str, topic_key: str, claim_subject: str,
                source_event_id: str | None, source_ts_epoch: float,
                value_normalized: str, body: str) -> str:
    fm = [
        f"claim_id: {claim_id}",
        f"claim_subject: {claim_subject}",
        f'description: "{value_normalized}"',
        f'name: "{topic_key} / {claim_subject}"',
        "source: slack",
    ]
    if source_event_id is not None:
        fm.append(f"source_event_id: {source_event_id}")
    fm += [
        f"source_ts_epoch: {source_ts_epoch}",
        "stance: current",
        "superseded_by: null",
        f"topic_key: {topic_key}",
        "type: claim-current",
        f'value_normalized: "{value_normalized}"',
    ]
    return "---\n" + "\n".join(fm) + "\n---\n\n" + body + "\n"


def _write_claim(claims_dir: Path, **kw) -> Path:
    """Write one claim at `<claim_id>.md` — the name the projection uses."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    path = claims_dir / f"{kw['claim_id']}.md"
    path.write_text(_claim_text(**kw), encoding="utf-8")
    return path


@pytest.fixture
def claims_brain(tmp_path: Path) -> dict:
    """A memory root holding ten claims that cover every rule.

    Returns a dict of role → Path plus `root`, so tests name files by what
    they are rather than by a 64-hex filename.
    """
    memory = tmp_path / ".agent" / "memory"
    cdir = memory / "semantic" / "claims"

    ids = {role: c * 64 for role, c in {
        "keep_oldest": "1", "dup_mid": "2", "dup_new": "3",
        "slack_keep": "4", "slack_dup": "5", "short": "6",
        "unknown": "7", "orphan": "8", "legit": "9", "mentions": "a",
    }.items()}

    files = {
        # --- three claims from ONE Slack message (person / project / channel
        #     topic keys). Oldest source_ts wins.
        "keep_oldest": _write_claim(
            cdir, claim_id=ids["keep_oldest"],
            topic_key="person:dana", claim_subject="release-stance",
            source_event_id=E1, source_ts_epoch=100.0,
            value_normalized="holding the release", body=LONG),
        "dup_mid": _write_claim(
            cdir, claim_id=ids["dup_mid"],
            topic_key="project:ps2", claim_subject="release-date",
            source_event_id=E1, source_ts_epoch=200.0,
            value_normalized="holding the release", body=LONG),
        "dup_new": _write_claim(
            cdir, claim_id=ids["dup_new"],
            topic_key="channel:releases", claim_subject="release-date",
            source_event_id=E1, source_ts_epoch=300.0,
            value_normalized="holding the release", body=LONG),

        # --- the two Slack "point:" stubs, sharing one event id. The keeper
        #     is itself a stub, so stub checks must run on keepers too.
        "slack_keep": _write_claim(
            cdir, claim_id=ids["slack_keep"],
            topic_key="person:dana", claim_subject="link",
            source_event_id=E2, source_ts_epoch=400.0,
            value_normalized="point", body=SLACK_POINT),
        "slack_dup": _write_claim(
            cdir, claim_id=ids["slack_dup"],
            topic_key="channel:releases", claim_subject="link",
            source_event_id=E2, source_ts_epoch=500.0,
            value_normalized="point", body=SLACK_POINT),

        # --- a real, short, one-line claim. NOT junk.
        "short": _write_claim(
            cdir, claim_id=ids["short"],
            topic_key="person:dana", claim_subject="due-date",
            source_event_id="slack:X:600", source_ts_epoch=600.0,
            value_normalized="due on June 1st", body="due on June 1st"),

        # --- a claim the extractor could not resolve to a value.
        "unknown": _write_claim(
            cdir, claim_id=ids["unknown"],
            topic_key="person:dana", claim_subject="status",
            source_event_id="slack:X:700", source_ts_epoch=700.0,
            value_normalized="unknown", body=LONG),

        # --- no source_event_id at all: not groupable, must be left alone.
        "orphan": _write_claim(
            cdir, claim_id=ids["orphan"],
            topic_key="person:dana", claim_subject="note",
            source_event_id=None, source_ts_epoch=800.0,
            value_normalized="handwritten note", body=LONG),

        # --- an ordinary healthy claim.
        "legit": _write_claim(
            cdir, claim_id=ids["legit"],
            topic_key="project:ps2", claim_subject="owner",
            source_event_id="slack:X:900", source_ts_epoch=900.0,
            value_normalized="dana owns it", body=LONG),

        # --- negative control for the Slack regex: mentions a permalink but
        #     is not a bare permalink.
        "mentions": _write_claim(
            cdir, claim_id=ids["mentions"],
            topic_key="project:ps2", claim_subject="thread",
            source_event_id="slack:X:1000", source_ts_epoch=1000.0,
            value_normalized="discussed in thread",
            body="We discussed it at <https://example.slack.com/archives/C1/p1> "
                 "during standup, and " + LONG),
    }
    files["root"] = memory
    files["claims_dir"] = cdir
    files["archived_dir"] = memory / "semantic" / "archived"
    files["overrides"] = memory / "semantic" / "claim_overrides.jsonl"
    files["ids"] = ids
    return files


FIXED_NOW = datetime(2026, 9, 4, 15, 4, 5, tzinfo=timezone.utc)


def _actions_by_file(actions) -> dict:
    return {a.file.name: a for a in actions}


def _reason_for(actions, path: Path) -> str | None:
    for a in actions:
        if a.file == path:
            return a.reason
    return None


def _break_atomic_write(monkeypatch, lc):
    """Make the archive write fail regardless of how lint_claims imported
    the helper (`from recall.lint import _atomic_write` or `lint._atomic_write`)."""
    monkeypatch.setattr(lint, "_atomic_write", lambda *a, **k: False)
    monkeypatch.setattr(lc, "_atomic_write", lambda *a, **k: False, raising=False)


# ---------------------------------------------------------------------------
# Root resolution + module constants
# ---------------------------------------------------------------------------

class TestSetup:
    def test_claims_dir_accepts_memory_root_and_brain_root(self, lc, claims_brain):
        expected = claims_brain["claims_dir"]
        assert lc.claims_dir(claims_brain["root"]) == expected
        assert lc.claims_dir(claims_brain["root"].parent) == expected

    def test_claims_dir_returns_none_when_absent(self, lc, tmp_path):
        assert lc.claims_dir(tmp_path / "nope") is None

    def test_plan_on_root_without_claims_is_empty(self, lc, tmp_path):
        assert lc.plan_claim_dedupe(tmp_path) == []

    def test_short_body_rule_is_off_by_default(self, lc):
        """40 of 44 live claims are under 200 chars and most are legitimate
        one-liners ("due on June 1st"). Length is not a content-free
        signal, so the rule ships disabled."""
        assert lc.STUB_MIN_CHARS == 0

    def test_action_field_names_are_the_json_contract(self, lc):
        names = [f.name for f in dataclasses.fields(lc.ClaimAction)]
        assert names == ["file", "claim_id", "source_event_id", "reason",
                         "keep", "detail"]


# ---------------------------------------------------------------------------
# Planning rules
# ---------------------------------------------------------------------------

class TestPlanRules:
    def test_groups_by_source_event_id_keeps_oldest(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        assert _reason_for(actions, claims_brain["keep_oldest"]) is None
        for role in ("dup_mid", "dup_new"):
            a = _actions_by_file(actions)[claims_brain[role].name]
            assert a.reason == "duplicate_source_event"
            assert a.source_event_id == E1
            assert a.keep == claims_brain["keep_oldest"]

    def test_tie_on_source_ts_falls_back_to_mtime(self, lc, tmp_path):
        """Same event, same source timestamp — the file written first is
        the one that stays."""
        memory = tmp_path / "memory"
        cdir = memory / "semantic" / "claims"
        first = _write_claim(cdir, claim_id="b" * 64,
                             topic_key="person:x", claim_subject="a",
                             source_event_id="e:1", source_ts_epoch=100.0,
                             value_normalized="v", body=LONG)
        second = _write_claim(cdir, claim_id="c" * 64,
                              topic_key="person:x", claim_subject="b",
                              source_event_id="e:1", source_ts_epoch=100.0,
                              value_normalized="v", body=LONG)
        os.utime(first, (1_000_000, 1_000_000))
        os.utime(second, (2_000_000, 2_000_000))

        actions = lc.plan_claim_dedupe(memory)
        assert _reason_for(actions, first) is None
        assert _reason_for(actions, second) == "duplicate_source_event"

    def test_slack_point_regex_matches_a_bare_permalink(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        a = _actions_by_file(actions)[claims_brain["slack_keep"].name]
        assert a.reason == "stub_slack_point"
        assert "point:" in a.detail

    def test_slack_regex_does_not_match_a_mentioned_permalink(
            self, lc, claims_brain):
        """Anchored at the start of the body: a claim that merely cites a
        Slack thread is real content."""
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        assert _reason_for(actions, claims_brain["mentions"]) is None

    def test_unknown_value_normalized_is_a_stub(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        a = _actions_by_file(actions)[claims_brain["unknown"].name]
        assert a.reason == "stub_unknown_value"

    def test_short_legit_claim_is_untouched_by_default(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        assert _reason_for(actions, claims_brain["short"]) is None

    def test_short_body_threshold_respects_the_flag(self, lc, claims_brain):
        flagged = lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=200)
        assert _reason_for(flagged, claims_brain["short"]) == "stub_short_body"
        assert "15" in _actions_by_file(flagged)[claims_brain["short"].name].detail

        loose = lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=10)
        assert _reason_for(loose, claims_brain["short"]) is None

    def test_precedence_duplicate_over_stub(self, lc, claims_brain):
        """The younger Slack stub is BOTH a duplicate and a stub. It is
        reported once, as a duplicate, so the manifest tells the operator
        why it is safe to drop (a sibling survives)."""
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        a = _actions_by_file(actions)[claims_brain["slack_dup"].name]
        assert a.reason == "duplicate_source_event"
        assert a.keep == claims_brain["slack_keep"]

    def test_one_action_per_file(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=200)
        seen = [a.file for a in actions]
        assert len(seen) == len(set(seen))

    def test_missing_source_event_id_is_never_actioned(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=200)
        assert _reason_for(actions, claims_brain["orphan"]) is None

    def test_healthy_claim_is_never_actioned(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=200)
        assert _reason_for(actions, claims_brain["legit"]) is None

    def test_expected_action_set(self, lc, claims_brain):
        by_reason = {}
        for a in lc.plan_claim_dedupe(claims_brain["root"]):
            by_reason.setdefault(a.reason, set()).add(a.file.name)
        assert by_reason == {
            "duplicate_source_event": {claims_brain["dup_mid"].name,
                                       claims_brain["dup_new"].name,
                                       claims_brain["slack_dup"].name},
            "stub_slack_point": {claims_brain["slack_keep"].name},
            "stub_unknown_value": {claims_brain["unknown"].name},
        }

    def test_symlink_skipped(self, lc, tmp_path):
        memory = tmp_path / "memory"
        cdir = memory / "semantic" / "claims"
        cdir.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text(_claim_text(
            claim_id="d" * 64, topic_key="p:x", claim_subject="s",
            source_event_id="e:9", source_ts_epoch=1.0,
            value_normalized="unknown", body=LONG), encoding="utf-8")
        (cdir / "linked.md").symlink_to(outside)
        assert lc.plan_claim_dedupe(memory) == []


# ---------------------------------------------------------------------------
# Apply — archive, retract, unlink
# ---------------------------------------------------------------------------

class TestApply:
    def test_dry_run_writes_nothing(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        assert lc.apply_claim_dedupe(actions, claims_brain["root"]) == []
        assert not claims_brain["archived_dir"].exists()
        assert not claims_brain["overrides"].exists()
        for role in ("dup_mid", "dup_new", "slack_keep", "slack_dup", "unknown"):
            assert claims_brain[role].exists()

    def test_apply_moves_to_archived_with_tombstone_and_needs_review(
            self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        applied = lc.apply_claim_dedupe(
            actions, claims_brain["root"], dry_run=False, now=FIXED_NOW)

        assert len(applied) == 5
        assert all(p.parent == claims_brain["archived_dir"] for p in applied)

        cid = claims_brain["ids"]["dup_mid"]
        dest = claims_brain["archived_dir"] / f"20260904T150405-claim-{cid[:16]}.md"
        assert dest in applied
        text = dest.read_text(encoding="utf-8")

        assert "<!-- tombstone: archived by `recall lint --dedupe-claims --apply` at " in text
        assert "2026-09-04T15:04:05Z" in text
        assert "reason: duplicate_source_event" in text
        assert f"source_event_id: {E1}" in text
        assert f"kept: semantic/claims/{claims_brain['keep_oldest'].name}" in text
        assert f"original: semantic/claims/{claims_brain['dup_mid'].name}" in text
        assert ("retraction: appended to semantic/claim_overrides.jsonl "
                "(key claim_id) -->") in text

        # The archived copy keeps its frontmatter AND is demoted, so it
        # stays non-retrievable even if the config exclude is missing.
        fm = parse_path(dest).frontmatter
        assert fm["claim_id"] == cid
        assert fm["needs_review"] is True

        # Original gone; keeper untouched.
        assert not claims_brain["dup_mid"].exists()
        assert claims_brain["keep_oldest"].exists()

    def test_stub_tombstone_records_no_keeper(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        lc.apply_claim_dedupe(actions, claims_brain["root"],
                              dry_run=False, now=FIXED_NOW)
        cid = claims_brain["ids"]["unknown"]
        dest = claims_brain["archived_dir"] / f"20260904T150405-claim-{cid[:16]}.md"
        text = dest.read_text(encoding="utf-8")
        assert "reason: stub_unknown_value" in text
        assert "kept: none" in text

    def test_apply_appends_retraction_row_readable_by_claim_overrides(
            self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        lc.apply_claim_dedupe(actions, claims_brain["root"],
                              dry_run=False, now=FIXED_NOW)

        overrides = claims_brain["overrides"]
        assert overrides.exists()
        resolved = claim_overrides.resolve_overrides(str(overrides))
        archived_ids = {claims_brain["ids"][r] for r in
                        ("dup_mid", "dup_new", "slack_dup", "slack_keep", "unknown")}
        assert archived_ids <= resolved.retracted_claim_ids
        assert claims_brain["ids"]["keep_oldest"] not in resolved.retracted_claim_ids

    def test_retraction_row_is_byte_identical_to_retract_by_claim_id(
            self, lc, claims_brain, tmp_path):
        """The consolidator only honours rows it recognises. Diverge on a
        single key and the projection quietly recreates the claim."""
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        lc.apply_claim_dedupe(actions, claims_brain["root"],
                              dry_run=False, now=FIXED_NOW)

        cid = claims_brain["ids"]["dup_mid"]
        rows = [json.loads(ln) for ln in
                claims_brain["overrides"].read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        ours = next(r for r in rows if r["claim_id"] == cid)

        reference_path = tmp_path / "reference.jsonl"
        claim_overrides.retract_by_claim_id(
            str(reference_path), claim_id=cid, actor=ours["actor"],
            note=ours["note"])
        reference = json.loads(reference_path.read_text().splitlines()[0])

        assert set(ours) == set(reference)
        assert {k: v for k, v in ours.items() if k != "at"} == \
               {k: v for k, v in reference.items() if k != "at"}
        assert ours["actor"] == "recall-lint-dedupe"
        assert ours["note"] == \
            f"duplicate_source_event; kept={claims_brain['ids']['keep_oldest']}"
        assert ours["schema_version"] == claim_overrides.CURRENT_SCHEMA

        # One canonical line per row: json.dumps(..., sort_keys=True).
        raw = [ln for ln in
               claims_brain["overrides"].read_text(encoding="utf-8").splitlines()
               if ln.strip()]
        assert json.dumps(ours, sort_keys=True) in raw

    def test_stub_retraction_note_says_kept_none(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        lc.apply_claim_dedupe(actions, claims_brain["root"],
                              dry_run=False, now=FIXED_NOW)
        rows = [json.loads(ln) for ln in
                claims_brain["overrides"].read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        row = next(r for r in rows if r["claim_id"] == claims_brain["ids"]["unknown"])
        assert row["note"] == "stub_unknown_value; kept=none"

    def test_apply_never_deletes_when_archive_write_fails(
            self, lc, claims_brain, monkeypatch):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        _break_atomic_write(monkeypatch, lc)

        applied = lc.apply_claim_dedupe(
            actions, claims_brain["root"], dry_run=False, now=FIXED_NOW)

        assert applied == []
        for role in ("dup_mid", "dup_new", "slack_keep", "slack_dup", "unknown"):
            assert claims_brain[role].exists(), f"{role} deleted without an archive"
        assert not claims_brain["overrides"].exists()

    def test_apply_never_deletes_when_the_retraction_fails(
            self, lc, claims_brain, monkeypatch):
        def boom(*a, **k):
            raise OSError("override log unwritable")

        monkeypatch.setattr(lc, "_append_override_retract", boom)
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        applied = lc.apply_claim_dedupe(
            actions, claims_brain["root"], dry_run=False, now=FIXED_NOW)

        assert applied == []
        for role in ("dup_mid", "dup_new", "slack_keep", "slack_dup", "unknown"):
            assert claims_brain[role].exists(), \
                f"{role} deleted with no retraction — the projection will resurrect it"

    def test_apply_is_idempotent(self, lc, claims_brain):
        first = lc.plan_claim_dedupe(claims_brain["root"])
        lc.apply_claim_dedupe(first, claims_brain["root"],
                              dry_run=False, now=FIXED_NOW)
        assert lc.plan_claim_dedupe(claims_brain["root"]) == []

    def test_archived_copies_are_not_re_planned(self, lc, claims_brain):
        """`semantic/archived/` is outside the claims dir, so the archived
        duplicates never re-enter the grouping."""
        lc.apply_claim_dedupe(lc.plan_claim_dedupe(claims_brain["root"]),
                              claims_brain["root"], dry_run=False, now=FIXED_NOW)
        assert len(list(claims_brain["archived_dir"].glob("*.md"))) == 5
        assert lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=0) == []

    def test_survivors_after_apply(self, lc, claims_brain):
        lc.apply_claim_dedupe(lc.plan_claim_dedupe(claims_brain["root"]),
                              claims_brain["root"], dry_run=False, now=FIXED_NOW)
        remaining = {p.name for p in claims_brain["claims_dir"].glob("*.md")}
        assert remaining == {claims_brain[r].name for r in
                             ("keep_oldest", "short", "orphan", "legit", "mentions")}


# ---------------------------------------------------------------------------
# The projection must not resurrect an archived claim
# ---------------------------------------------------------------------------

def test_projection_does_not_recreate_after_retraction(lc, tmp_path):
    """End-to-end on the real claim store: assert two claims from one
    event, project them, dedupe one away, then replay the way
    consolidation does. The archived claim must NOT come back."""
    import claims
    import projection

    brain = tmp_path / ".agent"
    (brain / "memory" / "semantic").mkdir(parents=True)
    log = claims._claims_path(str(brain))
    event_id = "slack:C1:1781028431.557709"

    kept_id = claims.compute_claim_id("person:dana", "stance", event_id)
    dup_id = claims.compute_claim_id("project:ps2", "stance", event_id)
    for cid, topic, ts in ((kept_id, "person:dana", 100.0),
                           (dup_id, "project:ps2", 100.0)):
        claims.append_assert(
            log, claim_id=cid,
            claim_value_fingerprint=claims.compute_value_fingerprint(
                topic, "stance", "holding the release"),
            topic_key=topic, claim_subject="stance",
            value_normalized="holding the release", value_raw=LONG,
            source_event_id=event_id, source="slack", source_ts_epoch=ts)

    state = claims.materialize_state(log)
    projection.project_to_markdown_reconcile(state, str(brain))
    cdir = brain / "memory" / "semantic" / "claims"
    assert (cdir / f"{kept_id}.md").exists()
    assert (cdir / f"{dup_id}.md").exists()

    memory = brain / "memory"
    actions = lc.plan_claim_dedupe(memory)
    assert len(actions) == 1
    archived_id = actions[0].claim_id
    lc.apply_claim_dedupe(actions, memory, dry_run=False, now=FIXED_NOW)
    assert not (cdir / f"{archived_id}.md").exists()

    # Replay the consolidator's override stage: overrides → retract events
    # → re-materialize → reconcile.
    overrides = claim_overrides.resolve_overrides(
        claim_overrides._overrides_path(str(brain)))
    assert archived_id in overrides.retracted_claim_ids
    for cid in overrides.retracted_claim_ids:
        claims.append_retract(log, claim_id=cid, reason=claims.REASON_OPERATOR)
    final = claims.materialize_state(
        log, restored_claim_ids=overrides.restored_claim_ids)
    projection.project_to_markdown_reconcile(final, str(brain))

    assert not (cdir / f"{archived_id}.md").exists(), \
        "the projection resurrected an archived claim — the retraction did not stick"
    survivor = kept_id if archived_id != kept_id else dup_id
    assert (cdir / f"{survivor}.md").exists()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def test_dry_run_manifest_text(self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"], stub_min_chars=200)
        out = lc.render_claim_manifest(actions, claims_brain["root"])

        assert out.startswith("== recall lint --dedupe-claims ==")
        assert "stub_min_chars=200" in out
        assert "ARCHIVE" in out
        for reason in ("duplicate_source_event", "stub_slack_point",
                       "stub_unknown_value", "stub_short_body"):
            assert reason in out
        assert f"semantic/claims/{claims_brain['dup_mid'].name}" in out
        assert f"source_event_id {E1}" in out
        assert f"keep semantic/claims/{claims_brain['keep_oldest'].name}" in out
        assert "would be archived to semantic/archived/" in out
        assert "--dedupe-claims --apply" in out

    def test_manifest_paths_are_relative_to_the_memory_root(self, lc, claims_brain):
        out = lc.render_claim_manifest(
            lc.plan_claim_dedupe(claims_brain["root"]), claims_brain["root"])
        assert str(claims_brain["root"]) not in out

    def test_applied_manifest_reports_archives_and_retractions(
            self, lc, claims_brain):
        actions = lc.plan_claim_dedupe(claims_brain["root"])
        applied = lc.apply_claim_dedupe(
            actions, claims_brain["root"], dry_run=False, now=FIXED_NOW)
        out = lc.render_claim_manifest(actions, claims_brain["root"], applied=applied)
        assert ("Archived 5 claim(s) to semantic/archived/ and appended "
                "5 retraction(s) to semantic/claim_overrides.jsonl.") in out

    def test_empty_plan_manifest(self, lc, tmp_path):
        out = lc.render_claim_manifest([], tmp_path)
        assert "0 file(s) would be archived" in out


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

class TestCli:
    def test_dedupe_claims_dry_run_manifest_and_exit_1(
            self, runner, isolated_xdg, claims_brain):
        result = runner.invoke(
            app, ["lint", "--brain", str(claims_brain["root"]), "--dedupe-claims"])
        assert result.exit_code == 1, result.output
        assert "== recall lint --dedupe-claims ==" in result.output
        assert "stub_min_chars=0" in result.output
        assert "5 file(s) would be archived" in result.output
        assert not claims_brain["archived_dir"].exists()

    def test_dedupe_claims_json(self, runner, isolated_xdg, claims_brain):
        result = runner.invoke(
            app, ["lint", "--brain", str(claims_brain["root"]),
                  "--dedupe-claims", "--json"])
        assert result.exit_code == 1, result.output
        for key in ('"file"', '"claim_id"', '"source_event_id"', '"reason"',
                    '"keep"', '"detail"'):
            assert key in result.output
        assert '"duplicate_source_event"' in result.output

    def test_dedupe_claims_apply_with_stub_min_chars(
            self, runner, isolated_xdg, claims_brain):
        result = runner.invoke(
            app, ["lint", "--brain", str(claims_brain["root"]),
                  "--dedupe-claims", "--apply", "--stub-min-chars", "200"])
        assert result.exit_code == 0, result.output
        assert "Archived 6 claim(s) to semantic/archived/" in result.output
        # The explicit threshold pulls in the short one-liner too.
        assert not claims_brain["short"].exists()
        assert claims_brain["keep_oldest"].exists()
        assert claims_brain["legit"].exists()

        rerun = runner.invoke(
            app, ["lint", "--brain", str(claims_brain["root"]), "--dedupe-claims"])
        assert rerun.exit_code == 0, rerun.output
        assert "0 file(s) would be archived" in rerun.output

    def test_lint_without_the_flag_does_not_touch_claims(
            self, runner, isolated_xdg, claims_brain):
        before = {p.name: p.read_bytes()
                  for p in claims_brain["claims_dir"].glob("*.md")}
        result = runner.invoke(app, ["lint", "--brain", str(claims_brain["root"])])
        assert result.exit_code == 0, result.output
        after = {p.name: p.read_bytes()
                 for p in claims_brain["claims_dir"].glob("*.md")}
        assert after == before
        assert "--dedupe-claims" not in result.output
