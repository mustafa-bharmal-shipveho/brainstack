"""LLM provider registry + auto-detection for the digest layer.

`PROVIDERS` is the central registry. Each entry is an instantiated
provider (the ABC has no constructor args). `resolve_provider(name)`
returns the active provider for a call, honoring this precedence:

    1. explicit `name` argument
    2. $BRAIN_LLM_PROVIDER env var
    3. `llm_provider` key in $BRAIN_CONFIG (TOML)
    4. first registered provider whose `is_available()` returns True

If nothing matches, raises `ProviderNotAvailable` with per-provider skip
reasons so the user sees exactly what to install or authenticate.
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import LLMProvider, LLMResult, LLMError, ProviderNotAvailable
from .claude_code import ClaudeCodeProvider
from .codex import CodexProvider


# Order matters: the first registered provider is also the auto-detect
# tiebreaker when multiple are available. Claude is the default because
# it has native JSON-schema enforcement (cleaner contract for digests)
# and Haiku is faster than Codex's default gpt-5.5 with reasoning xhigh.
PROVIDERS: dict[str, LLMProvider] = {
    "claude-code": ClaudeCodeProvider(),
    "codex":       CodexProvider(),
}


def _load_config_provider() -> str | None:
    """Read `llm_provider` from the TOML config file pointed to by
    $BRAIN_CONFIG. Missing file, missing key, or parse error → None
    (auto-detect falls through). Never raises.

    Uses tomllib when available (Python 3.11+). On older Pythons (the
    repo still supports 3.9 in some places) falls back to a minimal
    regex for the single key we care about. The config format is
    deliberately tiny so this fallback is safe."""
    cfg_path = os.environ.get("BRAIN_CONFIG")
    if not cfg_path:
        return None
    p = Path(cfg_path).expanduser()
    if not p.is_file():
        return None
    val: object = None
    try:
        import tomllib  # type: ignore[import-not-found]
        with open(p, "rb") as f:
            data = tomllib.load(f)
        val = data.get("llm_provider")
    except ImportError:
        import re
        try:
            text = p.read_text()
        except OSError:
            return None
        # Match `llm_provider = "x"` or `llm_provider="x"`, allow
        # leading whitespace + line comments. First match wins; nested
        # tables aren't supported by the fallback (and aren't needed).
        m = re.search(
            r"^\s*llm_provider\s*=\s*(\"[^\"]*\"|'[^']*')\s*(?:#.*)?$",
            text,
            re.MULTILINE,
        )
        if m:
            raw = m.group(1)
            val = raw[1:-1]
    except Exception:
        return None
    return val if isinstance(val, str) and val else None


def resolve_provider(name: str | None = None) -> LLMProvider:
    """Return the active provider. See module docstring for precedence."""
    # 1. explicit arg
    if name:
        if name not in PROVIDERS:
            raise ValueError(
                f"unknown LLM provider {name!r}; "
                f"registered: {sorted(PROVIDERS)}"
            )
        return PROVIDERS[name]

    # 2. env var
    env = os.environ.get("BRAIN_LLM_PROVIDER")
    if env:
        if env not in PROVIDERS:
            raise ValueError(
                f"BRAIN_LLM_PROVIDER={env!r} is not a registered provider; "
                f"registered: {sorted(PROVIDERS)}"
            )
        return PROVIDERS[env]

    # 3. config.toml
    cfg = _load_config_provider()
    if cfg:
        if cfg not in PROVIDERS:
            raise ValueError(
                f"config.toml llm_provider={cfg!r} is not registered; "
                f"registered: {sorted(PROVIDERS)}"
            )
        return PROVIDERS[cfg]

    # 4. first available
    reasons: dict[str, str] = {}
    for pname, p in PROVIDERS.items():
        ok, reason = p.is_available()
        if ok:
            return p
        reasons[pname] = reason or "unavailable (no reason given)"
    raise ProviderNotAvailable(reasons)


__all__ = [
    "PROVIDERS",
    "resolve_provider",
    "LLMProvider",
    "LLMResult",
    "LLMError",
    "ProviderNotAvailable",
]
