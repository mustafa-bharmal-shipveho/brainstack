#!/usr/bin/env python3
"""Discovery + adapter routing for native-memory migration.

`./install.sh --migrate` (no source path) drops into the interactive flow
defined here:

    1. discover_candidates() walks known AI-tool memory locations under
       $HOME and returns a list of Candidate(path, format, file_count, size).
    2. The user picks a candidate (or `none`).
    3. dispatch() routes to the right Adapter for the candidate's format.
    4. Adapters know how to convert one tool's memory into the brain's
       layer structure. PR-A registers ClaudeCodeAdapter only; PR-B and
       PR-C will register CursorPlansAdapter and CodexCliAdapter.

CLI entry points (used by install.sh):

    python3 migrate_dispatcher.py discover
        Print discovered candidates as JSON to stdout (consumers gate
        on `schema_version`).

    python3 migrate_dispatcher.py plan SRC [DST]
        Dry-run a specific source — prints the plan, writes nothing.

    python3 migrate_dispatcher.py execute SRC DST
        Real migration. Emits a JSON envelope of the MigrationResult
        on stdout when complete.

    python3 migrate_dispatcher.py interactive
        Discover + prompt + plan + confirm + execute. Used when a user
        runs `./install.sh --migrate` with no source path.

Adapter authoring (PR-B / PR-C):

    from agent.tools.migrate_dispatcher import (
        Adapter, MigrationResult, register_adapter,
    )

    class CursorPlansAdapter:
        name = "cursor-plans"
        supported_formats = frozenset({"cursor-plans"})

        def supports(self, fmt): return fmt in self.supported_formats
        def migrate(self, src, dst, dry_run, options=None):
            ...
            return MigrationResult(format="cursor-plans", ...)

    register_adapter(CursorPlansAdapter())
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import ClassVar, Iterable, Optional, Protocol, runtime_checkable

__all__ = (
    # Public types
    "Adapter",
    "Candidate",
    "MigrationResult",
    "NoAdapterError",
    "AdapterRegistrationError",
    # Public functions
    "detect_format",
    "discover_candidates",
    "dispatch",
    "register_adapter",
    "unregister_adapter",
    "registered_adapters",
    "get_adapter_for",
)

# Path-relative imports so the dispatcher works whether called from tests
# (cwd = repo root) or via install.sh (cwd = $BRAIN_ROOT).
_HERE = Path(__file__).resolve().parent
_BASE = _HERE.parent
sys.path.insert(0, str(_HERE))                # for migrate.py
sys.path.insert(0, str(_BASE / "memory"))     # for _atomic.py


class NoAdapterError(RuntimeError):
    """Raised when dispatch encounters a format with no registered adapter."""


class AdapterRegistrationError(RuntimeError):
    """Raised when an adapter doesn't satisfy the Adapter contract or
    when a duplicate adapter for the same format is registered."""


@dataclass
class Candidate:
    path: Path
    format: str
    file_count: int
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "format": self.format,
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
        }


@dataclass
class MigrationResult:
    """Result of one migrate operation. JSON-serializable via `to_dict()`.

    `tool_specific` is the per-adapter escape hatch — keep top-level fields
    tool-agnostic; let each adapter add its own counters under
    `tool_specific` (e.g. `episodes_imported`, `sessions_count`).

    `schema_version` lets future fields land additively while letting
    consumers gate on a known shape.
    """
    format: str
    files_written: int
    files_planned: int
    backup_path: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    namespace: str = "default"
    source_path: Optional[Path] = None
    tool_specific: dict = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "format": self.format,
            "namespace": self.namespace,
            "files_written": self.files_written,
            "files_planned": self.files_planned,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "source_path": str(self.source_path) if self.source_path else None,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
            "tool_specific": dict(self.tool_specific),
        }


@runtime_checkable
class Adapter(Protocol):
    """Protocol every adapter implements. PR-B (Cursor) and PR-C (Codex)
    will register additional adapters via `register_adapter`.

    Adapters are responsible for converting one tool's native memory
    layout into the brainstack 4-layer structure under `<dst>/memory/`.
    `migrate(dry_run=True)` MUST NOT write anything; `migrate(dry_run=False)`
    performs the full conversion.
    """
    name: str
    supported_formats: ClassVar[frozenset[str]]

    def supports(self, fmt: str) -> bool: ...

    def migrate(
        self,
        src: Path,
        dst: Path,
        dry_run: bool,
        options: Optional[dict] = None,
    ) -> MigrationResult: ...


# ---- Format detection ------------------------------------------------


def detect_format(src: Path) -> str:
    """Tag a source dir by what produced it.

    Returns one of:
      - already-symlinked: source is itself a symlink (assume it's
        already pointing at a brain)
      - claude-code-flat: prefix-named files at root (feedback_*, user_*, etc.)
      - claude-code-nested: has personal/{profile,notes,references}/ or
        semantic/lessons/
      - claude-code-mixed: both flat and nested signals
      - cursor-plans: *.plan.md files at root (Cursor's plans dir)
      - cursor-rules: a single .cursorrules file (per-project rules)
      - codex-cli: sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl OR history.jsonl
      - unknown: has .md files but no recognized pattern
      - empty: no .md or .jsonl files
    """
    if src.is_symlink():
        return "already-symlinked"
    if not src.is_dir():
        # A bare file like .cursorrules
        if src.is_file() and src.name == ".cursorrules":
            return "cursor-rules"
        return "unknown"

    # ---- Codex CLI ----
    sessions_dir = src / "sessions"
    if sessions_dir.is_dir():
        # Look for rollout-*.jsonl anywhere under sessions/
        for _ in sessions_dir.rglob("rollout-*.jsonl"):
            return "codex-cli"
    if (src / "history.jsonl").is_file() and (src / "config.toml").is_file():
        # The combo of history.jsonl + config.toml is Codex CLI's signature
        return "codex-cli"

    # ---- Cursor plans ----
    # Claude Code memory dirs always have `MEMORY.md` at root (the auto-memory
    # index). If that's present, this is Claude even if a stray .plan.md
    # exists. Don't claim cursor-plans in that case (codex review P2).
    plan_files = list(src.glob("*.plan.md"))
    if plan_files and not (src / "MEMORY.md").is_file():
        # Make sure we're not also in a claude-shaped dir.
        has_claude_flat = any(src.glob("feedback_*.md")) or any(src.glob("user_*.md"))
        has_claude_nested = (
            (src / "personal").is_dir() or (src / "semantic").is_dir()
        )
        if not has_claude_flat and not has_claude_nested:
            return "cursor-plans"

    # ---- Claude Code ----
    # Strong signals for "this is a Claude Code auto-memory dir":
    #  - MEMORY.md at root (the auto-memory loop's index file). Real Claude
    #    Code memory dirs found on the user's machine often have bare-named
    #    .md files (e.g. `slack_voice.md`, `team_ownership.md`) WITHOUT the
    #    `feedback_`/`user_`/etc. prefixes — but they always have MEMORY.md.
    #  - Prefix-named files at root (`feedback_*`, `user_*`, …)
    has_memory_index = (src / "MEMORY.md").is_file()
    has_prefix_flat = (
        any(src.glob("feedback_*.md"))
        or any(src.glob("user_*.md"))
        or any(src.glob("project_*.md"))
        or any(src.glob("cycle-*.md"))
        or any(src.glob("cycle_*.md"))
        or any(src.glob("reference_*.md"))
    )
    # Bare-named flat: at least one root-level .md file alongside MEMORY.md.
    has_bare_flat = has_memory_index and any(
        p.suffix == ".md" and p.name != "MEMORY.md"
        for p in src.iterdir()
        if p.is_file()
    )
    has_flat = has_prefix_flat or has_bare_flat

    has_nested = any(
        (src / sub).is_dir()
        for sub in (
            Path("personal") / "profile",
            Path("personal") / "notes",
            Path("personal") / "references",
            Path("semantic") / "lessons",
        )
    )
    if has_flat and has_nested:
        return "claude-code-mixed"
    if has_nested:
        return "claude-code-nested"
    if has_flat:
        return "claude-code-flat"

    # Has md files but doesn't match anything we recognize
    if any(src.rglob("*.md")):
        return "unknown"

    return "empty"


# ---- Discovery -------------------------------------------------------


def _safe_size(paths: Iterable[Path]) -> int:
    total = 0
    for p in paths:
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def discover_candidates(env: Optional[dict] = None) -> list[Candidate]:
    """Walk known AI-tool memory locations under HOME and return Candidates.

    The discovery contract is "best-effort observation, never error". A
    missing path, a permission denial, or a deleted dir mid-scan should
    yield an empty candidate list rather than raising — the user's
    interactive flow shouldn't break because one tool happens not to be
    installed.
    """
    if env is None:
        env = os.environ
    home_str = env.get("HOME") or os.path.expanduser("~")
    home = Path(home_str)
    cands: list[Candidate] = []

    # ---- Claude Code project memories ----
    claude_projects = home / ".claude" / "projects"
    if claude_projects.is_dir():
        try:
            entries = sorted(claude_projects.iterdir())
        except OSError:
            entries = []
        for proj in entries:
            mem = proj / "memory"
            # is_symlink() works whether the target exists or not
            if not (mem.is_symlink() or mem.is_dir()):
                continue
            try:
                fmt = detect_format(mem)
            except OSError:
                continue
            if fmt == "empty":
                continue
            file_count = 0
            size = 0
            if not mem.is_symlink():
                try:
                    md_files = list(mem.rglob("*.md"))
                    file_count = len(md_files)
                    size = _safe_size(md_files)
                except OSError:
                    pass
            cands.append(Candidate(
                path=mem.resolve() if mem.is_symlink() else mem,
                format=fmt,
                file_count=file_count,
                size_bytes=size,
            ))

    # ---- Cursor plans ----
    cursor_plans = home / ".cursor" / "plans"
    if cursor_plans.is_dir():
        try:
            plans = list(cursor_plans.glob("*.plan.md"))
        except OSError:
            plans = []
        if plans:
            cands.append(Candidate(
                path=cursor_plans,
                format="cursor-plans",
                file_count=len(plans),
                size_bytes=_safe_size(plans),
            ))

    # ---- Codex CLI ----
    codex = home / ".codex"
    if codex.is_dir():
        sessions = codex / "sessions"
        try:
            rollouts = list(sessions.rglob("rollout-*.jsonl")) if sessions.is_dir() else []
        except OSError:
            rollouts = []
        if rollouts or (codex / "history.jsonl").is_file():
            cands.append(Candidate(
                path=codex,
                format="codex-cli",
                file_count=len(rollouts),
                size_bytes=_safe_size(rollouts),
            ))

    return cands


# ---- Adapters --------------------------------------------------------


class ClaudeCodeAdapter:
    """Wraps `migrate.py`'s recursive walk for claude-code-* formats."""

    name = "claude-code"
    supported_formats: ClassVar[frozenset[str]] = frozenset({
        "claude-code-flat",
        "claude-code-nested",
        "claude-code-mixed",
    })

    def supports(self, fmt: str) -> bool:
        return fmt in self.supported_formats

    def migrate(
        self,
        src: Path,
        dst: Path,
        dry_run: bool,
        options: Optional[dict] = None,
    ) -> MigrationResult:
        options = options or {}
        namespace = options.get("namespace", "default")
        fmt = detect_format(src)
        if dry_run:
            return self._plan(src, dst, fmt, namespace)
        return self._execute(src, dst, fmt, namespace)

    def _plan(
        self, src: Path, dst: Path, fmt: str, namespace: str
    ) -> MigrationResult:
        # Use migrate.route_file to count what would be written. Mirrors the
        # symlink-skip + path-resolution defense that migrate.py main() has,
        # so a malicious symlink in the source can't make the dispatcher
        # plan a migration of files outside the source tree.
        from migrate import route_file
        files_planned = 0
        warnings: list[str] = []
        try:
            src_resolved = src.resolve()
        except OSError as e:
            return MigrationResult(
                format=fmt,
                files_written=0,
                files_planned=0,
                warnings=[f"could not resolve source: {e}"],
                dry_run=True,
                namespace=namespace,
                source_path=src,
            )
        try:
            for path in src.rglob("*.md"):
                # Skip symlinks unconditionally — same posture as migrate.py
                # main() to prevent symlink-as-file exfil.
                if path.is_symlink():
                    warnings.append(f"would skip symlink: {path}")
                    continue
                if not path.is_file():
                    continue
                # Defense-in-depth: reject paths that resolve outside src
                # (e.g. via a symlinked intermediate dir that rglob descended
                # into on Python 3.10–3.12).
                try:
                    if not str(path.resolve()).startswith(str(src_resolved)):
                        warnings.append(f"would skip: resolves outside source: {path}")
                        continue
                except OSError:
                    continue
                route = route_file(path, src)
                if route is not None:
                    files_planned += 1
        except OSError as e:
            warnings.append(f"walk error: {e}")
        return MigrationResult(
            format=fmt,
            files_written=0,
            files_planned=files_planned,
            warnings=warnings,
            dry_run=True,
            namespace=namespace,
            source_path=src,
        )

    def _execute(
        self, src: Path, dst: Path, fmt: str, namespace: str
    ) -> MigrationResult:
        # Invoke migrate.py as a subprocess. Parse the structured
        # MIGRATE_RESULT_JSON line emitted as the last line of stdout. If
        # it's missing or malformed, we raise rather than silently report
        # 0 files — old behavior would mask a real migration failure
        # behind a successful-looking result.
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(_HERE / "migrate.py"), str(src), str(dst)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"migrate.py failed (exit {proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        result_json: Optional[dict] = None
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("MIGRATE_RESULT_JSON: "):
                try:
                    result_json = json.loads(line[len("MIGRATE_RESULT_JSON: "):])
                except json.JSONDecodeError:
                    pass
                break
        if result_json is None:
            raise RuntimeError(
                "migrate.py did not emit a MIGRATE_RESULT_JSON line; "
                "stdout-parse contract violated. Full stdout:\n" + proc.stdout
            )
        files_written = int(result_json.get("files_written", 0))
        lessons_written = int(result_json.get("lessons_written", 0))
        return MigrationResult(
            format=fmt,
            files_written=files_written,
            files_planned=files_written,  # post-hoc — actuals == planned in non-dry mode
            warnings=[],
            dry_run=False,
            namespace=namespace,
            source_path=src,
            tool_specific={"lessons_written": lessons_written},
        )


# ---- Adapter registry (public API) ----------------------------------
#
# PR-B and PR-C land additional adapters via `register_adapter`. Treat
# `_ADAPTERS` as private; manipulate it through the public functions so
# duplicate-format detection + Protocol-shape validation run on every add.

_ADAPTERS: list[Adapter] = []


def register_adapter(adapter: Adapter) -> None:
    """Register an adapter. Validates the Protocol shape + fails fast on
    duplicate format registration.
    """
    if not isinstance(adapter, Adapter):
        raise AdapterRegistrationError(
            f"{type(adapter).__name__} doesn't satisfy the Adapter Protocol "
            "(missing `name`, `supported_formats`, `supports()`, or `migrate()`)"
        )
    existing_formats = {f for a in _ADAPTERS for f in a.supported_formats}
    overlap = set(adapter.supported_formats) & existing_formats
    if overlap:
        raise AdapterRegistrationError(
            f"Adapter {adapter.name!r} declares formats already handled "
            f"by another registered adapter: {sorted(overlap)}"
        )
    _ADAPTERS.append(adapter)


def unregister_adapter(name: str) -> None:
    """Remove an adapter by name. No-op if unknown. Mostly used by tests."""
    _ADAPTERS[:] = [a for a in _ADAPTERS if a.name != name]


def registered_adapters() -> list[str]:
    """Names of registered adapters, in registration order."""
    return [a.name for a in _ADAPTERS]


def get_adapter_for(fmt: str) -> Optional[Adapter]:
    """The adapter that supports `fmt`, or None if none does."""
    for adapter in _ADAPTERS:
        if adapter.supports(fmt):
            return adapter
    return None


# Bootstrap: register the built-in adapters. Each adapter's module
# self-registers on import via a `_register_once()` helper, so adding a
# new one is just a new import line here.
#
# When this file runs as `python3 migrate_dispatcher.py ...` (install.sh's
# invocation), Python loads it as `__main__`. Adapter modules later do
# `from migrate_dispatcher import register_adapter`, which would
# otherwise load a SECOND copy of this module under the canonical name,
# giving each its own `_ADAPTERS` list — adapters would register on the
# canonical copy while dispatch() reads from `__main__`'s. Alias both
# names to the same module object before any adapter loads.
if __name__ == "__main__":
    sys.modules.setdefault("migrate_dispatcher", sys.modules[__name__])

register_adapter(ClaudeCodeAdapter())

# PR-B: Cursor plans adapter. Imported for the side effect of registering
# the adapter — protected against duplicate-format errors via the
# adapter module's own `_register_once()` guard.
try:
    import cursor_adapter  # noqa: F401  side-effect import
except ImportError:
    pass

# PR-C: Codex CLI adapter. Same side-effect import pattern.
try:
    import codex_adapter  # noqa: F401  side-effect import
except ImportError:
    pass


def _src_dst_overlap(src: Path, dst: Path) -> bool:
    """True if src and dst share filesystem space such that walking src
    would reach dst's tree (or vice-versa). Mirrors migrate.py main()'s
    top-level guard so dispatcher dry-run + execute paths are equally
    protected. Per Schema persona finding: dry-run was bypassing this.
    """
    try:
        s = src.resolve()
        d = dst.resolve()
    except OSError:
        return False
    return s == d or s in d.parents or d in s.parents


def dispatch(
    src: Path,
    dst: Path,
    dry_run: bool = False,
    options: Optional[dict] = None,
) -> MigrationResult:
    """Detect format, route to adapter, return result.

    `options` is an opaque dict forwarded to the adapter — keeps the
    dispatch signature stable as PR-B / PR-C grow per-adapter knobs
    (currently `namespace`; future: `symlink_native`, etc.). Unknown
    keys are passed through; adapters ignore what they don't consume.

    Raises NoAdapterError if no adapter is registered for the detected
    format.
    """
    options = options or {}
    namespace = options.get("namespace", "default")

    # Detect format BEFORE the overlap check — an already-symlinked source
    # like `~/.claude/projects/X/memory -> $BRAIN_ROOT/memory` would otherwise
    # have its symlink resolved to inside dst and the overlap guard would
    # raise, even though the correct behavior is the no-op branch below
    # (codex review P2 #2).
    fmt = detect_format(src)

    if fmt == "already-symlinked":
        return MigrationResult(
            format=fmt,
            files_written=0,
            files_planned=0,
            warnings=["source is already a symlink — assumed migrated"],
            dry_run=dry_run,
            namespace=namespace,
            source_path=src,
        )

    # Overlap guard runs only on real (non-symlink) sources.
    if _src_dst_overlap(src, dst):
        raise NoAdapterError(
            f"Source ({src}) and target ({dst}) overlap; refusing — running "
            f"migrate would walk the brain itself."
        )

    adapter = get_adapter_for(fmt)
    if adapter is not None:
        return adapter.migrate(src, dst, dry_run=dry_run, options=options)

    # No adapter — produce a message that points at the right future work.
    if fmt == "cursor-plans":
        raise NoAdapterError(
            f"Cursor plans detected at {src}, but no Cursor adapter is "
            "registered yet — see brainstack roadmap "
            "https://github.com/mustafa-bharmal-shipveho/brainstack."
        )
    if fmt == "cursor-rules":
        raise NoAdapterError(
            f"Cursor rules detected at {src}, but no Cursor adapter is "
            "registered yet — see brainstack roadmap."
        )
    if fmt == "codex-cli":
        raise NoAdapterError(
            f"Codex CLI sessions detected at {src}, but no Codex adapter "
            "is registered yet — see brainstack roadmap (rollout-*.jsonl "
            "ingest is planned for episodic/codex/)."
        )
    if fmt == "empty":
        raise NoAdapterError(f"Source has no migratable files: {src}")
    if fmt == "unknown":
        raise NoAdapterError(
            f"Source format unrecognized: {src}. brainstack supports "
            "Claude Code (flat or nested), Cursor plans, and Codex CLI "
            "sessions. If your tool's format isn't listed, file an issue "
            "with a sample source layout."
        )
    raise NoAdapterError(f"No adapter for format {fmt!r}")


# ---- CLI -------------------------------------------------------------


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n // (1024 * 1024)} MB"


def _print_candidates(cands: list[Candidate]) -> None:
    if not cands:
        print("No memory dirs found at known locations under $HOME.")
        return
    print(f"\nFound {len(cands)} memory source(s):\n")
    for i, c in enumerate(cands, 1):
        print(f"  {i}. [{c.format}] {c.path}")
        print(f"      {c.file_count} file(s), {_format_size(c.size_bytes)}")


def _print_plan_header(src: Path, dst: Path, dry_run: bool) -> str:
    """Print the plan banner. Returns the detected format string."""
    fmt = detect_format(src)
    print("\nPlan:")
    print(f"  Source:           {src}")
    print(f"  Detected format:  {fmt}")
    print(f"  Brain target:     {dst}/memory/")
    print(f"  Dry-run:          {dry_run}")
    return fmt


def _print_plan(src: Path, dst: Path) -> int:
    """ALWAYS-DRY plan-printer for the `plan` subcommand. Never executes.

    Returns exit code: 0 on a clean plan, 2 if no adapter / format unknown.
    """
    _print_plan_header(src, dst, dry_run=True)
    try:
        result = dispatch(src=src, dst=dst, dry_run=True)
    except NoAdapterError as e:
        print(f"\n  No adapter: {e}")
        return 2

    print(f"  Format:           {result.format}")
    print(f"  Would write:      {result.files_planned} file(s)")
    if result.warnings:
        print("  Warnings:")
        for w in result.warnings:
            print(f"    - {w}")
    return 0


def _swap_to_symlink(src: Path, brain_memory: Path) -> tuple[Path, Path]:
    """Replace `src` (a real dir) with a symlink to `brain_memory`.

    Atomic-ish 3-step swap mirroring install.sh's logic so that the
    interactive flow gets the same `--symlink-native` behavior as the
    power-user `--migrate <path>` flow (codex review P1 #1):

      1. Create the new symlink at a sibling temp name.
      2. Move `src` to a timestamped backup.
      3. Rename the temp symlink into `src`'s position.

    Returns (backup_path, installed_symlink_path). Raises on any failure
    after rolling back any partial state.
    """
    import secrets
    import time

    src = Path(str(src).rstrip("/") or "/")
    brain_memory_abs = brain_memory.resolve()
    if not brain_memory_abs.is_dir():
        raise RuntimeError(
            f"brain memory dir does not exist: {brain_memory_abs}"
        )
    if src.is_symlink():
        raise RuntimeError(f"source is already a symlink: {src}")

    ts = int(time.time())
    rand = f"{os.getpid()}-{secrets.token_hex(3)}"
    backup = src.parent / f"{src.name}.bak.{ts}.{rand}"
    tmp_link = src.parent / f"{src.name}.symlink-tmp.{ts}.{rand}"
    if backup.exists() or backup.is_symlink():
        raise RuntimeError(f"backup target already exists: {backup}")
    if tmp_link.exists() or tmp_link.is_symlink():
        raise RuntimeError(f"temp symlink target already exists: {tmp_link}")

    # Step 1
    os.symlink(str(brain_memory_abs), str(tmp_link))
    try:
        # Step 2
        os.replace(str(src), str(backup))
    except OSError:
        # Roll back the temp symlink.
        try:
            os.unlink(str(tmp_link))
        except OSError:
            pass
        raise

    try:
        # Step 3
        os.replace(str(tmp_link), str(src))
    except OSError:
        # Source is gone; backup retains data; tmp_link is orphaned. Surface
        # all three paths so the user can recover by hand.
        raise RuntimeError(
            f"failed to install symlink at {src}; data preserved at "
            f"{backup}, temp symlink at {tmp_link}. "
            f"Recover with: mv '{backup}' '{src}'"
        )

    return backup, src


def _execute_with_confirm(
    src: Path,
    dst: Path,
    options: Optional[dict] = None,
    *,
    skip_confirm: bool = False,
) -> int:
    """Plan-print, prompt for confirm (unless `skip_confirm`), then execute.

    For Claude Code formats with `options.symlink_native=True` (default),
    ALSO run the atomic-ish symlink swap so that future native auto-memory
    writes flow into the brain — matching the power-user `--migrate <path>`
    flow's default. Per codex review P1 #1: the previous interactive flow
    skipped this, leaving the source as a regular dir and silently breaking
    `--symlink-native`.

    Returns exit code: 0 on success, non-zero on cancel / no-adapter / error.
    """
    options = options or {}
    symlink_native = options.get("symlink_native", True)
    fmt_for_swap = detect_format(src)
    is_claude = fmt_for_swap in ClaudeCodeAdapter.supported_formats

    _print_plan_header(src, dst, dry_run=False)
    try:
        plan = dispatch(src=src, dst=dst, dry_run=True, options=options)
    except NoAdapterError as e:
        print(f"\n  No adapter: {e}")
        return 2
    print(f"  Format:           {plan.format}")
    print(f"  Would write:      {plan.files_planned} file(s)")
    if plan.warnings:
        print("  Warnings:")
        for w in plan.warnings:
            print(f"    - {w}")
    if is_claude and symlink_native:
        print(f"  Symlink swap:     yes (after migrate, source -> {dst}/memory)")
    elif is_claude:
        print(f"  Symlink swap:     no (--no-symlink)")

    if not skip_confirm:
        print()
        print("Proceed? [y/N] ", end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt, BrokenPipeError):
            print()
            return 0
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 0

    try:
        result = dispatch(src=src, dst=dst, dry_run=False, options=options)
    except NoAdapterError as e:
        print(f"\n  No adapter: {e}", file=sys.stderr)
        return 2
    print(f"\nDone. Files written: {result.files_written}")
    if result.tool_specific:
        print(f"  Adapter detail: {result.tool_specific}")
    if result.warnings:
        print("  Warnings:")
        for w in result.warnings:
            print(f"    - {w}")

    # Symlink swap for Claude Code sources — the missing piece codex caught.
    if is_claude and symlink_native:
        try:
            backup, link = _swap_to_symlink(src, dst / "memory")
        except (OSError, RuntimeError) as e:
            print(f"\nWARNING: symlink swap failed — migration data is in "
                  f"{dst}/memory but {src} was NOT replaced with a symlink. "
                  f"Native writes after this will not reach the brain.\n"
                  f"  Reason: {e}", file=sys.stderr)
            return 1
        print(f"  Backed up source -> {backup}")
        print(f"  Symlinked {link} -> {dst / 'memory'}")
        print("  Native auto-memory writes now flow into the brain.")
    return 0


def _interactive(env: Optional[dict] = None, dst: Optional[Path] = None) -> int:
    """Discover, prompt, plan-print, confirm, execute."""
    if env is None:
        env = os.environ
    if dst is None:
        dst = Path(env.get("BRAIN_ROOT") or os.path.expanduser("~/.agent"))

    cands = discover_candidates(env=env)
    _print_candidates(cands)
    if not cands:
        print("\nNothing to migrate. Pass `--migrate <path>` if your")
        print("memory lives somewhere we don't auto-discover.")
        return 0

    # Multi-Claude collision warning: PR-A migrates everything into the
    # default namespace. If the user has 2+ Claude project memories,
    # filename collisions can silently overwrite. The `--namespace NS`
    # flag is deferred to PR-B; until then, surface the risk loud.
    claude_cands = [
        c for c in cands
        if c.format in ("claude-code-flat", "claude-code-nested", "claude-code-mixed")
    ]
    if len(claude_cands) > 1:
        print()
        print("WARNING: multiple Claude Code memories found. brainstack PR-A")
        print("migrates every Claude source into the same default namespace,")
        print("so filenames that overlap across projects will silently")
        print("overwrite each other. Pick one source at a time, or wait for")
        print("the per-tool `--namespace` flag (planned for the next PR).")

    print()
    print("Pick which to migrate (number, or `none` to exit):")
    print("> ", end="", flush=True)
    try:
        choice = input().strip().lower()
    except (EOFError, KeyboardInterrupt, BrokenPipeError):
        print()
        return 0
    if choice in ("none", "no", "n", "q", "quit", "exit", ""):
        print("Cancelled.")
        return 0

    try:
        idx_one_based = int(choice)
    except ValueError:
        print(f"Invalid choice: {choice!r}")
        return 2
    # Range check BEFORE indexing — Python's negative index would silently
    # pick the last candidate for "0" / "-1" / etc. Per reliability HIGH #6.
    if idx_one_based < 1 or idx_one_based > len(cands):
        print(f"Choice out of range: {choice} (expected 1..{len(cands)})")
        return 2
    chosen = cands[idx_one_based - 1]

    print(f"\nMigrating: {chosen.path}")
    return _execute_with_confirm(src=chosen.path, dst=dst)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="migrate_dispatcher")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_disc = sub.add_parser("discover", help="list discovered candidates as JSON")

    sp_plan = sub.add_parser("plan", help="dry-run plan for a specific source")
    sp_plan.add_argument("src")
    sp_plan.add_argument("dst", nargs="?", default=None)

    sp_exec = sub.add_parser("execute", help="run migration for a specific source")
    sp_exec.add_argument("src")
    sp_exec.add_argument("dst")

    sp_int = sub.add_parser("interactive", help="discover + prompt + execute")

    args = p.parse_args(argv)

    if args.cmd == "discover":
        cands = discover_candidates()
        json.dump([c.to_dict() for c in cands], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.cmd == "plan":
        dst = Path(args.dst) if args.dst else Path(os.environ.get("BRAIN_ROOT") or os.path.expanduser("~/.agent"))
        return _print_plan(src=Path(args.src), dst=dst)

    if args.cmd == "execute":
        try:
            result = dispatch(src=Path(args.src), dst=Path(args.dst), dry_run=False)
        except NoAdapterError as e:
            print(f"migrate: {e}", file=sys.stderr)
            return 2
        # Emit JSON envelope so external tooling has a stable shape.
        print(json.dumps(result.to_dict()))
        return 0

    if args.cmd == "interactive":
        return _interactive()

    return 2


if __name__ == "__main__":
    sys.exit(main())
