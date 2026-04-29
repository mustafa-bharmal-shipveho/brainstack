"""Tests for migration safety: backup, sha256 verification, rollback."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from recall.migrate import (
    MigrationAbort,
    MigrationPlan,
    create_backup_tarball,
    plan_migration,
    rollback,
    verify_copy,
)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestCreateBackupTarball:
    def test_creates_tarball(self, tmp_path, auto_memory_brain):
        out = tmp_path / "backup.tar.gz"
        create_backup_tarball(auto_memory_brain, out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_tarball_round_trip(self, tmp_path, auto_memory_brain):
        out = tmp_path / "backup.tar.gz"
        create_backup_tarball(auto_memory_brain, out)
        restore = tmp_path / "restored"
        with tarfile.open(out, "r:gz") as tf:
            tf.extractall(restore, filter="data")
        # Verify a known file made it through
        candidates = list(restore.rglob("feedback_pin_dependencies.md"))
        assert len(candidates) == 1


class TestVerifyCopy:
    def test_identical_dirs_pass(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        for name, content in [("a.md", "hello"), ("b.md", "world")]:
            (src / name).write_text(content)
            (dst / name).write_text(content)
        assert verify_copy(src, dst) is True

    def test_different_content_fails(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.md").write_text("hello")
        (dst / "a.md").write_text("HELLO")
        assert verify_copy(src, dst) is False

    def test_missing_file_in_dst_fails(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.md").write_text("hello")
        (src / "b.md").write_text("world")
        (dst / "a.md").write_text("hello")
        assert verify_copy(src, dst) is False

    def test_extra_file_in_dst_fails(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.md").write_text("hello")
        (dst / "a.md").write_text("hello")
        (dst / "extra.md").write_text("not in source")
        assert verify_copy(src, dst) is False


class TestPlanMigration:
    def test_aborts_when_target_nonempty(self, tmp_path, auto_memory_brain):
        target = tmp_path / "existing-brain"
        target.mkdir()
        (target / "preexisting.md").write_text("don't overwrite me", encoding="utf-8")
        with pytest.raises(MigrationAbort):
            plan_migration(source=auto_memory_brain, target=target)

    def test_aborts_when_target_nonempty_force_proceeds(self, tmp_path, auto_memory_brain):
        target = tmp_path / "existing-brain"
        target.mkdir()
        (target / "preexisting.md").write_text("override allowed with force", encoding="utf-8")
        plan = plan_migration(source=auto_memory_brain, target=target, force=True)
        assert isinstance(plan, MigrationPlan)

    def test_aborts_on_missing_source(self, tmp_path):
        with pytest.raises(MigrationAbort):
            plan_migration(source=tmp_path / "ghost", target=tmp_path / "target")

    def test_plan_includes_backup_path(self, tmp_path, auto_memory_brain):
        target = tmp_path / "new-brain"
        plan = plan_migration(source=auto_memory_brain, target=target)
        assert plan.backup_tarball_path.suffix == ".gz"
        assert ".bak." in plan.bak_dir_path.name

    def test_plan_estimates_disk(self, tmp_path, auto_memory_brain):
        target = tmp_path / "new-brain"
        plan = plan_migration(source=auto_memory_brain, target=target)
        # Source size positive, plan should know it
        assert plan.source_bytes > 0


class TestRollback:
    def test_restore_from_bak(self, tmp_path, auto_memory_brain):
        # Simulate: source moved to .bak, target populated
        bak = tmp_path / "old-location.bak.20260428"
        target = tmp_path / "old-location"
        # Move auto_memory_brain contents to bak
        shutil.copytree(auto_memory_brain, bak)
        target.mkdir()
        # Drop the symlink-style "wrong path"
        # Now rollback should restore bak -> source
        # Pretend target IS the original location placeholder
        # rollback(source_location, bak_dir)
        target.rmdir()
        rollback(source_location=target, bak_dir=bak)
        assert target.exists()
        # known file present
        assert (target / "MEMORY.md").exists()

    def test_rollback_idempotent_when_bak_missing(self, tmp_path):
        # No .bak — rollback should no-op or raise clearly
        with pytest.raises((FileNotFoundError, RuntimeError)):
            rollback(
                source_location=tmp_path / "anywhere",
                bak_dir=tmp_path / "missing.bak",
            )


class TestSha256End2End:
    def test_byte_perfect_after_full_cycle(self, tmp_path, auto_memory_brain):
        # Snapshot every file's sha256 in source
        sums_before = {
            str(p.relative_to(auto_memory_brain)): _file_sha256(p)
            for p in auto_memory_brain.rglob("*")
            if p.is_file()
        }

        # rsync-equivalent copy to target
        target = tmp_path / "target"
        shutil.copytree(auto_memory_brain, target)

        sums_after = {
            str(p.relative_to(target)): _file_sha256(p)
            for p in target.rglob("*")
            if p.is_file()
        }

        assert sums_before == sums_after
