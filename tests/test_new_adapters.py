"""Tests for the post-v0.4 adapters that mirror tool memory into brainstack
without modifying the source dirs (the "mirror, don't swap" architecture).

Three adapters under test:
  - `agent/tools/claude_session_adapter.py`
      Walks ~/.claude/projects/*/*.jsonl session transcripts and emits one
      episode per (tool_use, tool_result) pair into the `claude-sessions`
      namespace. Idempotent via SHA256 sidecar.

  - `agent/tools/claude_misc_adapter.py`
      Mirrors flat dirs (~/.claude/{plans,tasks,sessions,...}, project
      memory dirs, Cursor skills) into `<brain>/imports/<tool>/...`.
      Mtime-based incremental sync.

  - `agent/tools/sync_claude_extras.py`
      LaunchAgent wrapper. Acquires the same fcntl lock brainstack's
      `auto_migrate_all` uses, then runs both adapters in series.

Coverage focuses on correctness of the parser, idempotency of the sidecar,
and graceful handling of malformed input — the parts most likely to break.
End-to-end LaunchAgent behavior is verified manually (can't realistically
exercise launchd in pytest).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "agent" / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import claude_session_adapter as csa  # noqa: E402
import claude_misc_adapter as cma  # noqa: E402


PYTHON = sys.executable


# ---------- helpers ----------------------------------------------------


def _write_session(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _make_tool_use_event(tool_use_id: str, tool_name: str, tool_input: dict,
                          ts: str = "2026-05-04T10:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Running a tool"},
                {"type": "tool_use", "id": tool_use_id, "name": tool_name,
                 "input": tool_input},
            ],
        },
    }


def _make_tool_result_event(tool_use_id: str, output: str,
                             is_error: bool = False,
                             ts: str = "2026-05-04T10:00:01Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id,
                 "content": output, "is_error": is_error},
            ],
        },
    }


# ---------- claude_session_adapter -------------------------------------


class TestSessionAdapterParser:
    def test_extracts_one_episode_per_tool_use_pair(self, tmp_path: Path):
        sf = tmp_path / "proj-a" / "session-uuid.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_1", "Bash", {"command": "ls"}),
            _make_tool_result_event("tu_1", "file1.txt\nfile2.txt"),
        ])
        episodes = list(csa._extract_episodes(sf, "proj-a", "session-uuid"))
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["skill"] == "claude-code"
        assert ep["result"] == "success"
        assert ep["origin"] == "claude.session.Bash"
        assert "ls" in ep["action"]
        assert "file1.txt" in ep["detail"]
        assert ep["pain_score"] == 1  # success → low pain
        assert ep["source"]["session_id"] == "session-uuid"
        assert ep["source"]["project_slug"] == "proj-a"

    def test_marks_failure_when_is_error_true(self, tmp_path: Path):
        sf = tmp_path / "proj-b" / "fail.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_2", "Bash", {"command": "false"}),
            _make_tool_result_event("tu_2", "exit 1", is_error=True),
        ])
        episodes = list(csa._extract_episodes(sf, "proj-b", "fail"))
        assert len(episodes) == 1
        assert episodes[0]["result"] == "failure"
        assert episodes[0]["pain_score"] == 4  # failure → elevated pain

    def test_skips_low_signal_tools(self, tmp_path: Path):
        sf = tmp_path / "proj-c" / "noise.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_3", "Read", {"file_path": "/a"}),
            _make_tool_result_event("tu_3", ""),
            _make_tool_use_event("tu_4", "Glob", {"pattern": "*.py"}),
            _make_tool_result_event("tu_4", ""),
            _make_tool_use_event("tu_5", "Bash", {"command": "echo hi"}),
            _make_tool_result_event("tu_5", "hi"),
        ])
        episodes = list(csa._extract_episodes(sf, "proj-c", "noise"))
        # Read + Glob are skipped (low signal); only Bash emits an episode
        assert len(episodes) == 1
        assert episodes[0]["origin"] == "claude.session.Bash"

    def test_unpaired_tool_result_is_skipped(self, tmp_path: Path):
        """A tool_result without a matching tool_use must not crash."""
        sf = tmp_path / "proj-d" / "orphan.jsonl"
        _write_session(sf, [
            _make_tool_result_event("tu_orphan", "no preceding use"),
        ])
        episodes = list(csa._extract_episodes(sf, "proj-d", "orphan"))
        assert episodes == []

    def test_truncates_oversized_detail(self, tmp_path: Path):
        sf = tmp_path / "proj-e" / "huge.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_huge", "Bash", {"command": "yes"}),
            _make_tool_result_event("tu_huge", "x" * 10_000),
        ])
        episodes = list(csa._extract_episodes(sf, "proj-e", "huge"))
        # Detail is capped at 2KB plus a `...[truncated N bytes]` suffix
        assert len(episodes) == 1
        assert len(episodes[0]["detail"]) <= csa._DETAIL_CAP + 50
        assert "[truncated" in episodes[0]["detail"]

    def test_skips_non_session_event_types(self, tmp_path: Path):
        sf = tmp_path / "proj-f" / "noise.jsonl"
        _write_session(sf, [
            {"type": "permission-mode", "permissionMode": "auto"},
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "attachment", "name": "img.png"},
            {"type": "queue-operation"},
            _make_tool_use_event("tu_real", "Bash", {"command": "uname"}),
            _make_tool_result_event("tu_real", "Darwin"),
        ])
        episodes = list(csa._extract_episodes(sf, "proj-f", "noise"))
        assert len(episodes) == 1
        assert "uname" in episodes[0]["action"]

    def test_handles_malformed_jsonl_lines(self, tmp_path: Path):
        sf = tmp_path / "proj-g" / "broken.jsonl"
        sf.parent.mkdir(parents=True)
        sf.write_text(
            "this is not json\n"
            + json.dumps(_make_tool_use_event("tu_g", "Bash", {"command": "x"})) + "\n"
            + "{\"incomplete\": \n"
            + json.dumps(_make_tool_result_event("tu_g", "ok")) + "\n"
        )
        episodes = list(csa._extract_episodes(sf, "proj-g", "broken"))
        # One valid pair survives despite surrounding garbage
        assert len(episodes) == 1


class TestSessionAdapterEnumeration:
    def test_picks_up_top_level_and_subagent_transcripts(self, tmp_path: Path):
        proj = tmp_path / "proj-h"
        # Top-level session
        top = proj / "abc-123.jsonl"
        top.parent.mkdir(parents=True)
        top.write_text("{}\n")
        # Subagent transcript
        sub = proj / "abc-123" / "subagents" / "agent-x.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n")

        files = csa._enumerate_sessions(tmp_path)
        names = [f.name for f in files]
        assert "abc-123.jsonl" in names
        assert "agent-x.jsonl" in names

    def test_session_id_for_subagent_includes_path(self, tmp_path: Path):
        proj = tmp_path / "proj-i"
        sub = proj / "session-uuid" / "subagents" / "agent-zz.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n")
        slug, sid = csa._slug_and_session_id(sub, tmp_path)
        assert slug == "proj-i"
        assert "subagents" in sid
        assert sid.endswith("agent-zz")


class TestSessionAdapterIdempotency:
    def test_second_run_skips_unchanged_files(self, tmp_path: Path):
        # Build a synthetic source tree
        source = tmp_path / "src"
        sf = source / "proj-j" / "uuid-aaa.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_a", "Bash", {"command": "echo hi"}),
            _make_tool_result_event("tu_a", "hi"),
        ])
        brain = tmp_path / "brain"
        brain.mkdir()

        argv = ["--source", str(source), "--dst", str(brain)]
        rc = csa.main(argv)
        assert rc == 0
        episodic = brain / "memory" / "episodic" / "claude-sessions" / "AGENT_LEARNINGS.jsonl"
        first_lines = episodic.read_text().count("\n")
        assert first_lines >= 1

        # Second run: nothing should be added
        rc = csa.main(argv)
        assert rc == 0
        second_lines = episodic.read_text().count("\n")
        assert second_lines == first_lines

    def test_active_session_append_does_not_duplicate(self, tmp_path: Path):
        """The exact failure mode Codex caught: a session JSONL that
        grows between runs must not re-emit already-imported pairs.

        Run 1: write echo 1 → import → 1 episode.
        Append echo 2 to source.
        Run 2: import again → must emit echo 2 only. Total 2 episodes.

        Old SHA-keyed sidecar would emit 3 (echo 1 twice + echo 2)."""
        source = tmp_path / "src"
        sf = source / "proj" / "session.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_active_1", "Bash", {"command": "echo 1"}),
            _make_tool_result_event("tu_active_1", "1"),
        ])
        brain = tmp_path / "brain"
        brain.mkdir()

        argv = ["--source", str(source), "--dst", str(brain)]
        assert csa.main(argv) == 0

        episodic = brain / "memory" / "episodic" / "claude-sessions" / "AGENT_LEARNINGS.jsonl"
        first_episodes = [json.loads(l) for l in episodic.read_text().splitlines() if l.strip()]
        assert len(first_episodes) == 1
        assert first_episodes[0]["source"]["tool_use_id"] == "tu_active_1"

        # Simulate Claude appending another tool-call to the live JSONL
        with sf.open("a") as f:
            f.write("\n".join(json.dumps(e) for e in [
                _make_tool_use_event("tu_active_2", "Bash", {"command": "echo 2"}),
                _make_tool_result_event("tu_active_2", "2"),
            ]) + "\n")

        # Re-run — must emit ONLY the new pair
        assert csa.main(argv) == 0
        second_episodes = [json.loads(l) for l in episodic.read_text().splitlines() if l.strip()]
        assert len(second_episodes) == 2, (
            f"expected 2 episodes (no dupes), got {len(second_episodes)}: "
            f"{[e['source']['tool_use_id'] for e in second_episodes]}"
        )
        ids = [e["source"]["tool_use_id"] for e in second_episodes]
        assert ids == ["tu_active_1", "tu_active_2"]

    def test_recovers_from_sidecar_loss_via_episodic_self_load(self, tmp_path: Path):
        """If the sidecar is deleted but the episodic JSONL is intact,
        re-running must not double-emit. The pre-load from episodic
        rebuilds the seen set from the on-disk source of truth."""
        source = tmp_path / "src"
        sf = source / "proj" / "s.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_lost", "Bash", {"command": "echo lost"}),
            _make_tool_result_event("tu_lost", "lost"),
        ])
        brain = tmp_path / "brain"
        brain.mkdir()

        argv = ["--source", str(source), "--dst", str(brain)]
        assert csa.main(argv) == 0

        sidecar = brain / "memory" / "episodic" / "claude-sessions" / "_imported.jsonl"
        episodic = brain / "memory" / "episodic" / "claude-sessions" / "AGENT_LEARNINGS.jsonl"
        assert sidecar.is_file()
        before = episodic.read_text()

        # Delete the sidecar — episodic JSONL still exists
        sidecar.unlink()

        # Re-run — must NOT re-emit the already-imported episode
        assert csa.main(argv) == 0
        after = episodic.read_text()
        assert before.count("\n") == after.count("\n"), (
            "episode count grew despite identical source — episodic-self-load failed"
        )


# ---------- claude_misc_adapter ----------------------------------------


class TestMiscAdapterSourceDiscovery:
    def test_discover_project_memory_dirs_finds_real_dirs(self, tmp_path: Path,
                                                            monkeypatch):
        # Arrange a fake $HOME with a couple of project memory dirs
        fake_home = tmp_path / "home"
        proj_root = fake_home / ".claude" / "projects"
        (proj_root / "proj-a" / "memory").mkdir(parents=True)
        (proj_root / "proj-a" / "memory" / "feedback_x.md").write_text("x")
        # Empty memory dir — should be skipped
        (proj_root / "proj-empty" / "memory").mkdir(parents=True)
        # Symlinked dir — should be skipped (already pointing elsewhere)
        symtarget = tmp_path / "elsewhere"
        symtarget.mkdir()
        (proj_root / "proj-symlinked").mkdir()
        (proj_root / "proj-symlinked" / "memory").symlink_to(symtarget)

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        sources = cma._discover_project_memory_dirs()
        slugs = [s for s, _ in sources]
        assert any("proj-a" in s for s in slugs)
        assert not any("proj-empty" in s for s in slugs)
        assert not any("proj-symlinked" in s for s in slugs)

    def test_history_jsonl_and_ai_tracking_excluded(self):
        """The two paths flagged for verified secrets must not be in the
        static source list. Removing this exclusion would re-import known
        credentials into the brain on the next run."""
        for src, _ in cma._STATIC_SOURCES:
            assert "history.jsonl" not in src, (
                f"history.jsonl re-added to sources: {src} — would expose 5 verified creds"
            )
            assert "ai-tracking" not in src, (
                f"ai-tracking re-added: {src} — opaque SQLite with 77 high-entropy hits"
            )

    def test_extra_sources_config_unions_with_defaults(self, tmp_path: Path,
                                                       monkeypatch):
        """`<brain>/imports/extra_sources.txt` adds user-defined sources to
        the default list without replacing them. Comments and blank lines
        are skipped; malformed lines are warned about and ignored."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        brain = tmp_path / "brain"
        (brain / "imports").mkdir(parents=True)
        config = brain / "imports" / "extra_sources.txt"
        config.write_text(
            "# Personal notes\n"
            "\n"
            "~/Documents/My Notes=kb/personal\n"
            "  ~/Other=kb/other  \n"
            "no-equals-line\n"
            "=missing-src\n"
            "/abs/path=\n"
        )

        sources = cma._build_default_sources(brain)
        # Defaults still present (sample one)
        assert any(d == "claude/CLAUDE.md" for _, d in sources)
        # Extras appended
        srcs_dsts = [(s, d) for s, d in sources]
        assert ("~/Documents/My Notes", "kb/personal") in srcs_dsts
        assert ("~/Other", "kb/other") in srcs_dsts
        # Malformed lines were skipped
        assert not any(d == "missing-src" for _, d in sources)
        assert not any(s == "no-equals-line" for s, _ in sources)
        assert not any(s == "/abs/path" and d == "" for s, d in sources)

    def test_extra_sources_missing_config_is_no_op(self, tmp_path: Path,
                                                    monkeypatch):
        """When extra_sources.txt is absent, the source list is unchanged
        — no exception, no warning."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        brain = tmp_path / "brain"
        brain.mkdir()
        # No imports/extra_sources.txt file
        sources_with = cma._build_default_sources(brain)
        sources_without = cma._build_default_sources()
        assert sources_with == sources_without

    def test_extra_sources_rejects_path_traversal_in_dst(self, tmp_path: Path,
                                                         monkeypatch, capsys):
        """A line like SRC=../../outside must NOT be returned — the misc
        adapter joins DST with <brain>/imports/, so traversal silently
        writes outside the brain on every hourly sync. Codex 2026-05-05 P2."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        brain = tmp_path / "brain"
        (brain / "imports").mkdir(parents=True)
        config = brain / "imports" / "extra_sources.txt"
        config.write_text(
            "~/safe=kb/safe\n"
            "~/escape=../../outside\n"
            "~/abs=/etc/secrets\n"
            "~/dotdot=..\n"
            "~/embedded=kb/foo/../../etc\n"
            "~/empty=\n"
            "~/null=kb/foo\x00bar\n"
        )
        sources = cma._read_extra_sources(brain)
        # Only the safe entry survives
        assert sources == [("~/safe", "kb/safe")]

    def test_is_safe_dst_sub_unit(self):
        """Direct unit test for the DST validator — pin its semantics so a
        future refactor doesn't accidentally widen what's accepted."""
        accepts = ["kb/foo", "kb/team-a/notes", "personal_notes",
                   "kb/v1.2", "kb-with-dots.txt"]
        rejects = ["", "..", "../foo", "kb/../foo", "/abs", "kb/foo/..",
                   "foo\x00bar"]
        for d in accepts:
            assert cma._is_safe_dst_sub(d), f"should accept {d!r}"
        for d in rejects:
            assert not cma._is_safe_dst_sub(d), f"should reject {d!r}"


