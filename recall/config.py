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
    """Single source entry in the user config.

    `path` may contain unresolved env-var placeholders (e.g. `$BRAIN_ROOT/memory`)
    or `~`. The `__post_init__` validates and resolves into `_resolved_path` (the
    absolute filesystem path to actually read from), but preserves the original
    `path` string verbatim so `_config_to_dict` can serialize the env-var form
    back to disk. That way changing `$BRAIN_ROOT` between runs is reflected
    automatically — the saved config doesn't bake in a stale resolved value.

    Use `source.resolved_path` whenever you need an actual filesystem path.
    Tests that assert `.path` is absolute should use `.resolved_path` instead.
    """

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
        # Light-touch validation only — strict resolution happens at
        # the `.resolved_path` property so that:
        #   - load_config / config migration can introspect a source
        #     whose `path` references a future or operator-defined env
        #     var without the env being set at config-load time;
        #   - actual *use* of `resolved_path` (by discover_documents,
        #     reindex, query) still fails loudly on unresolvable vars,
        #     preventing the silent "Indexed 0 documents" mode.
        # Stored only for back-compat with any caller that still reads
        # `_resolved_path` directly.
        try:
            resolved = resolve_source_path(self.path)
        except ValueError:
            # Unresolved $VAR — defer to .resolved_path use-time check.
            resolved = self.path
        object.__setattr__(self, "_resolved_path", resolved)

    @property
    def resolved_path(self) -> str:
        """Filesystem path to read from. `path` may contain env-var literals;
        this is always an absolute resolved string.

        Re-resolves on access so a long-running process picks up env-var changes
        between calls (rare, but free correctness).
        """
        return resolve_source_path(self.path)


@dataclass(frozen=True)
class RankingConfig:
    """Hybrid retrieval config.

    `mode` selects which retrieval legs are active:
      - "hybrid": dense + sparse, fused via Qdrant RRF (default; best quality)
      - "dense":  embedding-only (use when sparse adds noise on a corpus)
      - "sparse": BM25-only (use when offline / before embedding model is downloaded)

    `embedder` and `sparse_embedder` are FastEmbed model names. The defaults are
    BAAI/bge-base-en-v1.5 (~440 MB, top English semantic) and Qdrant/bm25
    (sparse, no neural model, instant).

    `reranker` selects an optional third stage that reorders the top-N
    candidates by a direct (query, document) cross-encoder score.
      - "none":          no rerank — fastest path (default)
      - "cross_encoder": local FastEmbed cross-encoder (opt-in via --rerank
                         flag or config). On real-brain testing the rerank
                         was a wash — it helps when the query has clear
                         semantic intent and hurts when the bi-encoder
                         already had a good top-3. Default off; flip on
                         per-query with `--rerank cross_encoder` or per-user
                         by setting `"reranker": "cross_encoder"` here.
    """

    mode: str = "hybrid"
    embedder: str = "BAAI/bge-base-en-v1.5"
    sparse_embedder: str = "Qdrant/bm25"
    reranker: str = "none"
    reranker_model: str = "jinaai/jina-reranker-v1-turbo-en"
    rerank_n: int = 20

    # How to treat memories flagged `needs_review: true` (e.g. by `recall lint
    # --mark` when they reference dead paths). A flagged memory is potentially
    # stale, so injecting it costs trust.
    #   - "demote":  keep, but multiply its score by `needs_review_penalty` so
    #                fresh memories outrank it (default — never silently lose info)
    #   - "exclude": drop flagged memories from results entirely
    #   - "ignore":  no special handling (pre-lint behavior)
    needs_review_policy: str = "demote"
    # Score multiplier applied to flagged memories under the "demote" policy.
    # 0.0 = effectively exclude, 1.0 = no penalty. 0.5 halves the score.
    needs_review_penalty: float = 0.5

    # Whether `recall query` runs LLM query expansion when neither --expand
    # nor --no-expand is passed. Expansion costs one LLM CLI round-trip per
    # query (measured ~5-20 s with a cold claude/codex CLI), so it is
    # opt-in: flip this to true, or pass --expand per query.
    expand_default: bool = False


