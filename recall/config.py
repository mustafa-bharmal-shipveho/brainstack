"""Config loading + BRAIN_HOME / XDG path resolution.

Tool-neutral defaults: config at $XDG_CONFIG_HOME/recall/config.json, brain at
$BRAIN_HOME (default $XDG_DATA_HOME/brain), cache at $XDG_CACHE_HOME/recall.
No reference to any specific AI tool.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

VALID_FRONTMATTER_MODES = {"auto-memory", "optional"}


@dataclass(frozen=True)
class SourceConfig:
    name: str
    path: str
    glob: str
    frontmatter: str
    exclude: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Source 'name' must be a non-empty string")
        # Names appear in cache directory paths (~/.cache/recall/<name>/...).
        # Reject anything that could escape the cache root or shadow other sources.
        if (
            ".." in self.name
            or "/" in self.name
            or "\\" in self.name
            or self.name.startswith(".")
            or "\x00" in self.name
        ):
            raise ValueError(
                f"invalid source name: {self.name!r}. Names cannot contain "
                "'..', path separators, or null bytes, or start with a dot."
            )
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError(
                "Source 'path' must be a non-empty string. "
                "An empty path silently resolves to the current working directory, "
                "which is almost never what you want."
            )
        if self.frontmatter not in VALID_FRONTMATTER_MODES:
            raise ValueError(
                f"Invalid frontmatter mode: {self.frontmatter!r}. "
                f"Must be one of {sorted(VALID_FRONTMATTER_MODES)}"
            )
        # Resolve path: expand ~ and env vars
        resolved = os.path.expanduser(os.path.expandvars(self.path))
        # Note: we keep relative paths relative; absolute paths are absolute.
        # Tests assert is_absolute() when given an absolute path.
        if not os.path.isabs(resolved):
            resolved = str(Path(resolved).resolve())
        # frozen dataclass — bypass via object.__setattr__
        object.__setattr__(self, "path", resolved)


@dataclass(frozen=True)
class RankingConfig:
    bm25_weight: float = 1.0
    embedding_weight: float = 1.0
    embedding_model: str = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class Config:
    sources: list[SourceConfig]
    ranking: RankingConfig = field(default_factory=RankingConfig)
    default_k: int = 5

    def __post_init__(self):
        names = [s.name for s in self.sources]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"Duplicate source names in config: {dupes}")


# ---------------------------------------------------------------------------
# Path resolution (XDG-respecting, falls back to ~/.config, ~/.cache, ~/.local/share)
# ---------------------------------------------------------------------------


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def _xdg(env_var: str, fallback_subdir: str) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return _expand(raw)
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / fallback_subdir


def xdg_config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def xdg_cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def xdg_data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def resolve_brain_home() -> Path:
    """Resolve where the brain lives.

    Precedence:
      1. $BRAIN_HOME if set — explicit override; works for standalone recall users
         who configured their own path.
      2. $BRAIN_ROOT/memory if $BRAIN_ROOT is set — the brainstack-integrated default.
         brainstack's install.sh writes brain content to $BRAIN_ROOT/memory/, so the
         retriever inherits the same env var the user already configured.
      3. $XDG_DATA_HOME/brain — XDG fallback for users who haven't run brainstack
         and haven't set BRAIN_HOME explicitly.
    """
    explicit = os.environ.get("BRAIN_HOME")
    if explicit:
        return _expand(explicit)
    brainstack_root = os.environ.get("BRAIN_ROOT")
    if brainstack_root:
        return _expand(brainstack_root) / "memory"
    return xdg_data_home() / "brain"


def config_path() -> Path:
    return xdg_config_home() / "recall" / "config.json"


def cache_dir() -> Path:
    return xdg_cache_home() / "recall"


# ---------------------------------------------------------------------------
# Default config + load/save
# ---------------------------------------------------------------------------


def default_config() -> Config:
    brain = resolve_brain_home()
    return Config(
        sources=[
            SourceConfig(
                name="brain",
                path=str(brain),
                glob="**/*.md",
                frontmatter="auto-memory",
                exclude=[
                    "episodic/**",
                    "candidates/**",
                    "working/**",
                    "scripts/**",
                    "__pycache__/**",
                ],
            )
        ],
        ranking=RankingConfig(),
        default_k=5,
    )


def _config_to_dict(cfg: Config) -> dict:
    return {
        "sources": [
            {
                "name": s.name,
                "path": s.path,
                "glob": s.glob,
                "frontmatter": s.frontmatter,
                "exclude": list(s.exclude),
            }
            for s in cfg.sources
        ],
        "ranking": asdict(cfg.ranking),
        "default_k": cfg.default_k,
    }


def _config_from_dict(data: dict) -> Config:
    if "sources" not in data:
        raise ValueError("Config missing required field: 'sources'")
    if not isinstance(data["sources"], list):
        raise ValueError("Config 'sources' must be a list")
    sources: list[SourceConfig] = []
    for raw in data["sources"]:
        if "name" not in raw:
            raise ValueError(f"Source missing 'name': {raw}")
        if "path" not in raw:
            raise ValueError(f"Source {raw.get('name')} missing 'path'")
        sources.append(
            SourceConfig(
                name=raw["name"],
                path=raw["path"],
                glob=raw.get("glob", "**/*.md"),
                frontmatter=raw.get("frontmatter", "optional"),
                exclude=list(raw.get("exclude", [])),
            )
        )
    ranking_raw = data.get("ranking", {})
    ranking = RankingConfig(
        bm25_weight=float(ranking_raw.get("bm25_weight", 1.0)),
        embedding_weight=float(ranking_raw.get("embedding_weight", 1.0)),
        embedding_model=ranking_raw.get("embedding_model", "all-MiniLM-L6-v2"),
    )
    return Config(
        sources=sources,
        ranking=ranking,
        default_k=int(data.get("default_k", 5)),
    )


def load_config() -> Config:
    """Load config from disk, creating a default file if missing."""
    path = config_path()
    if not path.exists():
        cfg = default_config()
        save_config(cfg)
        return cfg
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _config_from_dict(raw)


def save_config(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_config_to_dict(cfg), indent=2), encoding="utf-8")
    os.replace(tmp, path)
