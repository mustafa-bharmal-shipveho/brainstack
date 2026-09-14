"""Tests for the unified session normalizer.

Both Claude and Codex transcripts get parsed into a common
`NormalizedSession` shape so the digest adapter doesn't care about
upstream format quirks. The contract pinned here:

  - Claude `~/.claude/projects/<slug>/<uuid>.jsonl` → NormalizedSession
  - Codex  `~/.codex/sessions/.../rollout-*.jsonl`  → NormalizedSession
  - OMP    `~/.omp/agent/sessions/<slug>/<ts>_<uuid>.jsonl`
                                                    → NormalizedSession
  - Common fields: session_id, source, started_at, ended_at, cwd,
    git_branch, project_slug, model, messages[], raw_token_estimate
  - NormalizedMessage: role, text (concatenated text+thinking content
    blocks), tool_calls[], timestamp
  - Malformed JSON lines are skipped (tolerant — never crash on bad data)
  - Empty session → returns None

No real-user data appears in these fixtures. No org names. Synthetic only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))


@pytest.fixture
def normalize_mod():
    import _session_normalize
    return _session_normalize


# ---------------------------------------------------------------------------
# Synthetic fixtures — match the real schemas verified earlier
# ---------------------------------------------------------------------------

def _claude_session_jsonl(tmp_path: Path, session_id: str = "sess-abc") -> Path:
    """Write a synthetic Claude session jsonl with a representative mix
    of event types. No real-world identifiers."""
    events = [
        {"type": "file-history-snapshot", "messageId": "x"},  # SKIP
        {"type": "permission-mode",
         "permissionMode": "auto"},                            # SKIP
        {"type": "user", "uuid": "u1",
         "parentUuid": None,
         "promptId": "p1",
         "timestamp": "2026-05-01T12:00:00Z",
         "cwd": "/tmp/work",
         "gitBranch": "main",
         "sessionId": session_id,
         "message": {"role": "user", "content": "Help me debug something."}},
        {"type": "assistant", "uuid": "a1",
         "parentUuid": "u1",
         "timestamp": "2026-05-01T12:00:05Z",
         "sessionId": session_id,
         "message": {
             "role": "assistant",
             "model": "claude-haiku-4-5",
             "content": [
                 {"type": "thinking", "thinking": "Let me think."},
                 {"type": "text", "text": "Sure, what is the error?"},
                 {"type": "tool_use", "id": "toolu_1", "name": "Read",
                  "input": {"file": "/tmp/x.py"}},
             ],
             "usage": {"input_tokens": 10, "output_tokens": 30,
                       "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 50},
         }},
        {"type": "user", "uuid": "u2",
         "parentUuid": "a1",
         "timestamp": "2026-05-01T12:00:10Z",
         "sessionId": session_id,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": "file contents..."}]}},
        {"type": "assistant", "uuid": "a2",
         "parentUuid": "u2",
         "timestamp": "2026-05-01T12:00:15Z",
         "sessionId": session_id,
         "message": {
             "role": "assistant",
             "model": "claude-haiku-4-5",
             "content": [
                 {"type": "text", "text": "Found it — typo on line 3."},
             ],
             "usage": {"input_tokens": 20, "output_tokens": 15},
         }},
    ]
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _codex_rollout_jsonl(tmp_path: Path, session_id: str = "019e-codex") -> Path:
    events = [
        {"timestamp": "2026-05-01T18:00:00Z", "type": "session_meta",
         "payload": {
             "id": session_id,
             "timestamp": "2026-05-01T18:00:00Z",
             "cwd": "/tmp/work",
             "originator": "codex_exec",
             "cli_version": "0.125.0",
             "model_provider": "openai",
             "git": {"branch": "main", "commit_hash": "abc123",
                     "repository_url": ""},
         }},
        {"timestamp": "2026-05-01T18:00:01Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "model": "gpt-5.5",
                     "effort": "standard"}},
        {"timestamp": "2026-05-01T18:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"timestamp": "2026-05-01T18:00:03Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "Please review this code."}]}},
        {"timestamp": "2026-05-01T18:00:30Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text",
                                  "text": "Looks fine, ship it."}]}},
        {"timestamp": "2026-05-01T18:00:35Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t1"}},
    ]
    path = tmp_path / f"rollout-{session_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _omp_session_jsonl(tmp_path: Path, session_id: str = "omp-abc") -> Path:
    """Write a synthetic OMP session jsonl with a representative mix of
    entry types. Mirrors the real schema (title/session/model_change
    metadata events, then message events whose content blocks are
    text/thinking/toolCall, plus toolResult-role messages and custom
    harness events). No real-world identifiers."""
    events = [
        {"type": "title", "v": 1, "title": "Synthetic session",
         "source": "auto", "updatedAt": "2026-05-01T12:00:20Z"},
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-05-01T12:00:00Z", "cwd": "/tmp/work",
         "title": "Synthetic session", "titleSource": "auto"},
        {"type": "model_change", "id": "mc1", "parentId": None,
         "timestamp": "2026-05-01T12:00:01Z",
         "model": "synthetic/model-1", "resolvedModelIsFallback": False},
        {"type": "thinking_level_change", "id": "tl1", "parentId": "mc1",
         "timestamp": "2026-05-01T12:00:01Z",
         "thinkingLevel": "high", "configured": None},
        {"type": "message", "id": "m1", "parentId": "tl1",
         "timestamp": "2026-05-01T12:00:02Z",
         "message": {"role": "user",
                     "content": [{"type": "text",
                                  "text": "Why is the sync job failing?"}],
                     "attribution": "user"}},
        {"type": "custom", "customType": "tool_execution_start",
         "data": {"toolCallId": "grep_0", "toolName": "grep",
                  "startedAt": "2026-05-01T12:00:03Z",
                  "intent": "Searching for the sync config"},
         "id": "c1", "parentId": "m1",
         "timestamp": "2026-05-01T12:00:03Z"},
        {"type": "message", "id": "m2", "parentId": "c1",
         "timestamp": "2026-05-01T12:00:04Z",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "thinking",
                          "thinking": "Check the sync config first."},
                         {"type": "text",
                          "text": "Let me look at the sync config."},
                         {"type": "toolCall", "id": "grep_0",
                          "name": "grep",
                          "arguments": {"pattern": "sync",
                                        "path": "/tmp/work"}},
                     ]}},
        {"type": "message", "id": "m3", "parentId": "m2",
         "timestamp": "2026-05-01T12:00:05Z",
         "message": {"role": "toolResult", "toolCallId": "grep_0",
                     "toolName": "grep",
                     "content": [{"type": "text",
                                  "text": "sync.sh:42: rsync failed"}]}},
        {"type": "message", "id": "m4", "parentId": "m3",
         "timestamp": "2026-05-01T12:00:10Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text",
                                  "text": "Found it — rsync fails at "
                                          "line 42."}]}},
        {"type": "custom_message",
         "customType": "mid-run-todo-nudge",
         "id": "cm1", "parentId": "m4",
         "timestamp": "2026-05-01T12:00:11Z"},
        {"type": "ttsr_injection", "id": "ti1", "parentId": "cm1",
         "timestamp": "2026-05-01T12:00:12Z",
         "injectedRules": ["ts-no-any"]},
        {"type": "title_change", "id": "tc1", "parentId": "ti1",
         "timestamp": "2026-05-01T12:00:15Z",
         "title": "Synthetic session", "source": "auto"},
    ]
    path = tmp_path / f"2026-05-01T12-00-00-000Z_{session_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


# ---------------------------------------------------------------------------
# Claude normalization
# ---------------------------------------------------------------------------

class TestNormalizeClaude:
    def test_returns_session_with_required_fields(self, normalize_mod,
                                                  tmp_path):
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="proj-x")
        assert s is not None
        assert s.session_id == "sess-abc"
        assert s.source == "claude"
        assert s.project_slug == "proj-x"
        assert s.cwd == "/tmp/work"
        assert s.git_branch == "main"
        assert s.model == "claude-haiku-4-5"
        assert s.started_at == "2026-05-01T12:00:00Z"
        assert s.ended_at == "2026-05-01T12:00:15Z"
        assert s.raw_token_estimate > 0

    def test_messages_have_correct_roles_and_order(self, normalize_mod,
                                                   tmp_path):
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        roles = [m.role for m in s.messages]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_assistant_text_concatenates_text_and_thinking(self,
                                                           normalize_mod,
                                                           tmp_path):
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        # First assistant message has thinking + text + tool_use; text field
        # should include both the thinking and the text content blocks so
        # the summarizer sees the assistant's full reasoning.
        a1 = s.messages[1]
        assert "Let me think." in a1.text
        assert "Sure, what is the error?" in a1.text

    def test_assistant_tool_calls_captured(self, normalize_mod, tmp_path):
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        a1 = s.messages[1]
        assert len(a1.tool_calls) == 1
        tc = a1.tool_calls[0]
        assert tc["name"] == "Read"
        assert tc["input"]["file"] == "/tmp/x.py"

    def test_user_content_string_normalized_as_text(self, normalize_mod,
                                                    tmp_path):
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        assert "debug something" in s.messages[0].text

    def test_user_tool_result_content_preserved_as_text(self, normalize_mod,
                                                        tmp_path):
        """Claude user events that are tool_result arrays carry the
        tool output as `content`. The summarizer needs to see the tool
        output (it's often where the actual finding lives). A normalizer
        that drops tool_result content silently steals important signal."""
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        # The 3rd message in our fixture is a user with tool_result
        u2 = s.messages[2]
        assert u2.role == "user"
        assert "file contents..." in u2.text

    def test_every_message_carries_its_timestamp(self, normalize_mod,
                                                  tmp_path):
        """Each NormalizedMessage must preserve its source event
        timestamp so map-reduce chunk boundaries can be drawn on time."""
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        timestamps = [m.timestamp for m in s.messages]
        # Distinct timestamps from the fixture (4 events, 4 timestamps)
        assert len(set(timestamps)) == 4
        # All non-empty
        for ts in timestamps:
            assert ts

    def test_skips_non_conversation_events(self, normalize_mod, tmp_path):
        """file-history-snapshot, permission-mode, etc. must not appear
        in messages[]. They're metadata, not turns."""
        path = _claude_session_jsonl(tmp_path)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        # 4 conversational events in the fixture
        assert len(s.messages) == 4

    def test_tolerates_malformed_json_lines(self, normalize_mod, tmp_path):
        """A corrupt or partial line in the middle of an active session
        must not crash the normalizer."""
        path = _claude_session_jsonl(tmp_path)
        text = path.read_text() + "{not valid json\n"
        path.write_text(text)
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        assert s is not None
        assert len(s.messages) == 4

    def test_empty_or_metadata_only_session_returns_none(self, normalize_mod,
                                                         tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text(
            json.dumps({"type": "file-history-snapshot"}) + "\n"
            + json.dumps({"type": "permission-mode",
                          "permissionMode": "auto"}) + "\n"
        )
        s = normalize_mod.normalize_claude_session(path, project_slug="p")
        assert s is None


# ---------------------------------------------------------------------------
# Codex normalization
# ---------------------------------------------------------------------------

class TestNormalizeCodex:
    def test_returns_session_with_required_fields(self, normalize_mod,
                                                  tmp_path):
        path = _codex_rollout_jsonl(tmp_path)
        s = normalize_mod.normalize_codex_session(path)
        assert s is not None
        assert s.session_id == "019e-codex"
        assert s.source == "codex"
        assert s.cwd == "/tmp/work"
        assert s.git_branch == "main"
        assert s.model == "gpt-5.5"
        # started_at = min event ts, ended_at = max
        assert s.started_at == "2026-05-01T18:00:00Z"
        assert s.ended_at == "2026-05-01T18:00:35Z"

    def test_messages_extracted_from_response_items_only(self, normalize_mod,
                                                         tmp_path):
        path = _codex_rollout_jsonl(tmp_path)
        s = normalize_mod.normalize_codex_session(path)
        roles = [m.role for m in s.messages]
        assert roles == ["user", "assistant"]
        assert "review this code" in s.messages[0].text.lower()
        assert "ship it" in s.messages[1].text.lower()

    def test_tolerates_malformed_lines(self, normalize_mod, tmp_path):
        path = _codex_rollout_jsonl(tmp_path)
        text = path.read_text() + "{broken\n"
        path.write_text(text)
        s = normalize_mod.normalize_codex_session(path)
        assert s is not None
        assert len(s.messages) == 2

    def test_metadata_only_session_returns_none(self, normalize_mod,
                                                 tmp_path):
        """A Codex rollout that has session_meta but no response_item
        events shouldn't produce a digest-able session — caller treats
        None as 'skip this session'."""
        path = tmp_path / "meta-only.jsonl"
        path.write_text(json.dumps({
            "timestamp": "2026-05-01T18:00:00Z",
            "type": "session_meta",
            "payload": {"id": "meta-only", "cwd": "/tmp"},
        }) + "\n")
        s = normalize_mod.normalize_codex_session(path)
        assert s is None

    def test_malformed_line_in_middle_still_recovers(self, normalize_mod,
                                                    tmp_path):
        """Garbage in the middle of a Codex rollout (not just at the end)
        must not lose the surrounding valid events."""
        path = _codex_rollout_jsonl(tmp_path)
        lines = path.read_text().splitlines()
        # Insert garbage between session_meta and turn_context
        lines.insert(1, "{this is not valid json")
        path.write_text("\n".join(lines) + "\n")
        s = normalize_mod.normalize_codex_session(path)
        assert s is not None
        assert len(s.messages) == 2


# ---------------------------------------------------------------------------
# OMP normalization
# ---------------------------------------------------------------------------

class TestNormalizeOMP:
    def test_returns_session_with_required_fields(self, normalize_mod,
                                                  tmp_path):
        path = _omp_session_jsonl(tmp_path)
        s = normalize_mod.normalize_omp_session(path, project_slug="proj-x")
        assert s is not None
        assert s.session_id == "omp-abc"
        assert s.source == "omp"
        assert s.project_slug == "proj-x"
        assert s.cwd == "/tmp/work"
        assert s.git_branch is None  # OMP doesn't record the branch
        assert s.model == "synthetic/model-1"
        assert s.started_at == "2026-05-01T12:00:00Z"
        assert s.ended_at == "2026-05-01T12:00:15Z"
        assert s.raw_token_estimate > 0

    def test_messages_have_correct_roles_and_order(self, normalize_mod,
                                                   tmp_path):
        """user prompt → assistant (thinking+text+toolCall) → toolResult
        (mapped to user, same as Claude tool_result) → assistant."""
        path = _omp_session_jsonl(tmp_path)
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        roles = [m.role for m in s.messages]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_assistant_text_concatenates_text_and_thinking(self,
                                                           normalize_mod,
                                                           tmp_path):
        path = _omp_session_jsonl(tmp_path)
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        a1 = s.messages[1]
        assert "Check the sync config first." in a1.text
        assert "Let me look at the sync config." in a1.text

    def test_assistant_tool_calls_captured(self, normalize_mod, tmp_path):
        """OMP `toolCall` blocks normalize to the Claude tool_use shape
        ({id, name, input}) so prompt assembly is source-agnostic."""
        path = _omp_session_jsonl(tmp_path)
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        a1 = s.messages[1]
        assert len(a1.tool_calls) == 1
        tc = a1.tool_calls[0]
        assert tc["id"] == "grep_0"
        assert tc["name"] == "grep"
        assert tc["input"]["pattern"] == "sync"

    def test_tool_result_content_preserved_as_user_text(self, normalize_mod,
                                                        tmp_path):
        """OMP toolResult messages carry tool output — often where the
        actionable finding lives. Dropping them would steal signal; they
        normalize to role="user" exactly like Claude tool_result blocks."""
        path = _omp_session_jsonl(tmp_path)
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        tr = s.messages[2]
        assert tr.role == "user"
        assert "rsync failed" in tr.text

    def test_skips_non_conversation_events(self, normalize_mod, tmp_path):
        """title/session/model_change/thinking_level_change/title_change/
        custom/custom_message/ttsr_injection are metadata, not turns."""
        path = _omp_session_jsonl(tmp_path)
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        assert len(s.messages) == 4

    def test_tolerates_malformed_json_lines(self, normalize_mod, tmp_path):
        path = _omp_session_jsonl(tmp_path)
        path.write_text(path.read_text() + "{not valid json\n")
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        assert s is not None
        assert len(s.messages) == 4

    def test_metadata_only_session_returns_none(self, normalize_mod,
                                                tmp_path):
        """An OMP session opened and abandoned before the first prompt
        (session + model_change events only) is not digest-able."""
        path = tmp_path / "meta-only.jsonl"
        path.write_text(
            json.dumps({"type": "session", "version": 3,
                        "id": "meta-only",
                        "timestamp": "2026-05-01T12:00:00Z",
                        "cwd": "/tmp/work"}) + "\n"
            + json.dumps({"type": "model_change", "id": "mc1",
                          "timestamp": "2026-05-01T12:00:01Z",
                          "model": "synthetic/model-1"}) + "\n"
        )
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        assert s is None

    def test_session_id_falls_back_to_file_stem(self, normalize_mod,
                                                tmp_path):
        """A transcript missing the `session` event (truncated head)
        still gets a stable id from its file name."""
        path = tmp_path / "2026-05-01T12-00-00-000Z_orphan.jsonl"
        path.write_text(json.dumps({
            "type": "message", "id": "m1",
            "timestamp": "2026-05-01T12:00:02Z",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": "hello"}]},
        }) + "\n")
        s = normalize_mod.normalize_omp_session(path, project_slug="p")
        assert s is not None
        assert s.session_id == "2026-05-01T12-00-00-000Z_orphan"


# ---------------------------------------------------------------------------
# Common contract
# ---------------------------------------------------------------------------

class TestCommonShape:
    def test_raw_token_estimate_uses_four_char_rule(self, normalize_mod,
                                                     tmp_path):
        """4 chars ≈ 1 token approximation. Doesn't have to be exact,
        but must be deterministic + monotonic with input size."""
        small_path = _claude_session_jsonl(tmp_path, "sess-small")
        big_path = _claude_session_jsonl(tmp_path, "sess-big")
        # Inflate the big one
        big_text = big_path.read_text() * 5
        big_path.write_text(big_text)
        s_small = normalize_mod.normalize_claude_session(small_path,
                                                         project_slug="p")
        s_big = normalize_mod.normalize_claude_session(big_path,
                                                       project_slug="p")
        assert s_big.raw_token_estimate > s_small.raw_token_estimate