@dataclass(frozen=True)
class AutoRecallConfig:
    """Scoping for the every-prompt auto-recall injection (Claude Code hook).

    `exclude_sources` names sources that must NOT feed auto-recall. They stay
    fully available to explicit `recall query` and the MCP surface; they just
    never get injected into a session automatically. This is the lever for a
    sensitive mirrored source (e.g. an `--add-source` folder of employer or
    incident notes) that you want searchable on demand but not surfaced into
    unrelated repositories on every prompt.

    Default empty = exclude nothing (auto-recall queries every source), which
    preserves pre-0.6 behavior. Per-memory / per-project (cwd) scoping is a
    separate, finer-grained mechanism tracked on the roadmap; this source-level
    exclusion is the coarse but immediately useful first cut.
    """

    exclude_sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    sources: list[SourceConfig]
    ranking: RankingConfig = field(default_factory=RankingConfig)
    auto_recall: AutoRecallConfig = field(default_factory=AutoRecallConfig)
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


# Re for finding "unresolved" `$VAR` placeholders after substitution.
# Matches POSIX-shell-style `$NAME` and `${NAME}` forms.
_UNRESOLVED_VAR_RE = __import__("re").compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")


def resolve_source_path(raw: str) -> str:
    """Resolve a config `path` string to an absolute filesystem path.

    Framework-shaped resolution (no hardcoded "if BRAIN_ROOT not set, use
    ~/.agent" branching at this layer — that precedence lives in
    `resolve_brain_home()`, which is the single canonical registry of
    "where the brain lives"):

      1. Substitute `$BRAIN_HOME` / `${BRAIN_HOME}` with the value of
         `resolve_brain_home()`. This ALWAYS produces a real path —
         even when `BRAIN_HOME` and `BRAIN_ROOT` are both unset (it
         falls through to the XDG default). So every consumer of
         SourceConfig sees a usable path regardless of shell env.
      2. Substitute `$BRAIN_ROOT` / `${BRAIN_ROOT}` with the value the
         env var would otherwise expand to. When unset, fall back to
         `resolve_brain_home().parent` — i.e. the parent of the brain,
         which is the same value `$BRAIN_ROOT` would have if the user
         followed brainstack's install.sh convention.
      3. Run `os.path.expanduser` + `os.path.expandvars` for any
         remaining `$VARS` (preserves prior behavior for non-brain envs).
      4. **If any `$VAR` survives substitution, raise ValueError with a
         clear, user-actionable message.** This is the safety net: prior
         code silently treated `$UNKNOWN/memory` as a literal directory
         name and indexed 0 documents with no error indicating why.

    Adding a new "where is the brain" env var is a one-line change in
    `resolve_brain_home()` — all consumers (SourceConfig included)
    pick it up automatically.
    """
    # Step 1: $BRAIN_HOME → resolve_brain_home()
    brain_home_value = str(resolve_brain_home())
    raw = raw.replace("${BRAIN_HOME}", brain_home_value)
    raw = raw.replace("$BRAIN_HOME", brain_home_value)

    # Step 2: $BRAIN_ROOT → env value if set, else resolve_brain_home().parent
    brain_root_value = os.environ.get("BRAIN_ROOT")
    if brain_root_value is None:
        # Canonical fallback: parent of the resolved brain home. With the
        # default config (`$BRAIN_ROOT/memory`) this gives `~/.agent`.
        brain_root_value = str(resolve_brain_home().parent)
    raw = raw.replace("${BRAIN_ROOT}", brain_root_value)
    raw = raw.replace("$BRAIN_ROOT", brain_root_value)

    # Step 3: normal env + ~ expansion for anything else.
    resolved = os.path.expanduser(os.path.expandvars(raw))

    # Step 4: loud failure if anything still unresolved. Without this
    # check, an unresolved `$FOO` becomes a literal path segment and
    # `discover_documents` silently finds zero files.
    leftover = _UNRESOLVED_VAR_RE.search(resolved)
    if leftover is not None:
        raise ValueError(
            f"recall: unresolved environment variable {leftover.group(0)!r} "
            f"in source path {raw!r}. Set the variable in your shell, or "
            f"replace the placeholder with an absolute path in your "
            f"recall config."
        )

    # Make absolute (so a relative path doesn't silently anchor to CWD
    # later).
    if not os.path.isabs(resolved):
        resolved = str(Path(resolved).resolve())
    return resolved


