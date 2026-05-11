"""Tests for the pluggable LLM provider system used by the digest adapter.

The provider layer is the framework's escape hatch: any LLM CLI the user
already has set up should be drivable from Python without an extra API
key. This file pins the contract:

  - LLMProvider ABC + LLMResult dataclass shape
  - PROVIDERS registry: 'claude-code' + 'codex' shipped by default
  - resolve_provider() resolution order:
        explicit arg > BRAIN_LLM_PROVIDER env > config.toml > first available
  - ClaudeCodeProvider subprocess shape: `claude -p --print --output-format json
        [--json-schema ...] --model <m> --max-budget-usd <n>`
  - CodexProvider subprocess shape: `codex exec --skip-git-repo-check`,
        prompt via stdin, post-hoc JSON parse from plain text output
  - Both retry once on JSON-schema validation failure with a stricter prompt
  - Both honor DIGEST_RATE_SLEEP_S env knob between calls
  - ProviderNotAvailable aggregates per-provider skip reasons so the user
    sees exactly what to fix

Framework purity: no test touches a real LLM. All subprocess calls are
mocked. No test fixture mentions any organization, codename, or
employer-specific lexicon.
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))


# ---------------------------------------------------------------------------
# Module-level imports (forced lazy so tests fail clean if module missing)
# ---------------------------------------------------------------------------

@pytest.fixture
def providers_mod():
    import llm_providers
    return llm_providers


@pytest.fixture
def base_mod():
    from llm_providers import base
    return base


# ---------------------------------------------------------------------------
# Base contract: LLMResult dataclass + LLMProvider ABC
# ---------------------------------------------------------------------------

class TestBaseContract:
    def test_llm_result_has_required_fields(self, base_mod):
        r = base_mod.LLMResult(
            text="hello",
            parsed_json={"k": "v"},
            tokens_in=10,
            tokens_out=5,
            provider="claude-code",
            model="haiku",
            cost_usd=None,
        )
        assert r.text == "hello"
        assert r.parsed_json == {"k": "v"}
        assert r.tokens_in == 10
        assert r.tokens_out == 5
        assert r.provider == "claude-code"
        assert r.model == "haiku"
        assert r.cost_usd is None

    def test_llm_provider_is_abstract(self, base_mod):
        with pytest.raises(TypeError):
            base_mod.LLMProvider()  # type: ignore[abstract]

    def test_provider_not_available_exception_carries_reasons(self, base_mod):
        exc = base_mod.ProviderNotAvailable(
            {"claude-code": "no CLAUDE_CODE_OAUTH_TOKEN",
             "codex":       "codex CLI not on PATH"}
        )
        # The user must see EVERY skip reason aggregated, not just the first.
        msg = str(exc)
        assert "claude-code" in msg
        assert "codex" in msg
        assert "no CLAUDE_CODE_OAUTH_TOKEN" in msg
        assert "codex CLI not on PATH" in msg


# ---------------------------------------------------------------------------
# Registry + resolve_provider() resolution order
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_default_registry_has_both_providers(self, providers_mod):
        assert "claude-code" in providers_mod.PROVIDERS
        assert "codex" in providers_mod.PROVIDERS

    def test_resolve_honors_explicit_name_arg(self, providers_mod, monkeypatch):
        monkeypatch.delenv("BRAIN_LLM_PROVIDER", raising=False)
        # Force both is_available() True so we can prove the arg wins.
        with patch.object(
            providers_mod.PROVIDERS["claude-code"], "is_available",
            return_value=(True, ""),
        ), patch.object(
            providers_mod.PROVIDERS["codex"], "is_available",
            return_value=(True, ""),
        ):
            p = providers_mod.resolve_provider("codex")
            assert p.name == "codex"

    def test_resolve_honors_env_var(self, providers_mod, monkeypatch):
        monkeypatch.setenv("BRAIN_LLM_PROVIDER", "codex")
        with patch.object(
            providers_mod.PROVIDERS["claude-code"], "is_available",
            return_value=(True, ""),
        ), patch.object(
            providers_mod.PROVIDERS["codex"], "is_available",
            return_value=(True, ""),
        ):
            p = providers_mod.resolve_provider()
            assert p.name == "codex"

    def test_resolve_falls_back_to_first_available(self, providers_mod,
                                                   monkeypatch):
        """When no explicit selection, walk PROVIDERS in registration
        order and pick the first whose is_available() is True. Pins the
        priority so we don't accidentally flip the default later."""
        monkeypatch.delenv("BRAIN_LLM_PROVIDER", raising=False)
        with patch.object(
            providers_mod.PROVIDERS["claude-code"], "is_available",
            return_value=(False, "no auth"),
        ), patch.object(
            providers_mod.PROVIDERS["codex"], "is_available",
            return_value=(True, ""),
        ):
            p = providers_mod.resolve_provider()
            assert p.name == "codex"

    def test_resolve_raises_when_nothing_available(self, providers_mod,
                                                   monkeypatch, base_mod):
        monkeypatch.delenv("BRAIN_LLM_PROVIDER", raising=False)
        with patch.object(
            providers_mod.PROVIDERS["claude-code"], "is_available",
            return_value=(False, "missing CLAUDE_CODE_OAUTH_TOKEN"),
        ), patch.object(
            providers_mod.PROVIDERS["codex"], "is_available",
            return_value=(False, "codex CLI not on PATH"),
        ):
            with pytest.raises(base_mod.ProviderNotAvailable) as ei:
                providers_mod.resolve_provider()
            msg = str(ei.value)
            assert "claude-code" in msg and "codex" in msg

    def test_resolve_with_unknown_name_raises_value_error(self,
                                                          providers_mod):
        with pytest.raises(ValueError):
            providers_mod.resolve_provider("nonexistent-provider")

    def test_resolve_honors_config_toml(self, providers_mod, monkeypatch,
                                         tmp_path):
        """Resolution order: arg > env > config.toml > first-available.
        Config-file selection must trump the auto-detected default."""
        monkeypatch.delenv("BRAIN_LLM_PROVIDER", raising=False)
        config = tmp_path / "config.toml"
        config.write_text('llm_provider = "codex"\n')
        monkeypatch.setenv("BRAIN_CONFIG", str(config))
        with patch.object(providers_mod.PROVIDERS["claude-code"],
                          "is_available", return_value=(True, "")), \
             patch.object(providers_mod.PROVIDERS["codex"],
                          "is_available", return_value=(True, "")):
            p = providers_mod.resolve_provider()
        assert p.name == "codex"

    def test_resolve_precedence_env_beats_config(self, providers_mod,
                                                  monkeypatch, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('llm_provider = "codex"\n')
        monkeypatch.setenv("BRAIN_CONFIG", str(config))
        monkeypatch.setenv("BRAIN_LLM_PROVIDER", "claude-code")
        with patch.object(providers_mod.PROVIDERS["claude-code"],
                          "is_available", return_value=(True, "")), \
             patch.object(providers_mod.PROVIDERS["codex"],
                          "is_available", return_value=(True, "")):
            p = providers_mod.resolve_provider()
        assert p.name == "claude-code"


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------

class TestClaudeCodeProvider:
    def _provider(self, providers_mod):
        return providers_mod.PROVIDERS["claude-code"]

    def test_name_and_default_model(self, providers_mod):
        p = self._provider(providers_mod)
        assert p.name == "claude-code"
        # Haiku is the cost-optimal default for summarization workloads.
        assert "haiku" in p.default_model.lower()

    def test_is_available_when_cli_missing(self, providers_mod, monkeypatch):
        p = self._provider(providers_mod)
        with patch("shutil.which", return_value=None):
            ok, reason = p.is_available()
        assert ok is False
        assert "claude" in reason.lower()

    def test_is_available_does_not_require_api_key(self, providers_mod,
                                                    monkeypatch):
        """Framework contract: subscription-billed via existing claude
        login, NOT a separate ANTHROPIC_API_KEY. is_available() must
        return True when the CLI exists, regardless of API key env vars.

        If this test fails, the provider has accidentally introduced an
        API-key dependency the user explicitly didn't want."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        p = self._provider(providers_mod)
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            ok, reason = p.is_available()
        assert ok is True, (
            f"claude-code must not require ANTHROPIC_API_KEY when CLI "
            f"is present (got reason: {reason!r})"
        )

    def test_invoke_builds_expected_subprocess_command(self, providers_mod):
        """Lock down the CLI flag surface. If `claude -p` changes flag
        names upstream, this test should be the first thing to break."""
        p = self._provider(providers_mod)
        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({
                    "type": "result", "subtype": "success",
                    "is_error": False,
                    "result": json.dumps({"answer": "ok"}),
                    "usage": {"input_tokens": 9, "output_tokens": 5},
                    "total_cost_usd": 0.01,
                    "modelUsage": {"claude-haiku-4-5": {
                        "inputTokens": 9, "outputTokens": 5
                    }},
                }),
                stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            r = p.invoke(
                system="You are a summarizer.",
                prompt="Summarize: hello world",
                model="haiku",
                json_schema={"type": "object",
                             "properties": {"answer": {"type": "string"}},
                             "required": ["answer"]},
                max_budget_usd=0.05,
                timeout_s=30,
            )

        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd and "json" in cmd
        assert "--model" in cmd and "haiku" in cmd
        assert "--max-budget-usd" in cmd
        # Schema must be JSON-serialized and passed via --json-schema
        idx = cmd.index("--json-schema")
        assert json.loads(cmd[idx + 1])["required"] == ["answer"]
        # The user prompt arrives on stdin (avoids argv length limits)
        assert "Summarize: hello world" in (captured["input"] or "")
        # Result parsing
        assert r.provider == "claude-code"
        assert r.parsed_json == {"answer": "ok"}
        assert r.tokens_in == 9
        assert r.tokens_out == 5

    def test_invoke_retries_once_on_schema_validation_failure(self,
                                                              providers_mod):
        """First call returns text that isn't valid JSON. Provider must
        retry once with a 'JSON only' reinforcement and succeed on the
        second. Two subprocess invocations, then a successful parse."""
        p = self._provider(providers_mod)
        calls = {"n": 0}

        def fake_run(cmd, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                result_payload = "this is not json prose only"
            else:
                result_payload = json.dumps({"answer": "ok"})
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({
                    "type": "result", "is_error": False,
                    "result": result_payload,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }),
                stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            r = p.invoke(
                system="sys", prompt="p",
                json_schema={"type": "object",
                             "properties": {"answer": {"type": "string"}},
                             "required": ["answer"]},
            )

        assert calls["n"] == 2, "must retry exactly once on parse fail"
        assert r.parsed_json == {"answer": "ok"}

    def test_invoke_raises_after_second_schema_failure(self, providers_mod):
        p = self._provider(providers_mod)
        from llm_providers.base import LLMError

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"type": "result", "is_error": False,
                                   "result": "still not json",
                                   "usage": {"input_tokens": 1,
                                             "output_tokens": 1}}),
                stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(LLMError):
                p.invoke(system="s", prompt="p",
                         json_schema={"type": "object",
                                      "required": ["answer"]})

    def test_invoke_surfaces_nonzero_exit(self, providers_mod):
        p = self._provider(providers_mod)
        from llm_providers.base import LLMError

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=2,
                stdout="", stderr="auth failed",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(LLMError, match="auth failed"):
                p.invoke(system="s", prompt="p")

    def test_invoke_surfaces_subprocess_timeout(self, providers_mod):
        p = self._provider(providers_mod)
        from llm_providers.base import LLMError

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(LLMError, match=r"(?i)timeout"):
                p.invoke(system="s", prompt="p", timeout_s=1)


# ---------------------------------------------------------------------------
# CodexProvider
# ---------------------------------------------------------------------------

class TestCodexProvider:
    def _provider(self, providers_mod):
        return providers_mod.PROVIDERS["codex"]

    def test_name(self, providers_mod):
        assert self._provider(providers_mod).name == "codex"

    def test_is_available_requires_cli_and_auth_file(self, providers_mod,
                                                     tmp_path, monkeypatch):
        p = self._provider(providers_mod)
        # CLI present but no auth.json
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            ok, reason = p.is_available()
        assert ok is False
        assert "auth" in reason.lower()
        # Now create auth file
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text("{}")
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            ok, reason = p.is_available()
        assert ok is True

    def test_invoke_builds_expected_command(self, providers_mod, tmp_path,
                                            monkeypatch):
        """Codex provider uses `codex exec --skip-git-repo-check` with
        prompt via stdin (verified via real `codex exec --help` output)."""
        p = self._provider(providers_mod)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text("{}")

        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            # Codex output: includes preamble, then "codex" marker, then text
            stdout = (
                "OpenAI Codex v0.125.0\n"
                "--------\nworkdir: /tmp\n--------\n"
                "user\nPrompt text\n\n"
                "codex\n"
                '{"answer": "ok"}\n'
                "tokens used\n2,462\n"
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.run", side_effect=fake_run):
            r = p.invoke(
                system="You are a summarizer.",
                prompt="Summarize: hello world",
                json_schema={"type": "object",
                             "properties": {"answer": {"type": "string"}},
                             "required": ["answer"]},
            )

        cmd = captured["cmd"]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--skip-git-repo-check" in cmd
        # Codex output post-hoc parsed: "answer" key must be present
        assert r.parsed_json == {"answer": "ok"}
        assert r.provider == "codex"
        # Codex has no native JSON schema flag — the schema is inlined into
        # the prompt as a directive so the model knows the expected shape.
        joined_input = captured["input"] or ""
        assert "JSON" in joined_input or "json" in joined_input

    def test_invoke_retries_when_no_json_in_output(self, providers_mod,
                                                   tmp_path, monkeypatch):
        p = self._provider(providers_mod)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text("{}")

        calls = {"n": 0}

        def fake_run(cmd, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                stdout = "codex\nI can't help with that.\n"
            else:
                stdout = 'codex\n{"answer": "ok"}\n'
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.run", side_effect=fake_run):
            r = p.invoke(
                system="s", prompt="p",
                json_schema={"type": "object", "required": ["answer"]},
            )
        assert calls["n"] == 2
        assert r.parsed_json == {"answer": "ok"}

    def test_invoke_surfaces_nonzero_exit(self, providers_mod, tmp_path,
                                          monkeypatch):
        p = self._provider(providers_mod)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text("{}")
        from llm_providers.base import LLMError

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="codex failed",
            )

        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(LLMError, match="codex failed"):
                p.invoke(system="s", prompt="p")

    def test_invoke_surfaces_timeout(self, providers_mod, tmp_path,
                                      monkeypatch):
        p = self._provider(providers_mod)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text("{}")
        from llm_providers.base import LLMError

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))

        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(LLMError, match=r"(?i)timeout"):
                p.invoke(system="s", prompt="p", timeout_s=1)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidationRetry:
    """Parsing-as-JSON is not enough. The response must MATCH the schema
    (required fields present, correct types). A response that parses but
    lacks `required` keys must trigger the same retry path as malformed
    JSON."""

    def test_claude_retries_on_schema_violation(self, providers_mod):
        p = providers_mod.PROVIDERS["claude-code"]
        calls = {"n": 0}

        def fake_run(cmd, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Parses but missing required key
                result_payload = json.dumps({"wrong_key": "x"})
            else:
                result_payload = json.dumps({"answer": "ok"})
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({
                    "type": "result", "is_error": False,
                    "result": result_payload,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }),
                stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            r = p.invoke(
                system="s", prompt="p",
                json_schema={"type": "object",
                             "properties": {"answer": {"type": "string"}},
                             "required": ["answer"]},
            )
        assert calls["n"] == 2
        assert r.parsed_json == {"answer": "ok"}

    def test_retry_prompt_reinforces_json_only(self, providers_mod):
        """The retry must add a stricter directive. A naive retry that
        sends the same prompt would just get the same bad response."""
        p = providers_mod.PROVIDERS["claude-code"]
        prompts_seen = []

        def fake_run(cmd, *args, **kwargs):
            prompts_seen.append(kwargs.get("input") or "")
            if len(prompts_seen) == 1:
                result = "not json prose only"
            else:
                result = json.dumps({"answer": "ok"})
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({
                    "type": "result", "is_error": False,
                    "result": result,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }),
                stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run):
            p.invoke(system="s", prompt="p",
                     json_schema={"type": "object", "required": ["answer"]})

        # Second prompt must differ from first — must contain a JSON-only
        # reinforcement of some kind.
        assert len(prompts_seen) == 2
        assert prompts_seen[1] != prompts_seen[0]
        assert "JSON" in prompts_seen[1] or "json" in prompts_seen[1]


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_digest_rate_sleep_s_env_honored(self, providers_mod, monkeypatch):
        """When DIGEST_RATE_SLEEP_S is set, providers should sleep that
        many seconds between consecutive invocations. Pins the
        rate-limit mitigation knob the plan promises."""
        monkeypatch.setenv("DIGEST_RATE_SLEEP_S", "0.05")
        p = providers_mod.PROVIDERS["claude-code"]

        sleeps = []
        def fake_sleep(s):
            sleeps.append(s)

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"type": "result", "is_error": False,
                                   "result": json.dumps({"x": 1}),
                                   "usage": {"input_tokens": 1,
                                             "output_tokens": 1}}),
                stderr="",
            )

        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("time.sleep", side_effect=fake_sleep):
            p.invoke(system="s", prompt="p1",
                     json_schema={"type": "object", "required": ["x"]})
            p.invoke(system="s", prompt="p2",
                     json_schema={"type": "object", "required": ["x"]})

        # At least one sleep of 0.05 between the two calls.
        assert any(abs(s - 0.05) < 1e-6 for s in sleeps), \
            f"expected a 0.05s throttle sleep, got {sleeps!r}"
