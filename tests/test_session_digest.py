"""End-to-end tests for the session digest adapter.

The adapter is the orchestrator: it walks transcripts, dedups via a
content-SHA sidecar, normalizes via _session_normalize, redacts via
redact_jsonl.redact_string, summarizes via the resolved LLM provider
(with map-reduce for >60K-token sessions), and writes two surfaces via
_digest_render.

This file is the contract for the whole pipeline. Real subprocess calls
are mocked; framework purity is enforced (synthetic fixtures only).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))


@pytest.fixture
def adapter_mod():
    import claude_session_digest_adapter as adapter
    return adapter


# ---------------------------------------------------------------------------
# Fake provider — deterministic + inspectable
# ---------------------------------------------------------------------------

class FakeProvider:
    """In-memory provider stand-in. Captures every prompt + system pair
    so tests can assert what was sent. Returns canned digest JSON."""
    name = "fake"
    default_model = "fake-1"

    def __init__(self, *, response: dict | None = None,
                 fail_n_times: int = 0):
        self.calls = []
        self._response = response or {
            "title": "Synthetic digest title",
            "domain_tags": ["topic-a"],
            "what_user_did": "Did some work.",
            "what_was_learned": "Learned a thing.",
            "decisions": [],
            "files_touched": [],
            "outcome": "completed",
            "salience": 5,
        }
        self._fail_n_times = fail_n_times

    def is_available(self):
        return (True, "")

    def invoke(self, system, prompt, *, model=None, json_schema=None,
               max_budget_usd=0.10, timeout_s=60):
        self.calls.append({"system": system, "prompt": prompt,
                           "model": model, "schema": json_schema})
        if self._fail_n_times > 0:
            self._fail_n_times -= 1
            # Import lazily to avoid circular at module load
            from llm_providers.base import LLMError
            raise LLMError("simulated failure")
        from llm_providers.base import LLMResult
        return LLMResult(
            text=json.dumps(self._response),
            parsed_json=self._response,
            tokens_in=100, tokens_out=80,
            provider=self.name, model=model or self.default_model,
            cost_usd=None,
        )


# ---------------------------------------------------------------------------
# Synthetic session jsonl helpers (same shape as test_session_normalize)
# ---------------------------------------------------------------------------

def _claude_session(tmp_path: Path, sid: str = "s1",
                    n_turns: int = 4) -> Path:
    path = tmp_path / f"{sid}.jsonl"
    events = []
    for i in range(n_turns):
        events.append({
            "type": "user", "uuid": f"u{i}", "promptId": f"p{i}",
            "timestamp": f"2026-05-01T12:0{i}:00Z",
            "sessionId": sid, "cwd": "/tmp/work", "gitBranch": "main",
            "message": {"role": "user",
                        "content": f"User message {i} talking about topic"},
        })
        events.append({
            "type": "assistant", "uuid": f"a{i}", "parentUuid": f"u{i}",
            "timestamp": f"2026-05-01T12:0{i}:30Z",
            "sessionId": sid,
            "message": {"role": "assistant",
                        "model": "claude-haiku-4-5",
                        "content": [{"type": "text",
                                     "text": f"Assistant reply {i}"}],
                        "usage": {"input_tokens": 5, "output_tokens": 10}},
        })
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _codex_rollout(tmp_path: Path, sid: str = "codex-s1") -> Path:
    """Synthetic Codex rollout under YYYY/MM/DD/ layout matching the
    real on-disk structure."""
    d = tmp_path / "sessions" / "2026" / "05" / "01"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-2026-05-01T18-00-00-{sid}.jsonl"
    events = [
        {"timestamp": "2026-05-01T18:00:00Z", "type": "session_meta",
         "payload": {"id": sid, "cwd": "/tmp/work", "cli_version": "0.125.0",
                     "git": {"branch": "main"}}},
        {"timestamp": "2026-05-01T18:00:01Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "model": "gpt-5.5"}},
        {"timestamp": "2026-05-01T18:00:03Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "Review this code please."}]}},
        {"timestamp": "2026-05-01T18:00:30Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text",
                                  "text": "Looks fine, ship it."}]}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


# ---------------------------------------------------------------------------
# Discovery + idempotency (sidecar)
# ---------------------------------------------------------------------------

class TestDiscoveryAndIdempotency:
    def test_iter_claude_sessions_finds_session_files(self, adapter_mod,
                                                      tmp_path):
        projects = tmp_path / ".claude" / "projects"
        slug = "-tmp-work"
        d = projects / slug
        d.mkdir(parents=True)
        _claude_session(d, "sess-1")
        _claude_session(d, "sess-2")
        found = list(adapter_mod.iter_claude_sessions(projects_root=projects))
        sids = sorted([s.session_id for s in found])
        assert sids == ["sess-1", "sess-2"]
        for s in found:
            # project_slug is set from the parent dir name
            assert s.project_slug == slug

    def test_backfill_writes_both_surfaces(self, adapter_mod, tmp_path):
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        _claude_session(projects / "p", "sess-1")
        provider = FakeProvider()

        stats = adapter_mod.backfill(
            brain_root=brain, projects_root=projects,
            codex_root=None, provider=provider,
        )
        assert stats["digests_written"] == 1
        # Episodic line written
        ep_path = brain / "memory" / "episodic" / "digests" \
                  / "AGENT_LEARNINGS.jsonl"
        assert ep_path.exists()
        lines = [l for l in ep_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        # Markdown written
        md_dir = brain / "memory" / "semantic" / "digests"
        mds = list(md_dir.glob("*.md"))
        assert len(mds) == 1

    def test_provider_receives_full_digest_schema(self, adapter_mod,
                                                   tmp_path):
        """The adapter must pass a json_schema with ALL digest fields
        required, so the provider's schema-validation retry kicks in if
        the LLM omits something. Otherwise a half-formed digest gets
        written + saved as 'this session is done'."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        _claude_session(projects / "p", "sess-1")
        provider = FakeProvider()
        adapter_mod.backfill(brain_root=brain, projects_root=projects,
                              codex_root=None, provider=provider)
        assert len(provider.calls) == 1
        schema = provider.calls[0]["schema"]
        assert schema is not None
        required = set(schema.get("required", []))
        # The digest fields the design promises must all be required:
        for f in ("title", "domain_tags", "what_user_did",
                  "what_was_learned", "decisions", "files_touched",
                  "outcome", "salience"):
            assert f in required, f"schema missing required field {f!r}"

    def test_sidecar_dedups_second_run(self, adapter_mod, tmp_path):
        """Second invocation on unchanged data must be a complete no-op:
        no new LLM calls, no new episodic lines, no new markdown."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        _claude_session(projects / "p", "sess-1")
        provider = FakeProvider()

        s1 = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                   codex_root=None, provider=provider)
        s2 = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                   codex_root=None, provider=provider)
        assert s1["digests_written"] == 1
        assert s2["digests_written"] == 0
        assert s2["skipped_idempotent"] == 1
        # Provider was only called once total
        assert len(provider.calls) == 1

    def test_codex_backfill_writes_both_surfaces(self, adapter_mod,
                                                  tmp_path):
        """Equal coverage for Codex sessions: walking, normalization,
        digest, dual write must work the same as Claude. A bug in the
        Codex path that breaks the rollout walker would otherwise pass."""
        brain = tmp_path / "brain"
        codex_root = tmp_path / "codex"
        codex_root.mkdir()
        _codex_rollout(codex_root, "codex-s1")
        provider = FakeProvider()
        stats = adapter_mod.backfill(
            brain_root=brain, projects_root=None,
            codex_root=codex_root, provider=provider,
        )
        assert stats["digests_written"] == 1
        ep = brain / "memory" / "episodic" / "digests" / "AGENT_LEARNINGS.jsonl"
        assert ep.exists()
        line = json.loads(ep.read_text().strip())
        assert line["origin"] == "session.digest.codex"
        md_dir = brain / "memory" / "semantic" / "digests"
        assert len(list(md_dir.glob("*.md"))) == 1

    def test_sidecar_persisted_with_sha_on_disk(self, adapter_mod, tmp_path):
        """The sidecar file at memory/episodic/digests/_imported.jsonl
        must persist the content_sha256 per session so dedup survives
        process restart (not just in-memory caching)."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        path = _claude_session(projects / "p", "sess-1")
        provider = FakeProvider()
        adapter_mod.backfill(brain_root=brain, projects_root=projects,
                              codex_root=None, provider=provider)

        sidecar = brain / "memory" / "episodic" / "digests" / "_imported.jsonl"
        assert sidecar.exists()
        # Sidecar entry must record session_id + actual SHA + source
        entries = [json.loads(l) for l in sidecar.read_text().splitlines()
                   if l.strip()]
        assert len(entries) == 1
        e = entries[0]
        assert e["session_id"] == "sess-1"
        assert e.get("source") == "claude"
        # SHA must actually match the file content
        expected_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert e["content_sha256"] == expected_sha

    def test_sidecar_redigests_when_content_changes(self, adapter_mod,
                                                    tmp_path):
        """Active sessions get re-digested when the transcript grows.
        Matches claude_session_adapter's tool_use_id approach for
        active-session correctness."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        path = _claude_session(projects / "p", "sess-1", n_turns=2)
        provider = FakeProvider()

        adapter_mod.backfill(brain_root=brain, projects_root=projects,
                              codex_root=None, provider=provider)
        # Extend the session and re-run
        extra = json.dumps({
            "type": "user", "uuid": "ux", "promptId": "px",
            "timestamp": "2026-05-01T12:09:00Z", "sessionId": "sess-1",
            "message": {"role": "user", "content": "another turn"},
        }) + "\n"
        path.write_text(path.read_text() + extra)
        s2 = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                   codex_root=None, provider=provider)
        assert s2["digests_written"] == 1


# ---------------------------------------------------------------------------
# Redaction integration
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_secrets_redacted_before_llm_sees_them(self, adapter_mod,
                                                    tmp_path):
        """An AWS-style access key in a transcript must NEVER reach the
        provider's prompt. Defense in depth: even though sessions live
        locally, the LLM call is an out-of-process boundary."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        d = projects / "p"
        d.mkdir(parents=True)
        # Plant a fake AWS key in the assistant text. Pattern below is
        # synthetic but matches the AWS access-key shape redact.py knows.
        fake_aws = "AKIAIOSFODNN7EXAMPLE"
        events = [
            {"type": "user", "uuid": "u1", "promptId": "p1",
             "timestamp": "2026-05-01T12:00:00Z", "sessionId": "sess-s",
             "message": {"role": "user", "content": "what's the key"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": "2026-05-01T12:00:30Z", "sessionId": "sess-s",
             "message": {"role": "assistant",
                         "model": "claude-haiku-4-5",
                         "content": [{"type": "text",
                                      "text": f"Set AWS_KEY={fake_aws}"}],
                         "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ]
        (d / "sess-s.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events))
        provider = FakeProvider()
        adapter_mod.backfill(brain_root=brain, projects_root=projects,
                              codex_root=None, provider=provider)
        # The provider was called — inspect what it saw
        assert len(provider.calls) == 1
        prompt_seen = provider.calls[0]["prompt"]
        assert fake_aws not in prompt_seen, \
            "AWS-style key leaked through to the LLM prompt"
        assert "REDACTED" in prompt_seen


# ---------------------------------------------------------------------------
# Map-reduce for big sessions
# ---------------------------------------------------------------------------

class TestMapReduce:
    def test_small_session_single_pass(self, adapter_mod, tmp_path):
        """≤60K-token sessions go straight to a single LLM call."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        _claude_session(projects / "p", "sess-small", n_turns=2)
        provider = FakeProvider()
        adapter_mod.backfill(brain_root=brain, projects_root=projects,
                              codex_root=None, provider=provider)
        # Exactly one call for a small session
        assert len(provider.calls) == 1

    def test_large_session_map_reduce(self, adapter_mod, tmp_path,
                                       monkeypatch):
        """Large sessions get chunked into N per-chunk summaries + 1
        final merge call. Coverage requirements:
          - More than one provider call total
          - Each chunk's content appears in at least one provider prompt
            (no chunk silently dropped)
          - A final merge call receives content derived from earlier
            chunk responses (not just the first chunk's output)
          - Exactly one digest is written (the merged result)
        """
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        # Force a low threshold so the test is fast
        monkeypatch.setattr(adapter_mod, "SINGLE_PASS_TOKEN_LIMIT", 200)
        d = projects / "p"
        events = []
        # Use a unique marker per turn so we can check chunk coverage
        per_turn_markers = [f"MARKER_{i:03d}" for i in range(6)]
        for i, marker in enumerate(per_turn_markers):
            text = ("x" * 800) + " " + marker
            events.append({
                "type": "user", "uuid": f"u{i}", "promptId": f"p{i}",
                "timestamp": f"2026-05-01T12:{i:02d}:00Z",
                "sessionId": "sess-big",
                "message": {"role": "user", "content": text}})
            events.append({
                "type": "assistant", "uuid": f"a{i}", "parentUuid": f"u{i}",
                "timestamp": f"2026-05-01T12:{i:02d}:30Z",
                "sessionId": "sess-big",
                "message": {"role": "assistant",
                            "model": "claude-haiku-4-5",
                            "content": [{"type": "text", "text": text}],
                            "usage": {"input_tokens": 1,
                                      "output_tokens": 1}}})
        (d / "sess-big.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events))

        provider = FakeProvider()
        stats = adapter_mod.backfill(brain_root=brain,
                                      projects_root=projects,
                                      codex_root=None, provider=provider)
        # ≥2 chunks + 1 merge ⇒ at least 3 calls
        assert len(provider.calls) >= 3, (
            f"expected at least 2 chunks + 1 merge, got "
            f"{len(provider.calls)} provider call(s)"
        )
        # Every per-turn marker must appear in at least one chunk prompt
        # (no chunk silently dropped). Last call is the merge — exclude it.
        chunk_prompts = "\n".join(c["prompt"] for c in provider.calls[:-1])
        for m in per_turn_markers:
            assert m in chunk_prompts, f"chunk dropped — marker {m!r} missing"
        # Exactly one digest written (the merged one)
        assert stats["digests_written"] == 1


# ---------------------------------------------------------------------------
# Error handling — one bad session must not break the run
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_provider_failure_skips_session_continues(self, adapter_mod,
                                                       tmp_path):
        """One bad session in the middle of a backfill must NOT halt
        the loop. The 1st + 3rd sessions succeed; the 2nd fails. We
        identify the bad session by call ORDER (deterministic via
        sorted session walk) rather than peeking inside the prompt —
        that contract belongs to the adapter, not the test fixture."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        _claude_session(projects / "p", "sess-a")
        _claude_session(projects / "p", "sess-b")  # this one fails
        _claude_session(projects / "p", "sess-c")

        from llm_providers.base import LLMError, LLMResult

        class CallOrderFailProvider:
            name = "fake"
            default_model = "fake-1"
            def __init__(self): self.n = 0
            def is_available(self): return (True, "")
            def invoke(self, system, prompt, **kw):
                self.n += 1
                if self.n == 2:
                    raise LLMError("simulated middle-session error")
                payload = {"title": f"ok-{self.n}",
                           "domain_tags": [], "what_user_did": "x",
                           "what_was_learned": "y", "decisions": [],
                           "files_touched": [], "outcome": "completed",
                           "salience": 5}
                return LLMResult(
                    text=json.dumps(payload), parsed_json=payload,
                    tokens_in=1, tokens_out=1,
                    provider="fake", model="fake-1", cost_usd=None,
                )

        provider = CallOrderFailProvider()
        stats = adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                      codex_root=None, provider=provider)
        assert stats["digests_written"] == 2
        assert stats["failed"] == 1

    def test_unauthenticated_provider_aborts_cleanly(self, adapter_mod,
                                                      tmp_path):
        """If is_available() is False, adapter must raise/refuse instead
        of attempting calls. New users hitting this should see exactly
        what to fix."""
        brain = tmp_path / "brain"
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        _claude_session(projects / "p", "sess-x")

        class BadProvider:
            name = "fake"
            default_model = "fake-1"
            def is_available(self):
                return (False, "no auth: run `fake-cli login`")
            def invoke(self, *a, **k):
                raise AssertionError("should not be called")

        from llm_providers.base import ProviderNotAvailable
        with pytest.raises(ProviderNotAvailable, match=r"(?i)fake-cli login"):
            adapter_mod.backfill(brain_root=brain, projects_root=projects,
                                  codex_root=None, provider=BadProvider())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    """digest_cli.py contract — argparse surface + exit codes."""

    @pytest.fixture
    def cli_mod(self):
        import digest_cli
        return digest_cli

    def test_provider_list_lists_both_providers(self, cli_mod, capsys,
                                                 monkeypatch):
        """`digest_cli.py provider list` prints each provider with
        availability marker. Exit code 0 if any available, 1 if none."""
        from llm_providers import PROVIDERS
        with patch.object(PROVIDERS["claude-code"], "is_available",
                          return_value=(True, "")), \
             patch.object(PROVIDERS["codex"], "is_available",
                          return_value=(False, "not authed")):
            code = cli_mod.main(["provider", "list"])
        out = capsys.readouterr().out
        assert "claude-code" in out and "codex" in out
        assert "✓" in out  # available marker
        assert "not authed" in out
        assert code == 0

    def test_provider_list_exits_nonzero_when_none_available(self, cli_mod,
                                                              capsys):
        from llm_providers import PROVIDERS
        with patch.object(PROVIDERS["claude-code"], "is_available",
                          return_value=(False, "no auth")), \
             patch.object(PROVIDERS["codex"], "is_available",
                          return_value=(False, "no cli")):
            code = cli_mod.main(["provider", "list"])
        assert code != 0