def _xdg(env_var: str, fallback_subdir: str) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return _expand(raw)
    home = os.environ.get("HOME")
    if not home:
        home = str(Path.home())
    if not home:
        raise EnvironmentError(
            f"$HOME not set and Path.home() failed; cannot resolve {env_var} fallback {fallback_subdir}"
        )
    return Path(home) / fallback_subdir


def xdg_config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def xdg_cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def xdg_data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def resolve_brain_home() -> Path:
    """Resolve where the brain lives. Single source of truth for every
    consumer (SourceConfig resolution, doctor, install.sh, dream cron).

    Precedence:
      1. $BRAIN_HOME if set — explicit override; works for standalone recall users
         who configured their own path.
      2. $BRAIN_ROOT/memory if $BRAIN_ROOT is set — the brainstack-integrated default.
         brainstack's install.sh writes brain content to $BRAIN_ROOT/memory/, so the
         retriever inherits the same env var the user already configured.
      3. ~/.agent/memory if it exists — the brainstack convention. install.sh
         writes here; the dream cron's launchd plist sets $BRAIN_ROOT to ~/.agent.
         This fallback lets `recall query` / `recall reindex` work from any
         fresh shell (no need to `export BRAIN_ROOT` first) when a brainstack
         install is already on disk. Without this step, a fresh shell hit
         "Indexed 0 documents" silently because $BRAIN_ROOT was unset.
      4. $XDG_DATA_HOME/brain — XDG fallback for users who haven't run brainstack
         and haven't set BRAIN_HOME explicitly.
    """
    explicit = os.environ.get("BRAIN_HOME")
    if explicit:
        return _expand(explicit)
    brainstack_root = os.environ.get("BRAIN_ROOT")
    if brainstack_root:
        return _expand(brainstack_root) / "memory"
    brainstack_default = Path(os.path.expanduser("~/.agent/memory"))
    if brainstack_default.is_dir():
        return brainstack_default
    return xdg_data_home() / "brain"


def config_path() -> Path:
    return xdg_config_home() / "recall" / "config.json"


def cache_dir() -> Path:
    return xdg_cache_home() / "recall"


# ---------------------------------------------------------------------------
# Default config + load/save
# ---------------------------------------------------------------------------


def _default_path_literal(suffix: str | None = None) -> str:
    """Path string written into auto-generated configs.

    `suffix=None` returns the path of the brain itself; a non-None suffix
    returns a sibling tier (e.g. ``"imports"`` → the `imports/` mirror).

    We prefer env-var literals (`$BRAIN_ROOT/...`, `$BRAIN_HOME`) over the
    *resolved* value so the saved config picks up env-var changes on the
    next run. The literal is expanded at config load time via
    `os.path.expandvars` in `SourceConfig.__post_init__`.

    `$BRAIN_HOME` is the brain directory itself, so a sibling tier has no
    clean env-var literal in that mode — fall back to a resolved sibling
    path. With `$BRAIN_ROOT` (which is the *parent* of the brain), a sibling
    is just `$BRAIN_ROOT/<suffix>`.
    """
    if os.environ.get("BRAIN_HOME"):
        return "$BRAIN_HOME" if suffix is None else str(resolve_brain_home().parent / suffix)
    if os.environ.get("BRAIN_ROOT"):
        return "$BRAIN_ROOT/memory" if suffix is None else f"$BRAIN_ROOT/{suffix}"
    base = resolve_brain_home()
    return str(base) if suffix is None else str(base.parent / suffix)


