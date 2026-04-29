"""Migration safety: backup tarball, sha256 verification, rollback.

Migration moves an existing memory directory to a canonical $BRAIN_HOME with
two-layer backup (tarball + renamed .bak dir). Reversible until the user runs
`recall migrate --finalize`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path


class MigrationAbort(RuntimeError):
    """Raised when migration cannot safely proceed."""


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    target: Path
    backup_tarball_path: Path
    bak_dir_path: Path
    source_bytes: int
    file_count: int


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def create_backup_tarball(source: Path, output: Path) -> None:
    """Create a gzipped tarball of `source` at `output`. Raises on error."""
    if not source.exists():
        raise MigrationAbort(f"Source does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as tf:
        tf.add(source, arcname=source.name)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                rel = str(p.relative_to(root))
                result[rel] = _file_sha256(p)
            except (OSError, ValueError):
                continue
    return result


def verify_copy(src: Path, dst: Path) -> bool:
    """Verify dst contains exactly the same files (by sha256) as src."""
    if not src.exists() or not dst.exists():
        return False
    return _scan(src) == _scan(dst)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _iso_timestamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _dir_size_bytes(path: Path) -> tuple[int, int]:
    total = 0
    count = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def _is_descendant(child: Path, parent: Path) -> bool:
    """True if child is parent or inside parent.

    Works for non-existent paths by walking up to the nearest existing
    ancestor before resolving symlinks. Pure lexical for the unresolved tail.
    """

    def _resolve_existing(p: Path) -> Path:
        # Walk up until we hit something that exists, resolve that, then
        # re-append the tail. Handles cases where target/ doesn't exist yet.
        try:
            return p.resolve(strict=False)
        except OSError:
            return p.absolute()

    try:
        child_r = _resolve_existing(child)
        parent_r = _resolve_existing(parent)
    except OSError:
        return False
    if child_r == parent_r:
        return True
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


def plan_migration(
    source: Path, target: Path, force: bool = False
) -> MigrationPlan:
    if not source.exists():
        raise MigrationAbort(f"Source does not exist: {source}")
    if not source.is_dir():
        raise MigrationAbort(f"Source is not a directory: {source}")

    # Refuse target == source — that's a no-op at best, self-destruction at worst.
    try:
        if source.resolve() == target.resolve():
            raise MigrationAbort(
                f"Target cannot equal source: {source}. Pick a different BRAIN_HOME."
            )
    except OSError:
        # Target may not exist yet, which is fine — only resolve source.
        pass

    # Refuse nested source/target — would either back up into itself or
    # overwrite the source mid-migration. Apply lexically so non-existent
    # targets are also caught.
    if _is_descendant(target, source):
        raise MigrationAbort(
            f"Target ({target}) is inside source ({source}). Choose a target outside the source tree."
        )
    if target.exists() and _is_descendant(source, target):
        raise MigrationAbort(
            f"Source ({source}) is inside target ({target}). Choose a source outside the target tree."
        )

    if target.exists():
        # Allow if empty
        is_empty = not any(target.iterdir())
        if not is_empty and not force:
            raise MigrationAbort(
                f"Target already exists and is non-empty: {target}. "
                "Re-run with force=True (or --force) to override; a backup tarball "
                "of the source will still be created."
            )

    timestamp = _iso_timestamp()
    backup_dir = target.parent / ".backups"
    backup_tarball = backup_dir / f"pre-migration-{timestamp}.tar.gz"
    bak_dir = source.parent / f"{source.name}.bak.{timestamp}"

    total, count = _dir_size_bytes(source)
    return MigrationPlan(
        source=source,
        target=target,
        backup_tarball_path=backup_tarball,
        bak_dir_path=bak_dir,
        source_bytes=total,
        file_count=count,
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def rollback(source_location: Path, bak_dir: Path) -> None:
    """Restore `bak_dir` to `source_location`. Raises if bak_dir missing."""
    if not bak_dir.exists():
        raise FileNotFoundError(f"Backup directory not found: {bak_dir}")
    if source_location.exists():
        if source_location.is_symlink():
            source_location.unlink()
        elif source_location.is_dir() and not any(source_location.iterdir()):
            source_location.rmdir()
        else:
            raise RuntimeError(
                f"Cannot rollback: {source_location} exists and is non-empty. "
                "Move it aside before retrying."
            )
    shutil.move(str(bak_dir), str(source_location))
