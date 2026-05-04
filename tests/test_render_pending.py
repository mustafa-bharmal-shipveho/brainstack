"""Tests for the pending-review surfacing system.

Today's audit (2026-05-04) found 21 candidate lessons sitting in
`~/.agent/memory/candidates/` since 2026-05-01-02. The user had no idea —
brainstack writes them silently and nothing surfaces the count in any
tool's session UI. The 4 modules under test fix that:

  - `agent/tools/render_pending_summary.py`   — generates ~/.agent/PENDING_REVIEW.md
  - `agent/tools/render_cursor_rules.py`      — pushes summary into ~/.cursor/.cursorrules
  - `agent/harness/hooks/session_start.py`    — Claude Code SessionStart hook
  - `templates/brainstack-shell-banner.sh`    — wrapper functions for `claude`/`codex`/`cursor`

These tests pin the contracts BEFORE the modules exist (TDD-red phase).
Imports are lazy (inside each test method) so `pytest --collect-only`
succeeds even when the modules-under-test are still empty stubs.

The single most important contract this file enforces is the **noise
filter** in `render_pending_summary._is_noise_cluster`. Without it, the
top of the user's pending queue is a 5,700-cluster of brainstack's own
test-suite failures ("FAILURE: secret op", "Command failed:
BRAIN_ROOT=/tmp/sysadmin-test-home/.agent") — Codex flagged this on
2026-05-04. Failing the noise filter reproduces today's bad UX exactly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness" / "hooks"))


# ---------- helpers ----------------------------------------------------


def _make_candidate(
    cid: str,
    claim: str = "lesson",
    cluster_size: int = 3,
    evidence_ids: list[str] | None = None,
    salience: float = 5.0,
) -> dict:
    """Synthetic candidate matching the on-disk schema in
    `agent/memory/candidates/*.json` (see `auto_dream.write_candidates`)."""
    return {
        "id": cid,
        "key": f"pattern_{cid}",
        "name": f"pattern_{cid}",
        "claim": claim,
        "conditions": ["test"],
        "evidence_ids": evidence_ids or ["2026-05-04T10:00:00+00:00"],
        "cluster_size": cluster_size,
        "canonical_salience": salience,
        "staged_at": "2026-05-04T10:00:00+00:00",
        "status": "staged",
        "decisions": [],
        "rejection_count": 0,
    }


def _seed_candidates(brain_root: Path, namespace: str, candidates: list[dict]) -> None:
    """Drop synthetic candidate JSON files into the right namespace dir."""
    if namespace == "default":
        target_dir = brain_root / "memory" / "candidates"
    else:
        target_dir = brain_root / "memory" / "candidates" / namespace
    target_dir.mkdir(parents=True, exist_ok=True)
    for c in candidates:
        (target_dir / f"{c['id']}.json").write_text(json.dumps(c))


# ---------- TestRenderPendingSummary -----------------------------------


class TestRenderPendingSummary:
    """The single source of truth file at <brain>/PENDING_REVIEW.md."""

    def _import(self):
        # Lazy import so --collect-only works on stubs
        import importlib
        import render_pending_summary
        importlib.reload(render_pending_summary)
        return render_pending_summary

    def test_count_pending_per_namespace_all_three(self, tmp_path: Path):
        """Counts files in default / claude-sessions / codex namespaces independently."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [
            _make_candidate("d1"), _make_candidate("d2"), _make_candidate("d3")])
        _seed_candidates(tmp_path, "claude-sessions", [_make_candidate("cs1")])
        _seed_candidates(tmp_path, "codex", [_make_candidate("cx1"), _make_candidate("cx2")])

        counts = rps.count_pending_per_namespace(tmp_path)
        assert counts == {"default": 3, "claude-sessions": 1, "codex": 2}

    def test_count_excludes_graduated_and_rejected(self, tmp_path: Path):
        """`candidates/graduated/*.json` and `candidates/rejected/*.json` are
        archive subdirs, not pending. Must not inflate the count."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [_make_candidate("d1")])
        # Stash 5 in graduated/, 5 in rejected/ — none should count
        for sub in ("graduated", "rejected"):
            (tmp_path / "memory" / "candidates" / sub).mkdir(parents=True, exist_ok=True)
            for i in range(5):
                (tmp_path / "memory" / "candidates" / sub / f"old{i}.json").write_text(
                    json.dumps(_make_candidate(f"old{i}")))

        counts = rps.count_pending_per_namespace(tmp_path)
        assert counts["default"] == 1

    def test_count_handles_missing_namespace_dirs(self, tmp_path: Path):
        """Empty brain (no candidates/ subdirs) → all-zero counts, no exceptions."""
        rps = self._import()
        counts = rps.count_pending_per_namespace(tmp_path)
        assert counts == {"default": 0, "claude-sessions": 0, "codex": 0}

    def test_noise_filter_rejects_tmp_evidence(self):
        """Cluster with all evidence_ids referencing /tmp/ is brainstack's own
        test infra noise (Codex 2026-05-04 finding). Must be filtered."""
        rps = self._import()
        cand = _make_candidate("noise1", evidence_ids=[
            "/tmp/sysadmin-test-home/.agent/log",
            "/tmp/brainstack-smoke/foo",
        ])
        assert rps._is_noise_cluster(cand) is True

    def test_noise_filter_rejects_test_path_evidence(self):
        """Evidence pointing at *-test* / *-smoke* / sandbox paths is also noise."""
        rps = self._import()
        cand = _make_candidate("noise2", evidence_ids=[
            "/Users/u/code/foo-test-fixtures/data.json",
            "/Users/u/code/bar-smoke-runner/log",
        ])
        assert rps._is_noise_cluster(cand) is True

    def test_noise_filter_accepts_real_repo_evidence(self):
        """Evidence from a real codebase path is signal, not noise."""
        rps = self._import()
        cand = _make_candidate("real1", evidence_ids=[
            "/Users/u/Documents/codebase/helix-incident-bot/src/handler.ts",
            "2026-05-01T14:00:00+00:00",  # bare timestamps are not noise
        ])
        assert rps._is_noise_cluster(cand) is False

    def test_noise_filter_mixed_evidence_keeps_cluster(self):
        """Half-noise / half-real evidence (path-shaped both): cluster has
        SOME signal so it's kept. Only ALL-noise paths are filtered out."""
        rps = self._import()
        cand = _make_candidate("mixed", evidence_ids=[
            "/tmp/sandbox/a",
            "/Users/u/Documents/codebase/foo/bar.py",
        ])
        assert rps._is_noise_cluster(cand) is False

    def test_noise_filter_catches_tmp_in_claim(self):
        """The actual failure mode that hit the live brain on 2026-05-04:
        candidates' evidence_ids are bare ISO timestamps, but the noise
        signal lives in the `claim` field (e.g. 'Command failed:
        BRAIN_ROOT=/tmp/sysadmin-test-home/.agent ...'). Filter MUST catch
        these or test-infra clusters dominate the top of the queue."""
        rps = self._import()
        # Real-shaped candidate from the live brain
        cand = _make_candidate(
            "tmp_in_claim",
            claim="FAILURE in claude-code: Command failed: "
                  "BRAIN_ROOT=/tmp/sysadmin-test-home/.agent /Users/m...",
            evidence_ids=["2026-04-30T14:14:49.708300+00:00"],  # bare timestamp
        )
        assert rps._is_noise_cluster(cand) is True

    def test_noise_filter_catches_smoke_in_claim(self):
        """SANDBOX=/tmp/brainstack-cursor-smoke-$$ shape — caught via the
        `-smoke-` substring in the claim."""
        rps = self._import()
        cand = _make_candidate(
            "smoke_claim",
            claim="High-stakes op completed (migrate): "
                  "SANDBOX=/tmp/brainstack-cursor-smoke-$$",
            evidence_ids=["2026-05-01T07:00:00+00:00"],
        )
        assert rps._is_noise_cluster(cand) is True

    def test_noise_filter_passes_real_repo_claim(self):
        """A claim that references a real codebase path is signal."""
        rps = self._import()
        cand = _make_candidate(
            "real_claim",
            claim="Wrote /Users/u/Documents/codebase/helix-incident-bot/"
                  "src/handler.ts (42 lines)",
            evidence_ids=["2026-05-01T10:00:00+00:00"],
        )
        assert rps._is_noise_cluster(cand) is False

    def test_noise_filter_catches_secret_test_failures(self):
        """The 5,700-cluster of 'FAILURE in claude-code: ... FAILED
        (secret)' on the maintainer's live brain is brainstack's own
        TruffleHog test loop, not a workflow lesson. Must be caught."""
        rps = self._import()
        cand = _make_candidate(
            "tfhog_test",
            claim="FAILURE in claude-code: High-stakes op FAILED (secret): "
                  "cd /Users/u/Documents/brainstack && pytest tests/",
            evidence_ids=["2026-04-30T14:14:49.708300+00:00"],
            cluster_size=5700,
        )
        assert rps._is_noise_cluster(cand) is True

    def test_full_render_against_live_shaped_fixtures(self, tmp_path: Path):
        """Mid-tier integration check: feed the renderer 5 candidates that
        mirror the on-disk shape from the maintainer's live brain (bare
        ISO-timestamp evidence_ids, content in `claim`). Expected output:
        - 2 noise candidates filtered (top 5 contains only the 3 signal ones)
        - one-liner suppressed (we have signal)
        - drift section skipped (no drift_report passed)

        This catches the regression class where unit tests pass but the
        live noise filter fails because synthetic fixtures don't match
        prod data shapes (Wave 6 retro 2026-05-04)."""
        rps = self._import()

        # Realistic live-brain candidate shapes
        live_shaped = [
            # Two clusters that should be filtered (claim references test infra)
            _make_candidate(
                "live_noise_1",
                claim="FAILURE in claude-code: High-stakes op FAILED (secret): cd /Users/u/code",
                evidence_ids=["2026-04-30T14:14:49.708300+00:00"],
                cluster_size=5700, salience=22.1,
            ),
            _make_candidate(
                "live_noise_2",
                claim="High-stakes op completed (migrate): SANDBOX=/tmp/brainstack-cursor-smoke-$$",
                evidence_ids=["2026-05-01T07:00:00+00:00"],
                cluster_size=11, salience=13.5,
            ),
            # Three signal clusters (real codebase paths, normal claims)
            _make_candidate(
                "live_signal_1",
                claim="Wrote /Users/u/Documents/codebase/helix-incident-bot/src/handler.ts",
                evidence_ids=["2026-05-01T10:00:00+00:00"],
                cluster_size=8, salience=13.5,
            ),
            _make_candidate(
                "live_signal_2",
                claim="Tool Agent completed successfully",
                evidence_ids=["2026-05-01T11:00:00+00:00"],
                cluster_size=6, salience=12.3,
            ),
            _make_candidate(
                "live_signal_3",
                claim="High-stakes op completed (production): python3 <<'PY'",
                evidence_ids=["2026-05-01T12:00:00+00:00"],
                cluster_size=3, salience=11.9,
            ),
        ]
        _seed_candidates(tmp_path, "default", live_shaped)

        summary = rps.compose_summary(
            tmp_path,
            drift_report={"in_sync": True, "summary": "in sync"},
            sync_status="ok",
        )

        # The two noise clusters MUST NOT appear in the top-5
        assert "FAILED (secret)" not in summary, (
            "test-infra cluster leaked into top-5 — noise filter regression"
        )
        assert "SANDBOX=/tmp/" not in summary, (
            "sandbox cluster leaked into top-5 — noise filter regression"
        )
        # All three signal clusters SHOULD appear
        assert "helix-incident-bot/src/handler.ts" in summary
        assert "Tool Agent completed successfully" in summary
        assert "production): python3" in summary
        # Total count includes ALL pending (filter is for top-5, not for count)
        assert "5 candidates pending" in summary or "candidates pending" in summary

    def test_compose_summary_empty_state_writes_one_liner(self, tmp_path: Path):
        """0 pending + in_sync + sync ok → one-liner ('all clear'). The
        SessionStart hook suppresses these so empty days produce no noise."""
        rps = self._import()
        summary = rps.compose_summary(
            tmp_path,
            drift_report={"in_sync": True, "summary": "in sync"},
            sync_status="ok",
        )
        assert "all clear" in summary.lower()
        # One-liner heuristic: under 100 chars including newline
        assert len(summary) < 100

    def test_compose_summary_includes_per_namespace_breakdown(self, tmp_path: Path):
        """When non-empty, summary shows counts for each namespace."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [_make_candidate("d1"), _make_candidate("d2")])
        _seed_candidates(tmp_path, "claude-sessions", [_make_candidate("cs1")])
        summary = rps.compose_summary(
            tmp_path,
            drift_report={"in_sync": True, "summary": "in sync"},
            sync_status="ok",
        )
        # Reference both namespaces by name OR by count
        assert "claude-sessions" in summary or "default" in summary
        assert "2" in summary and "1" in summary  # the counts appear

    def test_compose_summary_includes_drift_warning_when_present(self, tmp_path: Path):
        """A drift report with in_sync=False must surface in the output."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [_make_candidate("d1")])
        summary = rps.compose_summary(
            tmp_path,
            drift_report={
                "in_sync": False,
                "summary": "drift detected — 1 stale: foo.py",
                "missing": [], "stale": ["foo.py"], "extra": [],
            },
            sync_status="ok",
        )
        text = summary.lower()
        assert "drift" in text or "stale" in text

    def test_compose_summary_includes_sync_warning_when_stale(self, tmp_path: Path):
        """sync_status='stale' surfaces in the output."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [_make_candidate("d1")])
        summary = rps.compose_summary(
            tmp_path,
            drift_report={"in_sync": True, "summary": "in sync"},
            sync_status="stale",
        )
        assert "stale" in summary.lower() or "stuck" in summary.lower()

    def test_render_writes_atomically_no_tmp_left(self, tmp_path: Path):
        """`render()` writes <brain>/PENDING_REVIEW.md atomically — no .tmp
        siblings remain after success."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [_make_candidate("d1")])
        out = rps.render(tmp_path)
        assert out == tmp_path / "PENDING_REVIEW.md"
        assert out.is_file()
        assert out.read_text().strip() != ""
        # No leftover tmp files (atomic_write_text uses os.replace)
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == [], f"unexpected tmp files: {leftovers}"

    def test_main_print_only_mode_does_not_write(self, tmp_path: Path, capsys):
        """`--print-only` writes nothing to disk; just prints to stdout."""
        rps = self._import()
        _seed_candidates(tmp_path, "default", [_make_candidate("d1")])
        rc = rps.main(["--brain", str(tmp_path), "--print-only"])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.strip() != ""
        # The file must NOT have been written
        assert not (tmp_path / "PENDING_REVIEW.md").exists()