def _default_brain_path_literal() -> str:
    """Path literal for the brain source (memory tree)."""
    return _default_path_literal(None)


def _default_imports_path_literal() -> str:
    """Path literal for the imports tier (mirror of external folders)."""
    return _default_path_literal("imports")


# Migration marker — stamped on configs whose shape has been advanced to the
# v2 layout (brain + imports as default sources). Once stamped, load_config
# treats the config as final and never re-evaluates legacy migration paths.
# Bump this string if a v3 schema change ever needs a similar one-shot patch.
_MIGRATION_MARKER_KEY = "migration_marker"
_MIGRATION_MARKER_V2 = "v2-imports-source"


def _imports_source_default() -> SourceConfig:
    """The canonical `imports` source. Defined as a function (not a constant)
    so the path literal is recomputed against the current env on every call —
    matters when tests monkeypatch BRAIN_ROOT / BRAIN_HOME between cases."""
    return SourceConfig(
        name="imports",
        path=_default_imports_path_literal(),
        glob="**/*.md",
        frontmatter="auto-memory",
        exclude=[
            # Mirror tier carries non-markdown blobs (Claude session JSONLs,
            # Codex session JSONs, the misc-adapter sidecar). Retrieval should
            # only see actual prose; cache dirs are noise.
            "__pycache__/**",
            "*.json",
            "*.jsonl",
            "*.txt",
            ".imported_misc.jsonl",
        ],
    )


def default_config() -> Config:
    return Config(
        sources=[
            SourceConfig(
                name="brain",
                path=_default_brain_path_literal(),
                glob="**/*.md",
                frontmatter="auto-memory",
                exclude=[
                    # Write-side staging dirs — not graduated content
                    "episodic/**",
                    "candidates/**",
                    "working/**",
                    "scripts/**",
                    "__pycache__/**",
                    # Aggregate / index files: these are concatenations of other
                    # files' content, so they always score high on lexical AND
                    # semantic similarity, drowning out the actual source lessons.
                    "MEMORY.md",
                    "semantic/LESSONS.md",
                ],
            ),
            _imports_source_default(),
        ],
        ranking=RankingConfig(),
        default_k=5,
    )


def _accepted_legacy_brain_paths() -> set[str]:
    """All path strings the brain source could plausibly have been written
    with by `_default_path_literal()`. The migration helper accepts any of
    these as 'still a default brain source.'

    The resolved literal (`str(resolve_brain_home())`) is only accepted when
    NEITHER `$BRAIN_ROOT` nor `$BRAIN_HOME` is set — that's the only env in
    which `_default_path_literal()` would ever have produced a resolved
    literal at write time. If a user has explicitly set `$BRAIN_HOME` to a
    custom path AND wrote that same path as a literal in their config, we
    must NOT treat that as a default — it's an intentional customization.
    Codex 2026-05-05 P2.
    """
    accepted = {"$BRAIN_ROOT/memory", "$BRAIN_HOME"}
    if not os.environ.get("BRAIN_HOME") and not os.environ.get("BRAIN_ROOT"):
        accepted.add(str(resolve_brain_home()))
    return accepted


def _imports_path_from_brain_path(brain_path: str) -> str:
    """Derive the imports-tier path literal that mirrors `brain_path`'s style.

    Migration calls this so the appended `imports` source uses the SAME
    path-literal convention as the user's existing `brain` source. Without
    it, migration would read the current shell env (via
    `_default_imports_path_literal`), which may not match the env at
    original config write time — yielding a literal/resolved mismatch.
    """
    if brain_path == "$BRAIN_ROOT/memory":
        return "$BRAIN_ROOT/imports"
    if brain_path == "$BRAIN_HOME":
        # $BRAIN_HOME IS the brain directory; its sibling has no env-var literal.
        return str(resolve_brain_home().parent / "imports")
    return str(Path(brain_path).parent / "imports")


