"""OpenAI Codex CLI provider.

Drives `codex exec --skip-git-repo-check` for headless inference. Auth
via the user's existing Codex / ChatGPT login (`~/.codex/auth.json`).

Output shape (verified on a real run):

    OpenAI Codex v0.125.0 (research preview)
    --------
    workdir: /tmp
    model: gpt-5.5
    --------
    user
    <echoed prompt>

    codex
    <model response>
    tokens used
    2,462

We extract the assistant region after the literal `codex\\n` marker
and discard the trailing `tokens used\\nN` block. When `json_schema`
is requested, Codex has no native flag; we inline a "JSON only matching
this schema" directive into the prompt and validate post-hoc, same
retry-on-failure semantics as the Claude provider.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .base import LLMProvider, LLMResult, LLMError


# Capture everything between the `codex\n` boundary and (`tokens used` |
# the literal `user\n` of a next turn | end of stream). Greedy on the
# content side; the lookahead keeps us before the trailing token report.
_CODEX_REPLY_RE = re.compile(
    r"(?:^|\n)codex\n(?P<body>.*?)(?=\n+tokens used\b|\n+user\n|\Z)",
    re.DOTALL,
)


class CodexProvider(LLMProvider):
    name = "codex"
    # gpt-5.5 is the default on ChatGPT-account auth. gpt-5 raw is blocked
    # for ChatGPT-account users (verified empirically).
    default_model = "gpt-5.5"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("codex"):
            return (False, "codex CLI not on PATH — install OpenAI Codex CLI")
        auth = Path(os.environ.get("HOME", str(Path.home())))
        auth = auth / ".codex" / "auth.json"
        if not auth.is_file():
            return (False, f"no codex auth at {auth} — run `codex login`")
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
        """Same type-aware validation as ClaudeCodeProvider — keep
        provider behavior identical so a JSON-only response that
        validates for one validates for the other."""
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

    @classmethod
    def _extract_reply(cls, stdout: str) -> str:
        """Pull the assistant body out of the framing preamble + tokens
        trailer. Codex repeats the prompt verbatim before answering, so
        we must NOT confuse the echoed user text with the reply."""
        m = _CODEX_REPLY_RE.search(stdout)
        return m.group("body").strip() if m else stdout.strip()

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Try strict JSON first. Then fall back to the first balanced
        {...} substring (handles models that wrap JSON in prose despite
        being asked not to). None when nothing usable."""
        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        # Bracket-balanced first-object scan
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _build_cmd(model: str | None) -> list[str]:
        cmd = ["codex", "exec", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        return cmd

    @staticmethod
    def _wrap_prompt(system: str, prompt: str,
                     json_schema: dict | None) -> str:
        """Codex has no native system-prompt flag; we inline the system
        text and (when requested) a JSON-only directive."""
        parts: list[str] = []
        if system:
            parts.append("SYSTEM:\n" + system)
        if json_schema is not None:
            parts.append(
                "OUTPUT: Reply with ONLY a single JSON object containing "
                f"these required keys: {json_schema.get('required', [])}. "
                "No prose, no markdown fences. JSON only."
            )
        parts.append("USER:\n" + prompt)
        return "\n\n".join(parts)

    def _run_once(self, cmd: list[str], stdin_text: str,
                  timeout_s: int) -> str:
        self._throttle()
        try:
            res = subprocess.run(
                cmd, input=stdin_text, capture_output=True, text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"codex exec timeout after {timeout_s}s") from e
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            raise LLMError(f"codex exec exit={res.returncode}: {err[:500]}")
        return res.stdout

    @staticmethod
    def _count_tokens(stdout: str) -> int | None:
        """Codex prints `tokens used\\nN` at the end. Parse it for our
        cost accounting. Returns None if unparseable."""
        m = re.search(r"tokens used\s*\n\s*([\d,]+)", stdout)
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None

    def invoke(self, system: str, prompt: str, *,
               model: str | None = None,
               json_schema: dict | None = None,
               max_budget_usd: float = 0.10,  # ignored by Codex; kept for API parity
               timeout_s: int = 60) -> LLMResult:
        used_model = model or ""  # codex picks default when empty
        cmd = self._build_cmd(model or None)
        stdin = self._wrap_prompt(system, prompt, json_schema)

        stdout = self._run_once(cmd, stdin, timeout_s)
        reply = self._extract_reply(stdout)

        parsed: dict | None = None
        if json_schema is not None:
            cand = self._extract_json(reply)
            if cand is not None and self._validate_schema(cand, json_schema):
                parsed = cand

            if parsed is None:
                strict_prompt = (
                    f"{prompt}\n\n"
                    "Reply with ONLY a single JSON object matching the "
                    f"required keys: {json_schema.get('required', [])}. "
                    "No prose, no markdown code fences, JSON only."
                )
                stdin2 = self._wrap_prompt(system, strict_prompt, json_schema)
                stdout = self._run_once(cmd, stdin2, timeout_s)
                reply = self._extract_reply(stdout)
                cand = self._extract_json(reply)
                if cand is not None and self._validate_schema(cand,
                                                              json_schema):
                    parsed = cand
                if parsed is None:
                    raise LLMError(
                        "codex schema validation failed after retry; "
                        f"last reply preview: {reply[:200]!r}"
                    )

        tokens_total = self._count_tokens(stdout)
        # Codex's "tokens used" is the combined input+output count; we
        # don't get a split, so report it as input and leave output None.
        return LLMResult(
            text=reply,
            parsed_json=parsed,
            tokens_in=tokens_total,
            tokens_out=None,
            provider=self.name,
            model=used_model or self.default_model,
            cost_usd=None,  # subscription-billed
        )
