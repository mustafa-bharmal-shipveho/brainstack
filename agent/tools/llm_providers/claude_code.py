"""Claude Code CLI provider.

Drives `claude -p` for headless inference. Auth via the user's existing
Claude subscription — no separate ANTHROPIC_API_KEY required (the user
either has CLAUDE_CODE_OAUTH_TOKEN set or has logged in interactively
via `claude` and credentials live in the CLI's keychain entry).

Output parsing follows the documented `claude -p --output-format json`
envelope:

    {"type":"result","subtype":"success","is_error":false,
     "result":"<model text>",
     "usage":{"input_tokens":N,"output_tokens":N,...},
     "total_cost_usd":0.0089,
     "modelUsage":{"<model>":{"inputTokens":N,...}}}

When the caller supplies a `json_schema`, we pass it via
`--json-schema <serialized>` so Claude returns structured JSON, then
validate the result against the schema's required-keys list. On parse
or schema failure we retry once with a strict "JSON only" directive.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from .base import LLMProvider, LLMResult, LLMError


class ClaudeCodeProvider(LLMProvider):
    name = "claude-code"
    # Haiku 4.5 is the cost-optimal default for summarization.
    default_model = "claude-haiku-4-5"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return (False, "claude CLI not on PATH — install Claude Code")
        # When the CLI is present, treat the provider as available even
        # without ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN in env.
        # The framework promise is subscription-billed via existing
        # login. If the user truly isn't authed, the first invoke()
        # will surface a clear non-zero exit + auth-failure message.
        return (True, "")

    # -- internal helpers ----------------------------------------------------

    @staticmethod
    def _throttle() -> None:
        s = os.environ.get("DIGEST_RATE_SLEEP_S")
        if not s:
            return
        try:
            sec = float(s)
        except ValueError:
            return
        if sec > 0:
            time.sleep(sec)

    @staticmethod
    def _validate_schema(payload: object, schema: dict | None) -> bool:
        """Schema validation: object, all required keys present, AND
        per-property type matches when declared. Avoids a `jsonschema`
        dep — checks just the cases the digest schema uses today
        (`string`, `integer`, `array`). An out-of-shape response now
        triggers the retry path instead of silently passing."""
        if schema is None:
            return True
        if not isinstance(payload, dict):
            return False
        required = schema.get("required") or []
        if not all(k in payload for k in required):
            return False
        props = schema.get("properties") or {}
        for key, prop in props.items():
            if key not in payload:
                continue
            t = prop.get("type")
            v = payload[key]
            if t == "string" and not isinstance(v, str):
                return False
            if t == "integer" and not isinstance(v, int):
                return False
            if t == "array" and not isinstance(v, list):
                return False
            if t == "object" and not isinstance(v, dict):
                return False
        return True

    @staticmethod
    def _build_cmd(model: str, json_schema: dict | None,
                   max_budget_usd: float) -> list[str]:
        cmd = [
            "claude", "-p", "--print",
            "--output-format", "json",
            "--model", model,
            "--max-budget-usd", str(max_budget_usd),
        ]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema)]
        return cmd

    def _run_once(self, cmd: list[str], stdin_text: str,
                  timeout_s: int) -> dict:
        """One subprocess invocation. Returns parsed JSON envelope dict.
        Raises LLMError on timeout / non-zero exit / unparseable envelope."""
        self._throttle()
        try:
            res = subprocess.run(
                cmd, input=stdin_text, capture_output=True, text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude -p timeout after {timeout_s}s") from e
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            raise LLMError(
                f"claude -p exit={res.returncode}: {err[:500]}"
            )
        try:
            envelope = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"claude -p returned non-JSON envelope: {res.stdout[:500]}"
            ) from e
        return envelope

    def invoke(self, system: str, prompt: str, *,
               model: str | None = None,
               json_schema: dict | None = None,
               max_budget_usd: float = 0.10,
               timeout_s: int = 60) -> LLMResult:
        model = model or self.default_model
        cmd = self._build_cmd(model, json_schema, max_budget_usd)
        # `system` is concatenated into stdin as a leading directive so it
        # survives without CLI-side system-prompt flag plumbing. Claude
        # Code's own system prompt still runs ahead of this.
        stdin = f"SYSTEM:\n{system}\n\nUSER:\n{prompt}\n" if system else prompt

        envelope = self._run_once(cmd, stdin, timeout_s)
        result_text = envelope.get("result", "")
        usage = envelope.get("usage") or {}
        cost = envelope.get("total_cost_usd")

        parsed: dict | None = None
        if json_schema is not None:
            try:
                cand = json.loads(result_text)
                if self._validate_schema(cand, json_schema):
                    parsed = cand
            except json.JSONDecodeError:
                parsed = None

            if parsed is None:
                # Retry once with a stricter directive. Different prompt
                # → won't get the same bad response.
                strict_prompt = (
                    f"{prompt}\n\n"
                    "Reply with ONLY a single JSON object matching the "
                    f"required keys: {json_schema.get('required', [])}. "
                    "No prose, no markdown code fences, JSON only."
                )
                stdin2 = (f"SYSTEM:\n{system}\n\nUSER:\n{strict_prompt}\n"
                          if system else strict_prompt)
                envelope = self._run_once(cmd, stdin2, timeout_s)
                result_text = envelope.get("result", "")
                usage = envelope.get("usage") or {}
                cost = envelope.get("total_cost_usd", cost)
                try:
                    cand = json.loads(result_text)
                    if self._validate_schema(cand, json_schema):
                        parsed = cand
                except json.JSONDecodeError:
                    parsed = None
                if parsed is None:
                    raise LLMError(
                        "claude -p schema validation failed after retry; "
                        f"last result preview: {result_text[:200]!r}"
                    )

        return LLMResult(
            text=result_text,
            parsed_json=parsed,
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
            provider=self.name,
            model=model,
            cost_usd=cost,
        )
