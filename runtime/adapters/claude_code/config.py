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
    # 1500ms default (was 3000ms pre-S3): a warm recall daemon answers in
    # ~60-130ms + rerank, so the hard bound on the whole worker no longer
    # needs to absorb a cold qdrant/embedder load. Cold in-process fallback
    # (daemon down) will legitimately time out under this bound; that is
    # reported honestly via x_outcome=timeout rather than silently widened.
    auto_recall_timeout_ms: int = 1500
    auto_recall_min_chars: int = 8
    # Reject results below this similarity score before injecting. 0.0
    # disables the floor (all top-K results inject). On a hybrid retriever
    # with ~200 docs, ~0.30 cuts out the long-tail noise; raise toward
    # ~0.60 if you want only confident matches.
    auto_recall_min_score: float = 0.0
    # S3 (warm daemon): socket connect+respond budget. Effective budget is
    # min(this, auto_recall_timeout_ms) — the daemon path must still respect
    # the hook's overall wall clock.
    auto_recall_daemon_budget_ms: int = 800
    # Resolved via recall.config.daemon_socket_path (env override > this
    # literal > $BRAIN_ROOT/runtime > $BRAIN_HOME parent > ~/.agent). The
    # "$BRAIN_ROOT" literal is expanded at resolution time, not load time,
    # so it always reflects the CURRENT brain root.
    auto_recall_daemon_socket: str = "$BRAIN_ROOT/runtime/recall.sock"
    # S4 relevance gate: reject candidates below this cross-encoder score.
    # 0.0 = gate off (matches pre-S4 behavior). Calibrated value is written
    # to the user's global runtime config by eval/calibrate_rerank_gate.py.
    auto_recall_min_rerank: float = 0.0
    # Per-session dedup store kill switch (S2). True = don't re-inject a
    # doc whose content hasn't changed since it was last shown this session.
    auto_recall_dedup: bool = True
    budgets: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_BUDGETS))
    tool_bucket_overrides: dict[str, str] = field(default_factory=lambda: dict(_TOOL_BUCKET_OVERRIDES))
    config_path: Path | None = None
    # Ordered list (highest precedence first) of the config layers that
    # contributed to this instance. Today `load()` still reads a single
    # file, so this is `[config_path]` or `[]` — the per-key layered merge
    # (S1) will populate it with every layer consulted.
    config_layers: list[Path] = field(default_factory=list)

    @property
    def event_log_path(self) -> Path:
        return self.log_dir / "events.log.jsonl"

    @property
    def manifest_dir(self) -> Path:
        return self.log_dir / "manifest"

    @property
    def injected_dir(self) -> Path:
        """Per-session dedup store directory (S2), sibling to the event log
        so install.sh and the prune pass have exactly one directory tree to
        reason about."""
        return self.log_dir / "injected"

    @property
    def daemon_socket_path(self) -> Path:
        """Resolved socket path for the warm recall daemon (S3).

        Delegates to `recall.config.daemon_socket_path`, the single
        resolution order shared by the hook, the CLI, and the daemon
        itself. Imported lazily to avoid a hard import-time dependency
        from this adapter module onto `recall`.
        """
        from recall.config import daemon_socket_path as _daemon_socket_path

        return _daemon_socket_path(self.auto_recall_daemon_socket)

    def tool_to_bucket(self, tool_name: str) -> str:
        return self.tool_bucket_overrides.get(tool_name, _DEFAULT_TOOL_BUCKET)

    @staticmethod
    def global_config_path() -> Path:
        """The lowest-precedence file layer: `$BRAIN_ROOT/runtime/pyproject.toml`,
        defaulting to `~/.agent/runtime/pyproject.toml` when `$BRAIN_ROOT` is
        unset."""
        return (
            Path(os.environ.get("BRAIN_ROOT") or "~/.agent").expanduser()
            / "runtime"
            / "pyproject.toml"
        )

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
            # `load()` still reads a single file today (the per-key layered
            # merge across $RECALL_RUNTIME_CONFIG / cwd / global lands in a
            # later slice); record that one file as the sole layer so
            # `config_layers` is at least truthful about current behavior.
            config_layers=[path],
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
