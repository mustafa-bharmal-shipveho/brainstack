"""Runtime configuration loaded from pyproject.toml [tool.recall.runtime].

S1: `RuntimeConfig.load()` merges THREE file layers PER KEY (high to low
precedence), rather than picking one file and reading every key from it:

  1. $RECALL_RUNTIME_CONFIG (explicit path override; content trusted as-is)
  2. ./pyproject.toml — only when it carries a non-empty [tool.recall.runtime]
  3. global_config_path() — $BRAIN_ROOT/runtime/pyproject.toml, defaulting to
     ~/.agent/runtime/pyproject.toml when $BRAIN_ROOT is unset

For each key, the first layer that SETS it (and whose value coerces
cleanly) wins; a key nobody set, or that every layer got wrong, falls
through to the dataclass default. `[tool.recall.runtime.budget]` merges
the same way per sub-key. This is what stops a project pyproject.toml
that sets a single key (say `log_dir`) from silently resetting every OTHER
key — including a user's global `enable_auto_recall = true` — to defaults.

`load(config_path=...)` (explicit) stays a single-file read: no layering,
no global merge. See docs/runtime.md ("Configuration") for the full key
table and env vars.
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

    Loaded from pyproject.toml [tool.recall.runtime], per-key layered
    across $RECALL_RUNTIME_CONFIG / ./pyproject.toml / global_config_path()
    (see module docstring + docs/runtime.md "Configuration" for the full
    precedence rule and key table). Schema:
        [tool.recall.runtime]
        log_dir = "~/.agent/runtime/logs"
        capture_raw = false
        enable_reinjection = false
        reinjection_budget_tokens = 1500
        enable_auto_recall = false
        auto_recall_k = 5
        auto_recall_budget_tokens = 1500
        auto_recall_timeout_ms = 1500
        auto_recall_min_chars = 8
        auto_recall_min_score = 0.0
        auto_recall_daemon_budget_ms = 800
        auto_recall_daemon_socket = "$BRAIN_ROOT/runtime/recall.sock"
        auto_recall_min_rerank = -1.5  # unset/omitted, or "none" -> gate off
        auto_recall_dedup = true
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
    # Cross-encoder scores are raw logits (mostly negative), so `0.0` cannot
    # express a calibrated negative threshold — `None` is the "gate off"
    # sentinel instead; any float, including a negative one, enables the
    # gate. Calibrated value is written to the user's global runtime config
    # by eval/calibrate_rerank_gate.py.
    auto_recall_min_rerank: float | None = None
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
        """Load the runtime config.

        `config_path=...` (explicit) stays a single-file read: no layering,
        no global merge — `test_adapter_hooks.py` relies on this for a
        caller that already knows exactly which file it wants.

        Otherwise (S1): a per-key layered merge over, high to low,
        `$RECALL_RUNTIME_CONFIG` > `./pyproject.toml` (only if it carries a
        non-empty `[tool.recall.runtime]`) > `global_config_path()`. The
        first layer that SETS a given key wins that key; a malformed value
        in a layer is treated as "not set" and falls through to the next
        layer, then to the dataclass default. `budget` merges the same way
        per sub-key.
        """
        if config_path is not None:
            return cls._load_one(config_path, layers=[config_path])
        layers = cls._discover_layers()
        if not layers:
            return cls()
        # A layer that exists but fails to parse contributes nothing rather
        # than crashing the merge (`_read_section` returns `None` for that
        # case) — it still occupies its slot in `config_layers`.
        sections = [(_read_section(p) or {}) for p in layers]
        kwargs = cls._merge_sections(sections)
        return cls(**kwargs, config_path=layers[0], config_layers=list(layers))

    @classmethod
    def _load_one(cls, path: Path, *, layers: list[Path]) -> "RuntimeConfig":
        section = _read_section(path)
        if section is None:
            # Missing file / unreadable / not valid TOML: bare defaults,
            # no config_path — matches pre-S1 behavior exactly.
            return cls()
        kwargs = cls._merge_sections([section])
        return cls(**kwargs, config_path=path, config_layers=list(layers))

    @classmethod
    def _merge_sections(cls, sections: list[dict]) -> dict:
        """Per-key merge across `sections` (ordered high-precedence first).

        Every value is looked up independently: the first section that both
        HAS the key and coerces it successfully wins; sections that lack the
        key, or whose value fails coercion, are skipped for that key (not
        for the whole layer — a different key from the same low layer can
        still win elsewhere).
        """
        defaults = cls()

        def scalar(key: str, kind: str, default):
            for section in sections:
                if key in section:
                    ok, value = _coerce(section[key], kind)
                    if ok:
                        return value
            return default

        log_dir = defaults.log_dir
        for section in sections:
            if "log_dir" in section:
                log_dir = Path(str(section["log_dir"])).expanduser()
                break

        return dict(
            log_dir=log_dir,
            capture_raw=scalar("capture_raw", "bool", defaults.capture_raw),
            enable_reinjection=scalar("enable_reinjection", "bool", defaults.enable_reinjection),
            reinjection_budget_tokens=scalar(
                "reinjection_budget_tokens", "int", defaults.reinjection_budget_tokens
            ),
            enable_auto_recall=scalar("enable_auto_recall", "bool", defaults.enable_auto_recall),
            auto_recall_k=scalar("auto_recall_k", "int", defaults.auto_recall_k),
            auto_recall_budget_tokens=scalar(
                "auto_recall_budget_tokens", "int", defaults.auto_recall_budget_tokens
            ),
            auto_recall_timeout_ms=scalar(
                "auto_recall_timeout_ms", "int", defaults.auto_recall_timeout_ms
            ),
            auto_recall_min_chars=scalar(
                "auto_recall_min_chars", "int", defaults.auto_recall_min_chars
            ),
            auto_recall_min_score=scalar(
                "auto_recall_min_score", "float", defaults.auto_recall_min_score
            ),
            auto_recall_daemon_budget_ms=scalar(
                "auto_recall_daemon_budget_ms", "int", defaults.auto_recall_daemon_budget_ms
            ),
            auto_recall_daemon_socket=scalar(
                "auto_recall_daemon_socket", "str", defaults.auto_recall_daemon_socket
            ),
            auto_recall_min_rerank=scalar(
                "auto_recall_min_rerank", "float_or_none", defaults.auto_recall_min_rerank
            ),
            auto_recall_dedup=scalar("auto_recall_dedup", "bool", defaults.auto_recall_dedup),
            budgets=cls._merge_budgets(sections),
        )

    @staticmethod
    def _merge_budgets(sections: list[dict]) -> dict[str, int]:
        """`budget` sub-table merge: per sub-key, first section (high to
        low) that sets AND successfully coerces that sub-key wins; keys
        nobody set keep the dataclass default (or are omitted, for a
        custom key no layer ever validly set)."""
        budgets = dict(_DEFAULT_BUDGETS)
        layer_budgets: list[dict] = []
        keys: set[str] = set(_DEFAULT_BUDGETS)
        for section in sections:
            b = section.get("budget")
            b = b if isinstance(b, dict) else {}
            layer_budgets.append(b)
            keys.update(str(k) for k in b)
        for key in keys:
            for b in layer_budgets:
                if key in b:
                    try:
                        budgets[key] = int(b[key])
                        break
                    except (TypeError, ValueError):
                        continue
        return budgets

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

    @staticmethod
    def _discover_layers() -> list[Path]:
        """The ordered (high to low) list of file layers `load()` merges.

        - `$RECALL_RUNTIME_CONFIG`: included if set AND the path exists
          (content is not inspected — an explicit override is trusted as-is,
          matching the pre-S1 behavior of this tier).
        - `./pyproject.toml`: included ONLY if it carries a non-empty
          `[tool.recall.runtime]` table. A project's own build pyproject.toml
          (no such table at all) must not shadow the user's global config —
          Codex 2026-05-05 MED, preserved from `_discover_config`.
        - `global_config_path()`: included if the file exists, regardless of
          whether its `[tool.recall.runtime]` table is present or empty —
          this file is install.sh's dedicated home for the section, so mere
          existence is enough to treat it as "the" config layer.
        """
        layers: list[Path] = []
        env = os.environ.get("RECALL_RUNTIME_CONFIG")
        if env:
            p = Path(env).expanduser()
            if p.exists():
                layers.append(p)
        cwd_pyproject = Path.cwd() / "pyproject.toml"
        if cwd_pyproject.exists() and _has_runtime_section(cwd_pyproject):
            layers.append(cwd_pyproject)
        global_pyproject = RuntimeConfig.global_config_path()
        if global_pyproject.exists():
            layers.append(global_pyproject)
        return layers


def _has_runtime_section(path: Path) -> bool:
    """True iff `path` is a TOML file with a non-empty `[tool.recall.runtime]` table."""
    section = _read_section(path)
    return bool(section)


def _read_section(path: Path) -> dict | None:
    """Parse `path` and return its `[tool.recall.runtime]` table.

    `{}` when the file parses but has no such table (still a valid,
    contributing-nothing layer). `None` when the file cannot be read or
    parsed at all — the caller treats that as "this layer doesn't exist."
    """
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = data.get("tool", {}).get("recall", {}).get("runtime", {})
    return section if isinstance(section, dict) else {}


def _coerce(value: object, kind: str) -> tuple[bool, object]:
    """Lenient per-value coercion. Returns `(ok, coerced)`; `ok=False` means
    the caller should fall through to the next layer / the dataclass
    default rather than crash the hook on a typo'd config value.

    `bool` requires an actual TOML boolean (`isinstance(value, bool)`) —
    `bool("maybe")` would otherwise silently succeed (any non-empty string
    is truthy) and defeat the whole point of falling through on a bad
    value.
    """
    if kind == "bool":
        if isinstance(value, bool):
            return True, value
        return False, None
    if kind == "int":
        try:
            return True, int(value)
        except (TypeError, ValueError):
            return False, None
    if kind == "float":
        try:
            return True, float(value)
        except (TypeError, ValueError):
            return False, None
    if kind == "float_or_none":
        # Lenient "gate off" sentinel: absent (caller never sees this value
        # — the key isn't in the section), `None`, or a case-insensitive
        # "none"/"null" string all mean the gate is off. Any other string
        # is tried as a numeric literal (`"-1.9547"` -> `-1.9547`) before
        # falling through to the next layer.
        if value is None:
            return True, None
        if isinstance(value, str) and value.strip().lower() in ("none", "null", ""):
            return True, None
        try:
            return True, float(value)
        except (TypeError, ValueError):
            return False, None
    if kind == "str":
        return True, str(value)
    raise ValueError(f"unknown coercion kind: {kind!r}")


__all__ = ["RuntimeConfig"]
