"""Base contract for LLM providers used by the session digest layer.

A provider is a thin Python wrapper around an LLM CLI the user already
has set up (Claude Code, Codex, etc). The goal is "no extra API key" —
the user pays via their existing subscription, not a separate Anthropic
or OpenAI bill.

Provider authors implement two methods:

  - `is_available()` — returns (True, "") if the CLI is installed and
    authed. Returns (False, "<one-line reason>") so a user who's missing
    a provider sees EXACTLY what to fix (e.g. "run `claude setup-token`").

  - `invoke(system, prompt, ...)` — sends a single inference request,
    handles output parsing + schema validation + one retry.

Adding a new provider is a one-file plugin: subclass `LLMProvider`,
implement those two methods, register in `llm_providers/__init__.py`.
See `llm_providers/__README.md` for a worked example.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(Exception):
    """Raised for any inference-time failure: nonzero exit, timeout,
    malformed output that survived the retry, schema-violation after
    retry. Adapter code catches this per-session so one bad session
    doesn't break a backfill loop."""


class ProviderNotAvailable(Exception):
    """Raised by resolve_provider() when no registered provider is
    available. Carries the per-provider skip reasons so the message
    tells the user exactly what to do for each option (e.g. "claude-code:
    run `claude setup-token`; codex: missing ~/.codex/auth.json — run
    `codex login`"). New users hitting this should never have to read
    source to know what's wrong."""

    def __init__(self, reasons: dict[str, str]):
        self.reasons = dict(reasons)
        # Stable, readable join — every reason on its own line.
        parts = [f"{name}: {reason}" for name, reason in reasons.items()]
        super().__init__("no LLM provider available — " + "; ".join(parts))


@dataclass
class LLMResult:
    """Structured return from `LLMProvider.invoke`.

    `parsed_json` is populated when the caller passed a `json_schema`
    AND the response validated against it. Otherwise None (caller is
    expected to consume `.text` directly).

    `cost_usd` is None when the provider is subscription-billed (the
    canonical case for brainstack). It carries the per-call dollar
    estimate when available (e.g. claude -p reports `total_cost_usd`
    in its JSON envelope) so the backfill can log spend even when no
    real money changes hands.
    """
    text: str
    parsed_json: dict | None
    tokens_in: int | None
    tokens_out: int | None
    provider: str
    model: str
    cost_usd: float | None


class LLMProvider(ABC):
    """Abstract base. Subclasses must set `name` + `default_model` as
    class-level strings and implement `is_available` + `invoke`."""

    name: str = ""
    default_model: str = ""

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (True, "") when ready to invoke. Return (False, "<reason>")
        otherwise; the reason is the user-facing fix-it text. Must never
        raise — auto-detect walks every provider and a single raising
        provider would mask the others."""

    @abstractmethod
    def invoke(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
        json_schema: dict | None = None,
        max_budget_usd: float = 0.10,
        timeout_s: int = 60,
    ) -> LLMResult:
        """Run one inference. Implementations MUST:

        - retry exactly once when `json_schema` is given and the first
          response either fails to parse as JSON or parses but doesn't
          satisfy `schema["required"]`. The retry prompt must include a
          stricter "JSON only — no prose" directive so the model knows
          why it's being re-asked.
        - raise `LLMError` for: non-zero CLI exit, subprocess timeout,
          malformed output that survives the retry, schema-validation
          failure after retry.
        - honor `DIGEST_RATE_SLEEP_S` env var by sleeping that many
          seconds BEFORE invoking the subprocess (so two consecutive
          calls are throttled regardless of which call did the work).
        """
