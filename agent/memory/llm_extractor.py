"""LLM-based claim extractor — framework-shaped, uses the user's own LLM.

Why: the v1.1 `HeuristicExtractor` requires explicit predicate shapes
(`status: blocked`, `launches on YYYY-MM-DD`) that real conversational
text doesn't follow. The LLM extractor reads each event body through
the user's existing LLM CLI (Claude Code or Codex — whatever's already
authed) and maps the response back into the same `Claim` tuples that
`HeuristicExtractor` produces.

Framework contract (HARD):
  - **Pluggable** — implements the same `TopicKeyExtractor` Protocol
    as `HeuristicExtractor`. Drop-in replacement. The consolidator
    never knows which one is running.
  - **Producer-agnostic** — the prompt NEVER references `event["source"]`
    or any producer name. Same body → same claims regardless of who
    wrote it. Enforced by AC-6 AST scan.
  - **Predicate library is config-driven** — the prompt is BUILT from
    the `ExtractorConfig.predicates` dict. Adding a new predicate is a
    `~/.config/brainstack/extractors.toml` edit, not a code change.
  - **Uses the user's LLM** — `resolve_provider()` honors the user's
    existing CLI auth (Claude Code subscription or Codex). No new API
    key required. Provider precedence: explicit arg → `BRAIN_LLM_PROVIDER`
    env → `BRAIN_CONFIG` toml → auto-detect first-available.
  - **Cacheable + idempotent** — per-event cache makes re-runs free
    AND deterministic (AC-7 still holds).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional

import topic_keys
from _atomic import atomic_write_bytes


# Bump to invalidate the cache on prompt/schema changes.
LLM_EXTRACTOR_SCHEMA_VERSION = "1"

# Truncate body before sending — Slack messages are short; long ones
# get the middle elided rather than the tail (claims tend to appear at
# the start or end of a structured message).
_MAX_BODY_CHARS = 4000

# Per-call budget. Each event is one short prompt.
_MAX_BUDGET_USD = 0.02


_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _cache_dir(brain_root: str, namespace: str = "default") -> str:
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = os.path.abspath(brain_root)
    if namespace == "default":
        return os.path.join(root, "memory", "semantic",
                            "llm_extraction_cache")
    return os.path.join(root, "memory", "semantic", namespace,
                        "llm_extraction_cache")


def _safe_event_filename(event_id: str) -> str:
    """Hash event_id → fixed-length filename. Tolerates any character a
    producer might emit (Gmail-style ids with `/`, etc.)."""
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest() + ".json"


def _cache_path(brain_root: str, namespace: str, event_id: str) -> str:
    return os.path.join(_cache_dir(brain_root, namespace),
                        _safe_event_filename(event_id))


def _truncate_body(body: str) -> str:
    if len(body) <= _MAX_BODY_CHARS:
        return body
    head = _MAX_BODY_CHARS // 2 - 32
    tail = _MAX_BODY_CHARS // 2 - 32
    return (body[:head]
            + f"\n[...truncated {len(body) - head - tail} chars...]\n"
            + body[-tail:])


# JSON schema the LLM must produce. Strict so we can trust the output.
_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["topic_key", "claim_subject",
                             "value_normalized", "value_raw"],
                "properties": {
                    "topic_key": {"type": "string"},
                    "claim_subject": {"type": "string"},
                    "value_normalized": {"type": "string"},
                    "value_raw": {"type": "string"},
                },
            },
        }
    },
}


# Generic value-shape guidance per normalizer kind. The LLM is given
# only the shapes for normalizers that actually appear in the user's
# predicate config — so adding a custom normalizer in extractors.toml
# means adding its guidance here OR letting the LLM infer from the
# normalizer name. We treat this as a registry, not a switch.
_NORMALIZER_GUIDANCE: Dict[str, str] = {
    "date": "ISO date (YYYY-MM-DD). Skip if only relative ('Monday', "
            "'next week') with no absolute date nearby.",
    "enum": "one of: {enum_values}",
    "person": "lowercase display name or @-handle",
    "freeform-2k": "one-sentence summary, lowercased, no leading or "
                   "trailing whitespace, ≤200 chars",
}


def _build_system_prompt(config: topic_keys.ExtractorConfig) -> str:
    """Compose the system prompt from the configured predicate library.

    Adding a predicate to `extractors.toml` automatically expands the
    prompt — no code change required. This is the framework property
    the user asked for.
    """
    lines: List[str] = [
        "You extract structured facts (\"claims\") from a single short "
        "message body.",
        "",
        "Each claim has a claim_subject from this configured library:",
    ]
    for name, body in config.predicates.items():
        norm = str(body.get("normalizer") or "freeform-2k")
        guidance = _NORMALIZER_GUIDANCE.get(
            norm, f"value matching the '{norm}' normalizer"
        )
        if norm == "enum":
            ev = body.get("enum_values") or []
            guidance = _NORMALIZER_GUIDANCE["enum"].replace(
                "{enum_values}", " | ".join(str(v) for v in ev) or "unknown"
            )
        lines.append(f"  - \"{name}\" → value_normalized must be {guidance}")
    lines += [
        "",
        "topic_key is one of:",
        "  - project:<name>     → project codes (PS2, OKR, MYPROJ, …)",
        "  - team:<name>        → team or channel-driven topic",
        "  - person:<id>        → a person",
        "  - channel:<id>       → fallback when only a channel id is known",
        "",
        "RULES (strict):",
        "  1. Extract ONLY when the message asserts a clear fact. Skip "
        "casual chat, questions, hypotheticals, negated statements, "
        "and anything ambiguous.",
        "  2. Each emitted claim must have a clear noun (topic_key) AND "
        "a clear predicate (claim_subject) AND a normalized value.",
        "  3. Skip self-replies that lack a topic ('thanks', 'got it', "
        "single-emoji reactions, URLs alone, one-word acks).",
        "  4. Output an empty array if no claim should be emitted — "
        "this is the CORRECT answer for most messages.",
        "  5. Each event must emit at most ONE claim per "
        "(topic_key, claim_subject) slot.",
        "  6. value_raw is a short excerpt from the body (≤200 chars) "
        "that supports the claim.",
        "",
        "NEVER let the message's source channel or producer influence "
        "extraction. Same body → same claims regardless of who wrote it.",
        "",
        "Output ONLY a JSON object matching this exact schema (no prose, "
        "no markdown fences):",
        "  {\"claims\": [{\"topic_key\": \"...\", \"claim_subject\": \"...\", "
        "\"value_normalized\": \"...\", \"value_raw\": \"...\"}, ...]}",
    ]
    return "\n".join(lines)


def _build_user_prompt(event: Dict[str, Any]) -> str:
    """Render the per-event user prompt. The framework rule means we
    DO NOT include `event["source"]`. Optional fields documented in
    the producer contract (counterparty, channel_id, channel_type) are
    fair game — `HeuristicExtractor` uses them for opportunistic topic
    keys too.
    """
    body = _truncate_body(event.get("body_redacted") or "")
    parts = [f"BODY:\n{body}"]
    cp = event.get("counterparty")
    if cp:
        parts.append(f"\nCOUNTERPARTY: {cp}")
    ch_id = event.get("channel_id")
    ch_ty = event.get("channel_type")
    if ch_id:
        parts.append(f"\nCHANNEL_ID: {ch_id}")
    if ch_ty:
        parts.append(f"\nCHANNEL_TYPE: {ch_ty}")
    parts.append("\n\nExtract claims (JSON only).")
    return "".join(parts)


def _parse_claims(parsed: Dict[str, Any]) -> List[topic_keys.Claim]:
    """Validate + normalize the LLM's response into Claim tuples.

    Skips items that fail light validation, enforces storage-layer
    invariant (at most one claim per (topic, subject) per event).
    """
    out: List[topic_keys.Claim] = []
    seen_slots: set = set()
    items = parsed.get("claims") or []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        tk = str(item.get("topic_key") or "").strip()
        cs = str(item.get("claim_subject") or "").strip()
        vn = str(item.get("value_normalized") or "").strip()
        vr = str(item.get("value_raw") or "").strip()
        if not tk or not cs or not vn:
            continue
        slot = (tk, cs)
        if slot in seen_slots:
            continue
        seen_slots.add(slot)
        out.append(topic_keys.Claim(
            topic_key=tk,
            claim_subject=cs,
            value_normalized=vn,
            value_raw=vr or vn,
        ))
    return out


def _read_cache(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("schema_version") != LLM_EXTRACTOR_SCHEMA_VERSION:
        return None
    return obj


def _write_cache(path: str, claims_payload: List[Dict[str, Any]],
                 provider: str, model: str) -> None:
    payload = {
        "schema_version": LLM_EXTRACTOR_SCHEMA_VERSION,
        "claims": claims_payload,
        "provider": provider,
        "model": model,
    }
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, data)


@dataclass
class LLMExtractor:
    """LLM-driven claim extractor. Implements `TopicKeyExtractor`.

    Construction is pure (no I/O, no LLM call, no provider resolution).
    The LLM provider is resolved lazily on the first `extract()` call
    via `resolve_provider()` — which honors the user's existing setup:

      1. explicit `provider_name` arg
      2. `BRAIN_LLM_PROVIDER` env var
      3. `BRAIN_CONFIG` toml `llm_provider` key
      4. auto-detect: first available registered provider (Claude Code
         then Codex). Subscription-billed via the user's CLI — no new
         API key, no separate Anthropic/OpenAI bill.
    """

    brain_root: str
    namespace: str = "default"
    provider_name: Optional[str] = None
    model: Optional[str] = None
    config: Optional[topic_keys.ExtractorConfig] = None
    _provider: object = None
    _system_prompt: str = ""

    def _resolve(self):
        if self._provider is not None:
            return self._provider, self._system_prompt
        # Resolve the LLM provider via the existing framework
        # abstraction — picks whatever the user has configured.
        repo_tools = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "tools"))
        brain_tools = os.path.join(self.brain_root, "tools")
        for d in (repo_tools, brain_tools):
            if os.path.isdir(d) and d not in sys.path:
                sys.path.insert(0, d)
        from llm_providers import resolve_provider  # noqa: WPS433
        provider = resolve_provider(self.provider_name)
        cfg = self.config or topic_keys.ExtractorConfig()
        prompt = _build_system_prompt(cfg)
        # Cache on the instance.
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_system_prompt", prompt)
        return provider, prompt

    def extract(self, event: Dict[str, Any]) -> List[topic_keys.Claim]:
        body = event.get("body_redacted") or ""
        if not isinstance(body, str) or not body.strip():
            return []
        event_id = str(event.get("event_id") or "")
        if not event_id:
            return []

        cache_path = _cache_path(self.brain_root, self.namespace, event_id)

        # Cache hit → return cached claims unchanged. Idempotent re-runs.
        cached = _read_cache(cache_path)
        if cached is not None:
            return _parse_claims({"claims": cached.get("claims") or []})

        # Resolve provider lazily. If the user has no LLM CLI configured,
        # silently skip — the consolidator will surface this via the
        # dream-cycle summary (events processed but no claims).
        try:
            provider, system_prompt = self._resolve()
        except Exception:
            return []

        try:
            result = provider.invoke(
                system=system_prompt,
                prompt=_build_user_prompt(event),
                model=self.model,
                json_schema=_OUTPUT_SCHEMA,
                max_budget_usd=_MAX_BUDGET_USD,
                timeout_s=30,
            )
        except Exception:
            return []

        parsed = result.parsed_json
        if not isinstance(parsed, dict):
            return []

        claims = _parse_claims(parsed)
        claims_payload = [
            {"topic_key": c.topic_key,
             "claim_subject": c.claim_subject,
             "value_normalized": c.value_normalized,
             "value_raw": c.value_raw}
            for c in claims
        ]
        try:
            _write_cache(cache_path, claims_payload,
                         provider=getattr(result, "provider", "") or "",
                         model=getattr(result, "model", "") or "")
        except OSError:
            pass
        return claims


__all__ = ["LLMExtractor", "LLM_EXTRACTOR_SCHEMA_VERSION",
           "_build_system_prompt"]