def _maybe_migrate_add_imports_source(data: dict) -> tuple[dict, bool]:
    """One-shot migration: append the `imports` source to legacy single-source
    configs that still match the original `brain`-only defaults.

    **Mutates `data` in-place** when migrating; returns the same dict object
    plus a `did_mutate` flag. Callers that need an unaffected copy must
    deep-copy before passing in. Today's only caller is `load_config`, which
    has just-parsed JSON it never reuses, so in-place is correct + cheap.

    The caller persists the dict to disk when `did_mutate is True`.
    Idempotency is enforced via `migration_marker`: once stamped, this
    function short-circuits.

    Conservative — preserves the user's intent in every ambiguous case:

    * Marker already present → no-op (one-shot guard).
    * Source list is anything other than exactly one source → no-op
      (user has clearly customized; we don't know what they want).
    * That single source isn't named `brain`, or its path doesn't match the
      original default literal → no-op (custom path means custom intent).
    * A source already named `imports` exists → no-op (defense in depth).

    Mirrors the in-place ranking-schema migration pattern in
    `_config_from_dict` (legacy `bm25_weight`/`embedding_weight` remap).
    Stays at the dict level so `SourceConfig.__post_init__` runs only once
    per load.
    """
    if data.get(_MIGRATION_MARKER_KEY) == _MIGRATION_MARKER_V2:
        return data, False
    sources = data.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        return data, False
    only = sources[0]
    if not isinstance(only, dict):
        return data, False
    if only.get("name") != "brain":
        return data, False
    legacy_path = only.get("path")
    if not isinstance(legacy_path, str):
        return data, False
    # Path-equality check accepts any value `_default_path_literal()` could
    # have emitted across all envs the original config might have been
    # written under. A resolved-XDG path without BRAIN_ROOT/HOME at write
    # time matches only against the current resolved value (no way to
    # reconstruct the prior $XDG_DATA_HOME).
    if legacy_path not in _accepted_legacy_brain_paths():
        return data, False
    if any(isinstance(s, dict) and s.get("name") == "imports" for s in sources):
        # User somehow has imports without the marker — respect their config
        # but stamp the marker so we don't re-evaluate every load.
        data[_MIGRATION_MARKER_KEY] = _MIGRATION_MARKER_V2
        return data, True
    # Path literal mirrors the existing brain source's style, not the
    # current env — otherwise a shell where BRAIN_ROOT is unset would bake
    # a resolved path into imports while brain kept its $BRAIN_ROOT/memory
    # literal, yielding a portable/non-portable mix on disk.
    imports_path = _imports_path_from_brain_path(legacy_path)
    imports_template = _imports_source_default()
    data["sources"].append(
        {
            "name": imports_template.name,
            "path": imports_path,
            "glob": imports_template.glob,
            "frontmatter": imports_template.frontmatter,
            "exclude": list(imports_template.exclude),
        }
    )
    data[_MIGRATION_MARKER_KEY] = _MIGRATION_MARKER_V2
    return data, True


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
        "auto_recall": asdict(cfg.auto_recall),
        "default_k": cfg.default_k,
        # Always stamp the marker on save: fresh defaults + every migrated
        # config write through here, so a downgrade-and-re-upgrade cycle
        # won't accidentally re-trigger migration on already-current shape.
        _MIGRATION_MARKER_KEY: _MIGRATION_MARKER_V2,
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
    ranking_raw = data.get("ranking") or {}
    # Lenient migration of pre-Qdrant config shape
    # ({bm25_weight, embedding_weight, embedding_model}) → new shape
    # ({mode, embedder, sparse_embedder}). Old configs keep working without
    # the user editing the file by hand.
    legacy_keys = {"bm25_weight", "embedding_weight", "embedding_model"}
    if legacy_keys & set(ranking_raw):
        bw = float(ranking_raw.get("bm25_weight", 1.0))
        ew = float(ranking_raw.get("embedding_weight", 1.0))
        if bw > 0 and ew > 0:
            mode = "hybrid"
        elif ew > 0:
            mode = "dense"
        elif bw > 0:
            mode = "sparse"
        else:
            mode = "hybrid"
        ranking = RankingConfig(
            mode=mode,
            embedder=str(ranking_raw.get("embedder", "BAAI/bge-base-en-v1.5")),
            sparse_embedder=str(ranking_raw.get("sparse_embedder", "Qdrant/bm25")),
            reranker=str(ranking_raw.get("reranker", "cross_encoder")),
            reranker_model=str(
                ranking_raw.get("reranker_model", "jinaai/jina-reranker-v1-turbo-en")
            ),
            rerank_n=int(ranking_raw.get("rerank_n", 20)),
            needs_review_policy=str(ranking_raw.get("needs_review_policy", "demote")),
            needs_review_penalty=float(ranking_raw.get("needs_review_penalty", 0.5)),
            expand_default=bool(ranking_raw.get("expand_default", False)),
        )
    else:
        ranking = RankingConfig(
            mode=str(ranking_raw.get("mode", "hybrid")),
            embedder=str(ranking_raw.get("embedder", "BAAI/bge-base-en-v1.5")),
            sparse_embedder=str(ranking_raw.get("sparse_embedder", "Qdrant/bm25")),
            reranker=str(ranking_raw.get("reranker", "cross_encoder")),
            reranker_model=str(
                ranking_raw.get("reranker_model", "jinaai/jina-reranker-v1-turbo-en")
            ),
            rerank_n=int(ranking_raw.get("rerank_n", 20)),
            needs_review_policy=str(ranking_raw.get("needs_review_policy", "demote")),
            needs_review_penalty=float(ranking_raw.get("needs_review_penalty", 0.5)),
            expand_default=bool(ranking_raw.get("expand_default", False)),
        )
    auto_recall_raw = data.get("auto_recall") or {}
    auto_recall = AutoRecallConfig(
        exclude_sources=list(auto_recall_raw.get("exclude_sources", [])),
    )
    return Config(
        sources=sources,
        ranking=ranking,
        auto_recall=auto_recall,
        default_k=int(data.get("default_k", 5)),
    )