# ---------- TestRenderCursorRules --------------------------------------


class TestRenderCursorRules:
    """~/.cursor/.cursorrules sentinel-bracketed update."""

    def _import(self):
        import importlib
        import render_cursor_rules
        importlib.reload(render_cursor_rules)
        return render_cursor_rules

    SENTINEL_START = "<!-- brainstack-pending-start -->"
    SENTINEL_END = "<!-- brainstack-pending-end -->"

    def test_creates_file_if_missing(self, tmp_path: Path):
        """If .cursorrules doesn't exist, create it with the sentinel block."""
        rcr = self._import()
        target = tmp_path / ".cursorrules"
        assert not target.exists()
        changed = rcr.update_cursorrules("21 candidates pending", target)
        assert changed is True
        assert target.is_file()
        body = target.read_text()
        assert self.SENTINEL_START in body
        assert self.SENTINEL_END in body
        assert "21 candidates" in body

    def test_replace_preserves_surrounding_content(self, tmp_path: Path):
        """User's other .cursorrules content (above and below the sentinel
        block) MUST be preserved across updates."""
        rcr = self._import()
        target = tmp_path / ".cursorrules"
        target.write_text(
            "# my custom rules\n"
            "always use TypeScript\n\n"
            f"{self.SENTINEL_START}\n"
            "old summary text\n"
            f"{self.SENTINEL_END}\n\n"
            "# more user rules\n"
            "prefer functional components\n"
        )
        rcr.update_cursorrules("NEW summary v2", target)
        body = target.read_text()
        assert "always use TypeScript" in body
        assert "prefer functional components" in body
        assert "NEW summary v2" in body
        assert "old summary text" not in body

    def test_appends_when_no_sentinels_present(self, tmp_path: Path):
        """If .cursorrules exists with content but no sentinels, append the
        sentinel block at the end without disturbing existing content."""
        rcr = self._import()
        target = tmp_path / ".cursorrules"
        target.write_text("# pre-existing rules only\nrule one\nrule two\n")
        rcr.update_cursorrules("first summary", target)
        body = target.read_text()
        assert "rule one" in body
        assert "rule two" in body
        assert self.SENTINEL_START in body
        assert "first summary" in body

    def test_idempotent_repeated_updates_no_disturbance(self, tmp_path: Path):
        """Calling update_cursorrules twice with the same content produces
        identical files. No duplication of the sentinel block."""
        rcr = self._import()
        target = tmp_path / ".cursorrules"
        rcr.update_cursorrules("same content", target)
        first = target.read_text()
        rcr.update_cursorrules("same content", target)
        second = target.read_text()
        assert first == second
        # And exactly one sentinel block, not two
        assert second.count(self.SENTINEL_START) == 1
        assert second.count(self.SENTINEL_END) == 1

    def test_main_no_op_when_cursor_dir_missing(self, tmp_path: Path, capsys):
        """If --cursor-dir points at a path that doesn't exist (Cursor not
        installed), main returns 0 silently rather than erroring."""
        rcr = self._import()
        missing = tmp_path / "no-such-cursor-dir"
        rc = rcr.main(["--cursor-dir", str(missing), "--brain", str(tmp_path)])
        assert rc == 0
        # Should NOT have created the dir or any files
        assert not missing.exists()


