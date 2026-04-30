"""Cursor adapter for the migrate dispatcher.

Ingests `~/.cursor/plans/*.plan.md` files into the brain at
`<brain>/memory/personal/notes/cursor/`. Plans are copied verbatim
(byte-for-byte) — Cursor plans are markdown with YAML frontmatter,
which the brain stores as-is. No reformat, no schema mangling.

Future extensions (not in this PR):
  - `.cursorrules` files at repo roots — needs discovery extension
    to walk codebase dirs, not just $HOME
  - Per-project plan grouping based on filename prefix
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import ClassVar, Optional

# Path-relative imports — same shape as migrate_dispatcher.
_HERE = Path(__file__).resolve().parent
_BASE = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_BASE / "memory"))

from _atomic import atomic_write_bytes  # noqa: E402
from migrate_dispatcher import (  # noqa: E402
    AdapterRegistrationError,
    MigrationResult,
    register_adapter,
    registered_adapters,
)


_TARGET_REL = Path("personal") / "notes" / "cursor"


class CursorPlansAdapter:
    """Migrates `*.plan.md` files from Cursor's plans dir into the brain."""

    name = "cursor-plans"
    supported_formats: ClassVar[frozenset[str]] = frozenset({"cursor-plans"})

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
        # Cursor plans live under their own logical namespace so they
        # don't collide with Claude Code's default. PR-A reserved the
        # `namespace` field; this adapter sets it to "cursor" by default.
        namespace = options.get("namespace", "cursor")

        target_dir = dst / "memory" / _TARGET_REL
        warnings: list[str] = []

        # Walk source for plan files, skipping symlinks (defense-in-depth
        # against a malicious symlink pointing at sensitive content).
        plan_files: list[Path] = []
        try:
            for path in sorted(src.glob("*.plan.md")):
                if path.is_symlink():
                    warnings.append(f"would skip symlink: {path}")
                    continue
                if not path.is_file():
                    continue
                plan_files.append(path)
        except OSError as e:
            warnings.append(f"walk error: {e}")

        if dry_run:
            return MigrationResult(
                format="cursor-plans",
                files_written=0,
                files_planned=len(plan_files),
                warnings=warnings,
                dry_run=True,
                namespace=namespace,
                source_path=src,
                tool_specific={"plans_planned": len(plan_files)},
            )

        # Execute: atomic write each plan to the target dir. Per codex
        # review P2: an OSError mid-loop must NOT silently produce a
        # successful-looking result; partial migration is a real failure
        # the caller needs to surface (permission denied, disk full, etc.).
        if plan_files:
            target_dir.mkdir(parents=True, exist_ok=True)
        files_written = 0
        for path in plan_files:
            target = target_dir / path.name
            atomic_write_bytes(target, path.read_bytes())
            files_written += 1

        return MigrationResult(
            format="cursor-plans",
            files_written=files_written,
            files_planned=len(plan_files),
            warnings=warnings,
            dry_run=False,
            namespace=namespace,
            source_path=src,
            tool_specific={"plans_imported": files_written},
        )


# Register on import. Idempotent: if the dispatcher's bootstrap path
# imports this module twice (e.g. once via direct test import and once
# via discovery), the second register_adapter would raise on duplicate-
# format. Guard against that.
def _register_once() -> None:
    if "cursor-plans" in registered_adapters():
        return
    try:
        register_adapter(CursorPlansAdapter())
    except AdapterRegistrationError:
        # Race: someone else registered it between our check and the call.
        # Fine — they got there first.
        pass


_register_once()
