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
        enable_auto_recall = false
        auto_recall_k = 5
        auto_recall_budget_tokens = 1500
        auto_recall_timeout_ms = 3000
        auto_recall_min_chars = 8
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
    # Auto-recall: when enabled, the UserPromptSubmit hook fires recall
    # for every substantive user prompt and injects the top-K results as
    # additional context. Opt-in due to latency + retrieval-pollution risk.
    enable_auto_recall: bool = False
    auto_recall_k: int = 5
    auto_recall_budget_tokens: int = 1500
    # 3000ms default: every Claude Code hook is a fresh subprocess, so we
    # pay Python startup (~200ms) + recall imports (~300ms) + qdrant /
    # embedder load (~1500ms cold) per fire. 1500ms timed out on cold-start
    # in real-world testing. Users with warm setups can lower this; users
    # who hit timeouts can raise it. Tuned 2026-05-05 against live brain.
    auto_recall_timeout_ms: int = 3000
    auto_recall_min_chars: int = 8
    # Reject results below this similarity score before injecting. 0.0
    # disables the floor (all top-K results inject). On a hybrid retriever
    # with ~200 docs, ~0.30 cuts out the long-tail noise; raise toward
    # ~0.60 if you want only confident matches.
    auto_recall_min_score: float = 0.0
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
        # Lenient int parsing — malformed values fall back to defaults
        # rather than crashing every hook invocation.
        def _int(key: str, default: int) -> int:
            try:
                return int(section.get(key, default))
            except (TypeError, ValueError):
                return default

        def _float(key: str, default: float) -> float:
            try:
                return float(section.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            log_dir=log_dir,
            capture_raw=bool(section.get("capture_raw", False)),
            enable_reinjection=bool(section.get("enable_reinjection", False)),
            reinjection_budget_tokens=_int("reinjection_budget_tokens", 1500),
            enable_auto_recall=bool(section.get("enable_auto_recall", False)),
            auto_recall_k=_int("auto_recall_k", 5),
            auto_recall_budget_tokens=_int("auto_recall_budget_tokens", 1500),
            auto_recall_timeout_ms=_int("auto_recall_timeout_ms", 3000),
            auto_recall_min_chars=_int("auto_recall_min_chars", 8),
            auto_recall_min_score=_float("auto_recall_min_score", 0.0),
            budgets=budgets,
            config_path=path,
        )

    @staticmethod
    def _discover_config() -> Path | None:
        """Find the pyproject.toml that owns the [tool.recall.runtime] section.

        Order of search:
          1. $RECALL_RUNTIME_CONFIG (explicit override; not parsed for content)
          2. cwd / pyproject.toml — IF it has [tool.recall.runtime]
          3. ~/.agent/runtime/pyproject.toml — the default brainstack location

        Step 2 used to be "first existing file wins," which broke users who
        set `enable_auto_recall = true` (or `enable_reinjection = true`) in
        their global ~/.agent config but worked inside a project repo whose
        pyproject.toml had no [tool.recall.runtime] section: the project
        file shadowed the global one, returning the dataclass defaults.
        Codex 2026-05-05 MED. Now we fall through when cwd's file lacks
        the section.
        """
        env = os.environ.get("RECALL_RUNTIME_CONFIG")
        if env:
            p = Path(env).expanduser()
            return p if p.exists() else None
        cwd_pyproject = Path.cwd() / "pyproject.toml"
        if cwd_pyproject.exists() and _has_runtime_section(cwd_pyproject):
            return cwd_pyproject
        global_pyproject = Path("~/.agent/runtime/pyproject.toml").expanduser()
        if global_pyproject.exists():
            return global_pyproject
        # Last resort: cwd file even without the section (preserves prior
        # default-emission behavior on projects that have a pyproject but
        # no runtime config of their own)
        return cwd_pyproject if cwd_pyproject.exists() else None


def _has_runtime_section(path: Path) -> bool:
    """True iff `path` is a TOML file with a `[tool.recall.runtime]` table."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    section = data.get("tool", {}).get("recall", {}).get("runtime", {})
    return isinstance(section, dict) and bool(section)


__all__ = ["RuntimeConfig"]