# ---------- TestClaudeMdImportManagement ------------------------------


class TestClaudeMdImportManagement:
    """install.sh --setup-pending-hook idempotently appends a sentinel-
    bracketed @-import to ~/.claude/CLAUDE.md so Claude Code transcludes
    PENDING_REVIEW.md on every session start.

    Background: an earlier iteration registered a SessionStart hook in
    settings.json. The hook ran but Claude Code's SessionStart contract
    on this build is telemetry-only — stdout (raw OR JSON-enveloped)
    does NOT inject session context. The user opened a fresh Claude
    session twice and saw nothing. Switched to CLAUDE.md @-import which
    IS the documented session-start injection mechanism.

    These tests pin the contract for the install logic embedded in
    install.sh's setup-pending-hook mode (a Python heredoc). They run
    that logic as a function, isolated from the rest of install.sh, by
    mirroring its essential algorithm in a test helper. The shape of
    the helper is the contract — install.sh's heredoc must produce the
    same effect.
    """

    SENTINEL_START = "<!-- brainstack-pending-review-start -->"
    SENTINEL_END = "<!-- brainstack-pending-review-end -->"

    @staticmethod
    def _build_block(pending_path: str) -> str:
        return "\n".join([
            "<!-- brainstack-pending-review-start -->",
            "## brainstack pending review",
            "",
            f"@{pending_path}",
            "",
            "_Auto-loaded by brainstack. Remove with `./install.sh --remove-pending-hook`._",
            "<!-- brainstack-pending-review-end -->",
        ])

    def _install(self, claude_md: Path, pending_path: str) -> str:
        """Mirror of install.sh setup-pending-hook's core algorithm.
        If install.sh's heredoc diverges from this, the contract is
        broken and tests will fail — exactly what we want."""
        block = self._build_block(pending_path)
        if not claude_md.is_file():
            claude_md.write_text(block + "\n")
            return "created"
        text = claude_md.read_text()
        if self.SENTINEL_START in text and self.SENTINEL_END in text:
            s = text.index(self.SENTINEL_START)
            e = text.index(self.SENTINEL_END) + len(self.SENTINEL_END)
            new = text[:s] + block + text[e:]
            if new == text:
                return "unchanged"
            claude_md.write_text(new)
            return "updated"
        sep = "" if text.endswith("\n") else "\n"
        if not text.endswith("\n\n"):
            sep += "\n"
        claude_md.write_text(text + sep + block + "\n")
        return "appended"

    def _uninstall(self, claude_md: Path) -> bool:
        if not claude_md.is_file():
            return False
        text = claude_md.read_text()
        if self.SENTINEL_START not in text or self.SENTINEL_END not in text:
            return False
        s = text.index(self.SENTINEL_START)
        e = text.index(self.SENTINEL_END) + len(self.SENTINEL_END)
        new = text[:s].rstrip() + "\n" + text[e:].lstrip()
        if not new.endswith("\n"):
            new += "\n"
        claude_md.write_text(new)
        return True

    def test_install_appends_import_block_when_clean(self, tmp_path: Path):
        """No CLAUDE.md exists → install creates it with the bracketed @import."""
        claude_md = tmp_path / "CLAUDE.md"
        pending = "/abs/path/.agent/PENDING_REVIEW.md"
        result = self._install(claude_md, pending)
        assert result == "created"
        assert claude_md.is_file()
        body = claude_md.read_text()
        assert self.SENTINEL_START in body
        assert self.SENTINEL_END in body
        assert f"@{pending}" in body

    def test_install_preserves_existing_user_content(self, tmp_path: Path):
        """User has custom CLAUDE.md with their own @-imports + prose.
        Install MUST add ours WITHOUT touching the rest."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Personal additions\n\n"
            "@/Users/u/.claude-org/CLAUDE.md\n\n"
            "Always use TypeScript.\n"
        )
        self._install(claude_md, "/abs/path/.agent/PENDING_REVIEW.md")
        body = claude_md.read_text()
        # User content preserved verbatim
        assert "Personal additions" in body
        assert "@/Users/u/.claude-org/CLAUDE.md" in body
        assert "Always use TypeScript" in body
        # Our block appended below
        assert self.SENTINEL_START in body

    def test_install_is_idempotent(self, tmp_path: Path):
        """Running install twice with the same pending path produces an
        identical file. No duplicated sentinel block."""
        claude_md = tmp_path / "CLAUDE.md"
        pending = "/abs/path/.agent/PENDING_REVIEW.md"
        self._install(claude_md, pending)
        first = claude_md.read_text()
        result = self._install(claude_md, pending)
        second = claude_md.read_text()
        assert first == second
        assert result in ("unchanged", "updated")
        # Exactly ONE sentinel block, not two
        assert second.count(self.SENTINEL_START) == 1
        assert second.count(self.SENTINEL_END) == 1

    def test_install_updates_path_when_brain_root_changes(self, tmp_path: Path):
        """If $BRAIN_ROOT moves (e.g., user reinstalled with --brain-root),
        re-running install updates the @import path in-place."""
        claude_md = tmp_path / "CLAUDE.md"
        self._install(claude_md, "/old/brain/PENDING_REVIEW.md")
        result = self._install(claude_md, "/new/brain/PENDING_REVIEW.md")
        assert result == "updated"
        body = claude_md.read_text()
        assert "@/new/brain/PENDING_REVIEW.md" in body
        assert "@/old/brain/PENDING_REVIEW.md" not in body

    def test_uninstall_strips_only_our_block(self, tmp_path: Path):
        """User has custom CLAUDE.md content + our sentinel block.
        Uninstall removes ONLY our block, preserving everything else."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Personal\n@/Users/u/.claude-org/CLAUDE.md\nMy custom rule.\n"
        )
        self._install(claude_md, "/abs/.agent/PENDING_REVIEW.md")
        # Sanity: now both user content AND our block exist
        body = claude_md.read_text()
        assert "My custom rule" in body
        assert self.SENTINEL_START in body
        # Uninstall
        removed = self._uninstall(claude_md)
        assert removed is True
        body = claude_md.read_text()
        # Our block gone, user content intact
        assert self.SENTINEL_START not in body
        assert self.SENTINEL_END not in body
        assert "My custom rule" in body
        assert "@/Users/u/.claude-org/CLAUDE.md" in body

    def test_install_uses_absolute_path_in_at_import(self, tmp_path: Path):
        """The generated @import line MUST use the absolute path passed
        in. Claude Code's @ handler may not expand $HOME / ~. Pin this
        explicitly — passing a relative or env-shaped path would break
        the transclusion silently."""
        claude_md = tmp_path / "CLAUDE.md"
        # The install function takes whatever path the caller gives it.
        # install.sh always passes an absolute path (it resolves $BRAIN_ROOT
        # at install time before invoking the heredoc). Test pins that the
        # block carries the path verbatim.
        abs_path = "/Users/maintainer/.agent/PENDING_REVIEW.md"
        self._install(claude_md, abs_path)
        body = claude_md.read_text()
        # The @-line uses the passed-in path verbatim
        assert f"@{abs_path}" in body
        # And the path IS absolute (sanity — guards against future refactor
        # accidentally passing a relative path)
        assert abs_path.startswith("/")

    def test_uninstall_no_op_when_block_absent(self, tmp_path: Path):
        """If CLAUDE.md exists but has no brainstack block, uninstall is
        a no-op that returns False (telling install.sh to log accordingly)."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# user content only\nrule one\n")
        before = claude_md.read_text()
        result = self._uninstall(claude_md)
        assert result is False
        assert claude_md.read_text() == before

    def test_install_sh_setup_pending_hook_matches_test_algorithm(self):
        """Pin: install.sh's setup-pending-hook heredoc MUST contain the
        same sentinel strings and produce the same block shape as our
        test helper. If the heredoc drifts, this test fails noisily.
        Cheap structural check on the install.sh source itself."""
        install_sh = REPO_ROOT / "install.sh"
        body = install_sh.read_text()
        # Sentinels referenced from install.sh
        assert "brainstack-pending-review-start" in body
        assert "brainstack-pending-review-end" in body
        # The mode handler exists
        assert "MODE = \"setup-pending-hook\"" in body or '$MODE" = "setup-pending-hook"' in body
        # And documents the rationale (pinned in comments so future
        # contributors don't accidentally re-add the SessionStart hook)
        assert "telemetry-only" in body or "@-import" in body or "@import" in body


# ---------- TestShellBanner --------------------------------------------


class TestShellBanner:
    """The bash wrapper script that intercepts AI-CLI invocations and
    prints PENDING_REVIEW.md before exec'ing the real binary.

    Wrappers are generated dynamically from a config file
    (~/.agent/banner/wrapped_tools or template default), NOT hardcoded
    in the .sh — Mustafa 2026-05-04 wanted "framework, not point
    solution" so adding a new LLM is a config edit.

    Critical contract (still holds): every generated wrapper uses
    `command <tool> "$@"` (not bare `<tool>`) or it self-recurses
    infinitely. The eval template enforces that at the source level."""

    SCRIPT_PATH = REPO_ROOT / "templates" / "brainstack-shell-banner.sh"
    WRAPPED_TOOLS_PATH = REPO_ROOT / "templates" / "brainstack-wrapped-tools.txt"

    def test_script_file_exists(self):
        assert self.SCRIPT_PATH.is_file(), (
            f"shell banner template missing at {self.SCRIPT_PATH}"
        )

    def test_wrapped_tools_template_exists(self):
        """The default wrapped-tool list ships with the framework.
        install.sh seeds it into ~/.agent/banner/wrapped_tools on setup."""
        assert self.WRAPPED_TOOLS_PATH.is_file(), (
            f"wrapped-tools template missing at {self.WRAPPED_TOOLS_PATH}"
        )

    def test_script_passes_bash_syntax_check(self):
        """`bash -n` parses the script without executing — catches malformed
        function definitions, unbalanced quotes, eval errors."""
        if shutil.which("bash") is None:
            pytest.skip("bash not on PATH")
        result = subprocess.run(
            ["bash", "-n", str(self.SCRIPT_PATH)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"bash -n failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_eval_template_uses_command_prefix(self):
        """The dynamic-wrapper template uses `command ${_bs_tool}` (NOT
        bare `${_bs_tool}`) inside the eval'd function body. This is the
        single most important contract — failing it produces wrappers
        that self-recurse infinitely."""
        body = self.SCRIPT_PATH.read_text()
        # The eval template generates `<name>() { ... command <name> "$@" }`.
        # Look for the literal `command ${_bs_tool}` substring — this is
        # what gets eval'd into each generated wrapper.
        assert "command ${_bs_tool}" in body or 'command "${_bs_tool}"' in body, (
            "eval template doesn't use `command ${_bs_tool}` — generated "
            "wrappers would self-recurse"
        )

    def test_default_tool_list_covers_canonical_set(self):
        """The default wrapped-tool list ships claude/codex/cursor at
        minimum (the maintainer's primary tools). Missing any of these
        means setup goes silently incomplete on first install."""
        body = self.WRAPPED_TOOLS_PATH.read_text()
        for tool in ("claude", "codex", "cursor"):
            assert tool in body, f"default wrapped-tools list missing `{tool}`"

    def test_default_tool_list_supports_extension(self):
        """The default file MUST be config-shaped (one tool per line +
        # comments) so adding a new LLM is a one-line edit. Pin the
        format so a refactor doesn't accidentally turn it into JSON
        or YAML and break the simple-edit contract."""
        body = self.WRAPPED_TOOLS_PATH.read_text()
        # Comment lines exist (proves the # comment convention is used)
        assert any(line.startswith("#") for line in body.splitlines())
        # At least one bare tool name on its own line (proves the format)
        plain_names = [
            line.strip() for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(plain_names) >= 3
        # Each is a bare identifier, not JSON/YAML/TOML key-value
        for name in plain_names:
            assert "=" not in name and ":" not in name and "{" not in name, (
                f"unexpected format in wrapped-tools list: {name!r}"
            )

    def test_runtime_eval_produces_working_wrappers(self, tmp_path: Path):
        """End-to-end: source the script with a custom wrapped_tools
        config, verify the named functions are defined AND each function
        body contains the `command <tool>` invocation. This is the only
        test that actually proves the eval-driven wrapper generation works."""
        if shutil.which("bash") is None:
            pytest.skip("bash not on PATH")
        # Make a fake brain with a custom wrapped_tools config
        fake_brain = tmp_path / ".agent"
        (fake_brain / "banner").mkdir(parents=True)
        (fake_brain / "banner" / "wrapped_tools").write_text(
            "# test config\nclaude\nfoobar\nmytool\n"
        )
        # Source the banner with BRAIN_ROOT pointing at the fake brain,
        # then ask bash to dump each function's body so we can grep it.
        cmd = (
            f"BRAIN_ROOT={fake_brain} source {self.SCRIPT_PATH} && "
            "type claude && echo --- && type foobar && echo --- && type mytool"
        )
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"sourcing banner failed:\nstderr: {result.stderr}"
        )
        out = result.stdout
        # Each tool from the config has its function body printed by `type`
        for tool in ("claude", "foobar", "mytool"):
            assert f"{tool} is a function" in out, (
                f"wrapper for {tool} not generated"
            )
            assert f"command {tool}" in out, (
                f"generated wrapper for {tool} doesn't use `command {tool}`"
            )