class TestMiscAdapterIncremental:
    def test_unchanged_file_is_skipped_on_rerun(self, tmp_path: Path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src_file = src_dir / "note.md"
        src_file.write_text("first\n")

        brain = tmp_path / "brain"
        brain.mkdir()
        sidecar_path = brain / cma._SIDECAR_REL
        sidecar = {}

        n1, c1, r1, upd1 = cma._process_source(src_dir, "claude/notes",
                                                brain / "imports", sidecar, dry_run=False)
        assert c1 == 1  # one file copied first time

        cma._append_sidecar(sidecar_path, upd1)
        sidecar = cma._read_sidecar(sidecar_path)

        # Second pass with same mtime → no copy
        n2, c2, r2, upd2 = cma._process_source(src_dir, "claude/notes",
                                                brain / "imports", sidecar, dry_run=False)
        assert c2 == 0
        assert upd2 == []

    def test_modified_file_triggers_recopy(self, tmp_path: Path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src_file = src_dir / "note.md"
        src_file.write_text("v1\n")

        brain = tmp_path / "brain"
        brain.mkdir()
        sidecar_path = brain / cma._SIDECAR_REL

        _, _, _, upd1 = cma._process_source(src_dir, "claude/notes",
                                             brain / "imports", {}, dry_run=False)
        cma._append_sidecar(sidecar_path, upd1)

        # Bump mtime + content
        time.sleep(0.05)
        src_file.write_text("v2 — different content\n")
        os.utime(src_file, None)

        sidecar = cma._read_sidecar(sidecar_path)
        _, c2, _, _ = cma._process_source(src_dir, "claude/notes",
                                           brain / "imports", sidecar, dry_run=False)
        assert c2 == 1


# ---------- end-to-end smoke ------------------------------------------


class TestEndToEndCLI:
    def test_session_adapter_dry_run_emits_no_writes(self, tmp_path: Path):
        source = tmp_path / "src"
        sf = source / "proj-k" / "uuid.jsonl"
        _write_session(sf, [
            _make_tool_use_event("tu_k", "Bash", {"command": "true"}),
            _make_tool_result_event("tu_k", "ok"),
        ])
        brain = tmp_path / "brain"
        brain.mkdir()
        rc = csa.main(["--source", str(source), "--dst", str(brain), "--dry-run"])
        assert rc == 0
        episodic = brain / "memory" / "episodic" / "claude-sessions" / "AGENT_LEARNINGS.jsonl"
        assert not episodic.exists()  # dry-run wrote nothing

    def test_misc_adapter_skips_missing_source_silently(self, tmp_path: Path):
        brain = tmp_path / "brain"
        brain.mkdir()
        rc = cma.main([
            "--brain", str(brain),
            "--source", f"{tmp_path}/does-not-exist=claude/missing",
            "--dry-run",
        ])
        assert rc == 0  # missing source isn't an error
