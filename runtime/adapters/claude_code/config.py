"""Runtime configuration loaded from pyproject.toml [tool.recall.runtime].

For v0.2 we read pyproject.toml in a deterministic search order:
  1. $RECALL_RUNTIME_CONFIG (explicit path override)
  2. cwd / pyproject.toml
  3. ~/.agent/runtime/pyproject.toml (brainstack default location)

If nothing is found we fall back to safe defaults: log path under
~/.agent/runtime/logs/, modest budgets, LRU policy.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


_DEFAULT_BUDGETS: dict[str, int] = {
    "claude_md": 4000,
    "hot": 2000,
    "retrieved": 20000,
    "scratchpad": 10000,
}

_DEFAULT_TOOL_BUCKET = "retrieved"

_TOOL_BUCKET_OVERRIDES: dict[str, str] = {
    # User-driven hot edits go to scratchpad by default; users may pin.
    "Edit": "scratchpad",
    "Write": "scratchpad",
}


@dataclass
class RuntimeConfig:
    """The set of values the adapter needs to operate.

    Loaded from pyproject.toml [tool.recall.runtime]. Schema:
        [tool.recall.runtime]
        log_dir = "~/.agent/runtime/logs"
        capture_raw = false
        enable_reinjection = false
        reinjection_budget_tokens = 1500
        [tool.recall.runtime.budget]
        claude_md = 4000
        hot = 2000
        retrieved = 20000
        scratchpad = 10000
    """

    log_dir: Path = field(default_factory=lambda: Path("~/.agent/runtime/logs").expanduser())
    capture_raw: bool = False
    enable_reinjection: bool = False
    reinjection_budget_tokens: int = 1500
    budgets: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_BUDGETS))
    tool_bucket_overrides: dict[str, str] = field(default_factory=lambda: dict(_TOOL_BUCKET_OVERRIDES))
    config_path: Path | None = None

    @property
    def event_log_path(self) -> Path:
        return self.log_dir / "events.log.jsonl"

    @property
    def manifest_dir(self) -> Path:
        return self.log_dir / "manifest"

    def tool_to_bucket(self, tool_name: str) -> str:
        return self.tool_bucket_overrides.get(tool_name, _DEFAULT_TOOL_BUCKET)

    @classmethod
    def load(cls, *, config_path: Path | None = None) -> "RuntimeConfig":
        path = config_path or cls._discover_config()
        if path is None:
            return cls()
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return cls()
        section = data.get("tool", {}).get("recall", {}).get("runtime", {})
        if not isinstance(section, dict):
            return cls()
        budgets = dict(_DEFAULT_BUDGETS)
        section_budgets = section.get("budget")
        if isinstance(section_budgets, dict):
            for k, v in section_budgets.items():
                try:
                    budgets[str(k)] = int(v)
                except (TypeError, ValueError):
                    pass
        log_dir = Path(str(section.get("log_dir", "~/.agent/runtime/logs"))).expanduser()
        return cls(
            log_dir=log_dir,
            capture_raw=bool(section.get("capture_raw", False)),
            enable_reinjection=bool(section.get("enable_reinjection", False)),
            reinjection_budget_tokens=int(section.get("reinjection_budget_tokens", 1500)),
            budgets=budgets,
            config_path=path,
        )

    @staticmethod
    def _discover_config() -> Path | None:
        env = os.environ.get("RECALL_RUNTIME_CONFIG")
        if env:
            p = Path(env).expanduser()
            return p if p.exists() else None
        candidates = [
            Path.cwd() / "pyproject.toml",
            Path("~/.agent/runtime/pyproject.toml").expanduser(),
        ]
        for c in candidates:
            if c.exists():
                return c
        return None


__all__ = ["RuntimeConfig"]