def effective_mode(cfg: "Config", override: str | None = None) -> str:
    """Resolve the retrieval mode that every surface must agree on.

    Precedence: explicit override (a CLI flag) > `RECALL_MODE` env > the
    config's `ranking.mode`. Centralized so the CLI, MCP server, auto-recall
    hook, and the index-build path all pick the SAME mode. That matters
    because a `sparse` mode must skip the dense embedder on BOTH query and
    indexing; if indexing silently used hybrid it would download the dense
    model the user set sparse to avoid.
    """
    return override or os.environ.get("RECALL_MODE") or cfg.ranking.mode


def load_config() -> Config:
    """Load config from disk, creating a default file if missing.

    On load, we run a one-shot dict-level migration that adds the `imports`
    source to legacy single-source configs (see
    `_maybe_migrate_add_imports_source`). When the migration mutates the
    raw dict, we persist the new shape immediately so the next load is a
    pure read. Marker-based; runs at most once per config.
    """
    path = config_path()
    if not path.exists():
        cfg = default_config()
        save_config(cfg)
        return cfg
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw, did_migrate = _maybe_migrate_add_imports_source(raw)
    cfg = _config_from_dict(raw)
    if did_migrate:
        # Write-on-read side effect: same precedent as the missing-file
        # branch above. Persists the marker + appended `imports` source.
        save_config(cfg)
    return cfg


def save_config(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_config_to_dict(cfg), indent=2), encoding="utf-8")
    os.replace(tmp, path)
