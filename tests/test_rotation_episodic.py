"""Rotation of `AGENT_LEARNINGS.jsonl` — writers and readers (S5 / R5).

The brain's episodic JSONL grows without bound today: the live
`memory/episodic/codex/AGENT_LEARNINGS.jsonl` reached 107 MB and GitHub
rejected the push. S5 adds size-triggered rotation so no single tracked
file can grow past the threshold.

Contract pinned here
--------------------
Naming
    `<stem>.<YYYY-MM-DD><suffix>` — `AGENT_LEARNINGS.jsonl` rolls to
    `AGENT_LEARNINGS.2026-09-04.jsonl`. A second roll on the same day
    takes a counter before the suffix: `AGENT_LEARNINGS.2026-09-04.1.jsonl`.
    The day is UTC.

Writers rotate, readers glob
    Every writer checks the current file's size *before* appending and
    renames it out of the way once it is at/over the threshold. Rolled
    files are then immutable — nothing ever appends to them again.
    Readers (`sdk.stats`, `auto_dream`, `consolidate`, the dedup preload
    in `claude_session_adapter`) glob `AGENT_LEARNINGS*.jsonl` so history
    that moved into a rolled file is still visible.

Boundary rule
    Rotation triggers when the file is strictly LARGER than the threshold.
    Exactly at the threshold is not oversize. (`plans/guards.md` writes
    this as `size >= threshold`; the tests pin `>`, so all three rotation
    sites need the strict comparison.)

Threshold injection — IMPLEMENTATION NOTE
    These tests set the threshold two ways:
      * explicitly, via the `max_bytes=` keyword on `append_jsonl`, and
      * by monkeypatching the module constant `ROTATE_BYTES`.
    The second only works if the constant is read **at call time**
    (e.g. `max_bytes: int | None = None` resolved to `ROTATE_BYTES`
    inside the body), not bound as a def-time default argument. Callers
    like `sdk.append_episodic` and the codex/claude-session adapters do
    not thread a threshold through, so monkeypatching the constant is
    the only hermetic way to exercise their rotation path.

Everything here is hermetic: tmp brains only, never `~/.agent`.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "agent" / "harness" / "hooks"
MEMORY_DIR = REPO_ROOT / "agent" / "memory"
HARNESS_DIR = REPO_ROOT / "agent" / "harness"
TOOLS_DIR = REPO_ROOT / "agent" / "tools"

# Order matters: `promote.py` exists in BOTH agent/tools and agent/memory,
# and auto_dream imports the memory one. Insert tools first so memory ends up
# ahead of it on sys.path (same convention as tests/test_codex_adapter.py).
for _d in (TOOLS_DIR, HARNESS_DIR, HOOKS_DIR, MEMORY_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# POSIX-only: rotation rides on the same flock sentinel as the appender.
fcntl = pytest.importorskip("fcntl")

import _atomic  # noqa: E402
import _episodic_io  # noqa: E402

from agent.memory import sdk  # noqa: E402


DAY = "2026-09-04"
CURRENT = "AGENT_LEARNINGS.jsonl"
ROLLED_GLOB = "AGENT_LEARNINGS*.jsonl"
# Rolled-file name shape, e.g. AGENT_LEARNINGS.2026-09-04.jsonl and
# AGENT_LEARNINGS.2026-09-04.1.jsonl
ROLLED_RE = re.compile(r"^AGENT_LEARNINGS\.\d{4}-\d{2}-\d{2}(\.\d+)?\.jsonl$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _entry(idx: int, *, pad: int = 1500, ts: str | None = None) -> dict:
    """One episode roughly `pad` bytes wide so roll points are predictable."""
    return {
        "id": f"e-{idx:04d}",
        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
        "action": f"step {idx}",
        "summary": "s" * pad,
    }


def _exact_line(nbytes: int, marker: str) -> dict:
    """An entry whose serialized JSONL line is exactly `nbytes` long."""
    overhead = len(json.dumps({"id": marker, "pad": ""}) + "\n")
    pad = nbytes - overhead
    assert pad >= 0, f"{nbytes} is too small to hold marker {marker!r}"
    entry = {"id": marker, "pad": "x" * pad}
    assert len(json.dumps(entry) + "\n") == nbytes
    return entry


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


def _ids(path: Path) -> list[str]:
    out = []
    for ln in _lines(path):
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            out.append(row["id"])
    return out


def _episodic_glob(directory: Path) -> list[Path]:
    return sorted(directory.glob(ROLLED_GLOB))


def _all_ids(directory: Path) -> set[str]:
    ids: set[str] = set()
    for p in _episodic_glob(directory):
        ids |= set(_ids(p))
    return ids


def _total_lines(directory: Path) -> int:
    return sum(len(_lines(p)) for p in _episodic_glob(directory))


def _rolled_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob(ROLLED_GLOB) if p.name != CURRENT)


def _seed_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _fixed_day(monkeypatch) -> None:
    """Pin the UTC day the appender stamps into rolled names."""
    monkeypatch.setattr(_episodic_io, "_today", lambda: DAY)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Keep the dream cycle and the consolidator away from the real user.

    `topic_keys.default_extractors` reads `$XDG_CONFIG_HOME/brainstack`
    (falling back to `~/.config/brainstack`) to decide whether to run the
    LLM extractor. A test must never inherit that, or it may shell out to
    a real provider.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    return home


def _make_brain(brain: Path) -> Path:
    """Minimal brain layout the dream cycle needs (shape from
    tests/test_dream_runner.py::make_brain)."""
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "episodic" / "snapshots").mkdir()
    (brain / "memory" / "working").mkdir()
    (brain / "memory" / "candidates").mkdir()
    (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
    (brain / "memory" / "episodic" / CURRENT).touch()
    return brain


def _import_memory(name: str):
    """Import a module that lives in agent/memory/ (not a package)."""
    return __import__(name)


def _patch_dream_globals(monkeypatch, auto_dream, brain: Path) -> None:
    """Point `run_dream_cycle()`'s module-level paths at a tmp brain.

    `run_dream_cycle` reads ROOT / EPISODIC / CANDIDATES / SEMANTIC /
    REVIEW_QUEUE at call time, so patching the module attributes is
    enough (same trick tests/test_concurrent_appends.py uses).
    """
    mem = brain / "memory"
    episodic = mem / "episodic" / CURRENT
    monkeypatch.setenv("BRAIN_ROOT", str(brain))
    monkeypatch.setattr(auto_dream, "ROOT", str(mem))
    monkeypatch.setattr(auto_dream, "EPISODIC", str(episodic))
    monkeypatch.setattr(auto_dream, "EPISODIC_LOCK", str(episodic) + ".lock")
    monkeypatch.setattr(auto_dream, "CANDIDATES", str(mem / "candidates"))
    monkeypatch.setattr(auto_dream, "SEMANTIC", str(mem / "semantic"))
    monkeypatch.setattr(
        auto_dream, "REVIEW_QUEUE", str(mem / "working" / "REVIEW_QUEUE.md")
    )


# --------------------------------------------------------------------------
# _episodic_io.append_jsonl — the hook + SDK writer
# --------------------------------------------------------------------------


def test_append_jsonl_rolls_at_threshold_into_dated_file(tmp_path, monkeypatch):
    """Crossing the threshold renames the current file to a dated sibling;
    the fresh current file holds only the lines written after the roll."""
    _fixed_day(monkeypatch)
    epi = tmp_path / "memory" / "episodic"
    path = epi / CURRENT

    _episodic_io.append_jsonl(str(path), _entry(0), max_bytes=1024)
    assert _rolled_files(epi) == [], "must not roll before the threshold is crossed"

    # The file is now ~1.5 KB, i.e. over the 1 KB threshold, so the next
    # append rotates first and lands in a brand-new current file.
    _episodic_io.append_jsonl(str(path), _entry(1), max_bytes=1024)

    rolled = epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    assert rolled.exists(), (
        f"expected a dated roll {rolled.name}; found {[p.name for p in _episodic_glob(epi)]}"
    )
    assert _ids(rolled) == ["e-0000"]
    assert _ids(path) == ["e-0001"], "current file must hold only post-roll lines"
    assert _total_lines(epi) == 2, "no line may be lost across a roll"


def test_append_jsonl_boundary_rolls_only_past_threshold(tmp_path, monkeypatch):
    """Exactly at `max_bytes` is not oversize; one byte over is.

    Off-by-one here decides whether a brain that hovers at the threshold
    rolls on every single append or never rolls at all.
    """
    _fixed_day(monkeypatch)
    threshold = 512

    at_dir = tmp_path / "at" / "episodic"
    at = at_dir / CURRENT
    _episodic_io.append_jsonl(str(at), _exact_line(threshold, "at"), max_bytes=threshold)
    assert at.stat().st_size == threshold
    _episodic_io.append_jsonl(str(at), {"id": "after"}, max_bytes=threshold)
    assert _rolled_files(at_dir) == [], (
        "a file sitting exactly at the threshold must not roll"
    )
    assert _ids(at) == ["at", "after"]

    over_dir = tmp_path / "over" / "episodic"
    over = over_dir / CURRENT
    _episodic_io.append_jsonl(str(over), _exact_line(threshold + 1, "over"),
                              max_bytes=threshold)
    assert over.stat().st_size == threshold + 1
    _episodic_io.append_jsonl(str(over), {"id": "after"}, max_bytes=threshold)
    rolls = _rolled_files(over_dir)
    assert len(rolls) == 1, (
        f"one byte over the threshold must roll; found {[p.name for p in rolls]}"
    )
    assert _ids(rolls[0]) == ["over"]
    assert _ids(over) == ["after"]


def test_roll_twice_same_day_gets_counter_suffix(tmp_path, monkeypatch):
    """A second roll on the same UTC day gets `.1` before the suffix."""
    _fixed_day(monkeypatch)
    epi = tmp_path / "memory" / "episodic"
    path = epi / CURRENT

    for i in range(4):
        _episodic_io.append_jsonl(str(path), _entry(i), max_bytes=1024)

    first = epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    second = epi / f"AGENT_LEARNINGS.{DAY}.1.jsonl"
    third = epi / f"AGENT_LEARNINGS.{DAY}.2.jsonl"
    assert first.exists() and second.exists() and third.exists(), (
        f"expected three same-day rolls; found {[p.name for p in _episodic_glob(epi)]}"
    )
    assert _ids(first) == ["e-0000"]
    assert _ids(second) == ["e-0001"]
    assert _ids(third) == ["e-0002"]
    assert _ids(path) == ["e-0003"]
    assert _total_lines(epi) == 4


def test_rolled_file_never_appended_again(tmp_path, monkeypatch):
    """Once rolled, a file is immutable — later appends go to the current
    file only. Rolled files are what `snapshots/` archival and git-history
    pruning rely on being stable."""
    _fixed_day(monkeypatch)
    epi = tmp_path / "memory" / "episodic"
    path = epi / CURRENT

    _episodic_io.append_jsonl(str(path), _entry(0), max_bytes=1024)
    _episodic_io.append_jsonl(str(path), _entry(1), max_bytes=1024)
    rolled = epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    frozen = rolled.read_bytes()

    for i in range(2, 6):
        _episodic_io.append_jsonl(str(path), _entry(i), max_bytes=1024)

    assert rolled.read_bytes() == frozen, "a rolled file was written to again"
    assert _total_lines(epi) == 6


# --- concurrency ----------------------------------------------------------


def _rot_appender_worker(jsonl_path: str, hooks_dir: str, worker_id: int,
                         n_rows: int, max_bytes: int) -> None:
    """Append n_rows rows through the real hook writer (spawned child)."""
    sys.path.insert(0, hooks_dir)
    import _episodic_io as io  # noqa: PLC0415 — child process import

    for i in range(n_rows):
        io.append_jsonl(
            jsonl_path,
            {"id": f"cc-{worker_id:02d}-{i:03d}", "summary": "p" * 300},
            max_bytes=max_bytes,
        )


@pytest.mark.slow
@pytest.mark.timeout(120)
def test_concurrent_appenders_across_roll_lose_nothing(tmp_path):
    """8 processes appending through dozens of rolls lose no rows.

    The rename happens while the appender holds the `<path>.lock` sentinel,
    so a roll can never land between another writer's open() and write().
    """
    epi = tmp_path / "memory" / "episodic"
    epi.mkdir(parents=True)
    jsonl = epi / CURRENT
    jsonl.touch()

    n_workers, rows_per_worker, max_bytes = 8, 25, 1024
    expected = {
        f"cc-{w:02d}-{i:03d}"
        for w in range(n_workers)
        for i in range(rows_per_worker)
    }

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(
            target=_rot_appender_worker,
            args=(str(jsonl), str(HOOKS_DIR), w, rows_per_worker, max_bytes),
        )
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=90)
    for p in procs:
        assert p.exitcode == 0, f"appender exited {p.exitcode}"

    assert _rolled_files(epi), "expected at least one roll under this load"
    for rolled in _rolled_files(epi):
        assert ROLLED_RE.match(rolled.name), f"bad rolled name {rolled.name}"

    surviving = _all_ids(epi)
    lost = expected - surviving
    assert not lost, (
        f"data loss across rolls: {len(lost)}/{len(expected)} rows missing; "
        f"sample {sorted(lost)[:10]}"
    )
    assert _total_lines(epi) == len(expected), "duplicate rows across rolled files"


# --------------------------------------------------------------------------
# _atomic — the shared primitives the adapters, dream and consolidate use
# --------------------------------------------------------------------------


def test_atomic_rolled_name_rotate_and_episodic_files(tmp_path):
    """`rolled_name` / `rotate_if_oversize` / `episodic_files` contract."""
    epi = tmp_path / "episodic"
    epi.mkdir()
    current = epi / CURRENT

    assert _atomic.rolled_name(current, DAY) == epi / f"AGENT_LEARNINGS.{DAY}.jsonl"

    _seed_jsonl(current, [{"id": "a"}, {"id": "b"}])
    rolled = _atomic.rotate_if_oversize(current, max_bytes=1, today=DAY)
    assert rolled == epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    assert rolled.exists() and not current.exists()
    assert _ids(rolled) == ["a", "b"]

    # Under the threshold → no rotation, returns None.
    _seed_jsonl(current, [{"id": "c"}])
    assert _atomic.rotate_if_oversize(current, max_bytes=10_000_000, today=DAY) is None
    assert current.exists()

    # Same-day second roll takes the counter suffix.
    second = _atomic.rotate_if_oversize(current, max_bytes=1, today=DAY)
    assert second == epi / f"AGENT_LEARNINGS.{DAY}.1.jsonl"

    _seed_jsonl(current, [{"id": "d"}])
    (epi / "_imported.jsonl").write_text("{}\n")  # sibling stream, not ours
    files = _atomic.episodic_files(current)
    assert [p.name for p in files] == [
        f"AGENT_LEARNINGS.{DAY}.1.jsonl",
        f"AGENT_LEARNINGS.{DAY}.jsonl",
        CURRENT,
    ], "rolled files ascending by name, current last"


def test_atomic_rotate_boundary_rolls_only_past_threshold(tmp_path):
    """The adapters' rotation site follows the same strict-greater rule as
    the two append writers."""
    epi = tmp_path / "episodic"
    current = epi / CURRENT
    _seed_jsonl(current, [{"id": "a"}])
    size = current.stat().st_size

    assert _atomic.rotate_if_oversize(current, max_bytes=size, today=DAY) is None, (
        "a file sitting exactly at the threshold must not roll"
    )
    assert current.exists()

    rolled = _atomic.rotate_if_oversize(current, max_bytes=size - 1, today=DAY)
    assert rolled == epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    assert not current.exists()

    # Nothing to roll once the file is gone.
    assert _atomic.rotate_if_oversize(current, max_bytes=1, today=DAY) is None


# --------------------------------------------------------------------------
# agent/memory/sdk.py — namespace writer + stats reader
# --------------------------------------------------------------------------


def test_sdk_append_episodic_rolls_namespace_file(tmp_path, monkeypatch):
    """`sdk.append_episodic` inherits rotation from the hook writer.

    It passes no threshold, so this only passes if `ROTATE_BYTES` is read
    at call time (see the module docstring).
    """
    _fixed_day(monkeypatch)
    monkeypatch.setattr(_episodic_io, "ROTATE_BYTES", 1024)
    brain = tmp_path / ".agent"

    for i in range(4):
        sdk.append_episodic("inbox", _entry(i), brain_root=str(brain))

    ns_dir = brain / "memory" / "episodic" / "inbox"
    assert (ns_dir / f"AGENT_LEARNINGS.{DAY}.jsonl").exists(), (
        f"namespace file never rolled; found {[p.name for p in _episodic_glob(ns_dir)]}"
    )
    assert _total_lines(ns_dir) == 4
    assert _all_ids(ns_dir) == {f"e-{i:04d}" for i in range(4)}


def test_sdk_stats_counts_rolled_files(tmp_path):
    """Episode counts span rolled history, not just the current file."""
    brain = tmp_path / ".agent"
    epi = brain / "memory" / "episodic"
    _seed_jsonl(epi / CURRENT, [{"id": f"d{i}"} for i in range(3)])
    _seed_jsonl(epi / "AGENT_LEARNINGS.2026-09-03.jsonl", [{"id": f"r{i}"} for i in range(5)])
    _seed_jsonl(epi / "inbox" / CURRENT, [{"id": f"i{i}"} for i in range(2)])
    _seed_jsonl(epi / "inbox" / f"AGENT_LEARNINGS.{DAY}.jsonl", [{"id": f"j{i}"} for i in range(4)])

    out = sdk.stats(brain_root=str(brain))
    assert out["perNamespace"]["default"]["episodes"] == 8
    assert out["perNamespace"]["inbox"]["episodes"] == 6
    assert out["episodeCount"] == 14


def test_sdk_lists_namespace_with_only_a_rolled_file(tmp_path):
    """A namespace whose current file has just been rolled away still
    exists. Without this, a rotation would make the namespace vanish from
    `recall`/`sdk stats` until the next append."""
    brain = tmp_path / ".agent"
    epi = brain / "memory" / "episodic"
    _seed_jsonl(epi / f"AGENT_LEARNINGS.{DAY}.jsonl", [{"id": "top"}])
    _seed_jsonl(epi / "codex" / f"AGENT_LEARNINGS.{DAY}.jsonl",
                [{"id": f"c{i}"} for i in range(3)])

    out = sdk.stats(brain_root=str(brain))
    assert "codex" in out["namespaces"]
    assert "default" in out["namespaces"]
    assert out["perNamespace"]["codex"]["episodes"] == 3
    assert out["perNamespace"]["default"]["episodes"] == 1


# --------------------------------------------------------------------------
# auto_dream — reads rolled + current, rewrites current, archives old rolls
# --------------------------------------------------------------------------


def test_auto_dream_clusters_rolled_entries_but_rewrites_only_current(
    tmp_path, isolated_home, monkeypatch
):
    """Clustering sees rolled history; the rewrite touches only the
    current file, so a rolled file survives a dream cycle byte-identical."""
    auto_dream = _import_memory("auto_dream")
    brain = _make_brain(tmp_path / ".agent")
    epi = brain / "memory" / "episodic"

    rolled = epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    _seed_jsonl(rolled, [
        {"id": "old-1", "timestamp": _iso_days_ago(1), "action": "rolled one"},
        {"id": "old-2", "timestamp": _iso_days_ago(1), "action": "rolled two"},
    ])
    _seed_jsonl(epi / CURRENT, [
        {"id": "new-1", "timestamp": _iso_days_ago(0), "action": "current one"},
    ])
    frozen = rolled.read_bytes()

    seen: list[list[dict]] = []

    def _spy(entries, **kwargs):
        seen.append(list(entries))
        return {}

    monkeypatch.setattr(auto_dream, "cluster_and_extract", _spy)

    auto_dream.run(brain_root=str(brain), namespace="default")

    assert seen, "cluster_and_extract was never called"
    clustered = {e.get("id") for e in seen[0]}
    assert {"old-1", "old-2", "new-1"} <= clustered, (
        f"rolled entries were not clustered; saw {sorted(clustered)}"
    )
    assert rolled.read_bytes() == frozen, "the dream cycle rewrote a rolled file"
    assert _ids(epi / CURRENT) == ["new-1"], (
        "the rewrite must contain only entries whose source was the current file"
    )


def test_auto_dream_archives_roll_when_all_entries_expired(
    tmp_path, isolated_home, monkeypatch
):
    """A rolled file whose newest entry is past DECAY_DAYS moves to
    snapshots/, so disk use is bounded without touching live history."""
    auto_dream = _import_memory("auto_dream")
    brain = _make_brain(tmp_path / ".agent")
    _patch_dream_globals(monkeypatch, auto_dream, brain)
    epi = brain / "memory" / "episodic"

    expired = epi / f"AGENT_LEARNINGS.{DAY}.jsonl"
    _seed_jsonl(expired, [
        {"id": "gone-1", "timestamp": _iso_days_ago(200), "action": "ancient"},
        {"id": "gone-2", "timestamp": _iso_days_ago(120), "action": "ancient too"},
    ])
    fresh_roll = epi / "AGENT_LEARNINGS.2026-09-05.jsonl"
    _seed_jsonl(fresh_roll, [
        {"id": "keep-1", "timestamp": _iso_days_ago(2), "action": "recent roll"},
    ])
    _seed_jsonl(epi / CURRENT, [
        {"id": "live-1", "timestamp": _iso_days_ago(0), "action": "live"},
    ])

    auto_dream.run_dream_cycle()

    assert not expired.exists(), "expired roll was left in the live episodic dir"
    archived = list((epi / "snapshots").glob(expired.name))
    assert archived, (
        f"expired roll not archived; snapshots holds "
        f"{[p.name for p in (epi / 'snapshots').glob('*')]}"
    )
    assert fresh_roll.exists(), "a roll with in-window entries must not be archived"
    assert (epi / CURRENT).exists()


def test_auto_dream_archives_rolls_in_unregistered_namespaces(
    tmp_path, isolated_home, monkeypatch
):
    """codex / claude-sessions have no registered clusterer, but their
    rolls must still be bounded — the archive sweep walks every
    `memory/episodic/<ns>/` dir."""
    auto_dream = _import_memory("auto_dream")
    brain = _make_brain(tmp_path / ".agent")
    _patch_dream_globals(monkeypatch, auto_dream, brain)
    epi = brain / "memory" / "episodic"

    ns_dir = epi / "codex"
    expired = ns_dir / f"AGENT_LEARNINGS.{DAY}.jsonl"
    _seed_jsonl(expired, [
        {"id": "codex-old", "timestamp": _iso_days_ago(150), "action": "ancient"},
    ])
    ns_current = ns_dir / CURRENT
    _seed_jsonl(ns_current, [
        {"id": "codex-live", "timestamp": _iso_days_ago(1), "action": "live"},
    ])
    _seed_jsonl(epi / CURRENT, [
        {"id": "live-1", "timestamp": _iso_days_ago(0), "action": "live"},
    ])

    auto_dream.run_dream_cycle()

    assert not expired.exists(), "expired namespace roll left in the live tree"
    assert list(epi.rglob(f"snapshots/{expired.name}")), (
        f"namespace roll not archived; episodic tree now "
        f"{sorted(str(p.relative_to(epi)) for p in epi.rglob('*') if p.is_file())}"
    )
    assert _ids(ns_current) == ["codex-live"], (
        "the namespace's current file must be left alone"
    )


# --------------------------------------------------------------------------
# consolidate — the claim-store reader
# --------------------------------------------------------------------------


def test_consolidate_episodic_paths_globs_rolled_sorted(tmp_path):
    """`_episodic_paths` returns rolled files (ascending) then the current
    file, per directory, and never descends into snapshots/."""
    consolidate = _import_memory("consolidate")
    brain = tmp_path / ".agent"
    epi = brain / "memory" / "episodic"

    _seed_jsonl(epi / CURRENT, [{"id": "top-current"}])
    _seed_jsonl(epi / "AGENT_LEARNINGS.2026-09-03.jsonl", [{"id": "top-r1"}])
    _seed_jsonl(epi / "AGENT_LEARNINGS.2026-09-04.jsonl", [{"id": "top-r2"}])
    _seed_jsonl(epi / "codex" / CURRENT, [{"id": "ns-current"}])
    _seed_jsonl(epi / "codex" / "AGENT_LEARNINGS.2026-09-01.jsonl", [{"id": "ns-r1"}])
    _seed_jsonl(epi / "snapshots" / "AGENT_LEARNINGS.2026-01-01.jsonl", [{"id": "archived"}])
    (epi / "codex" / "_imported.jsonl").write_text("{}\n")

    paths = [Path(p) for p in consolidate._episodic_paths(str(brain), "default")]
    names = [p.name for p in paths]

    assert all("snapshots" not in p.parts for p in paths), (
        f"archived snapshots must not be consolidated: {paths}"
    )
    assert "_imported.jsonl" not in names

    top = [p.name for p in paths if p.parent == epi]
    assert top == [
        "AGENT_LEARNINGS.2026-09-03.jsonl",
        "AGENT_LEARNINGS.2026-09-04.jsonl",
        CURRENT,
    ], f"top-level order wrong: {top}"

    ns = [p.name for p in paths if p.parent == epi / "codex"]
    assert ns == ["AGENT_LEARNINGS.2026-09-01.jsonl", CURRENT], f"namespace order wrong: {ns}"

    only_ns = [Path(p).name for p in consolidate._episodic_paths(str(brain), "codex")]
    assert only_ns == ["AGENT_LEARNINGS.2026-09-01.jsonl", CURRENT]


# --------------------------------------------------------------------------
# adapters — full-file rewriters that must roll before they rewrite
# --------------------------------------------------------------------------


def _codex_source(root: Path, rollout_rel: str, lines: list[dict]) -> Path:
    """Synthetic ~/.codex/ tree (shape from tests/test_codex_adapter.py)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text("# fake config\n")
    path = root / "sessions" / rollout_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in lines) + "\n")
    return root


def _codex_event(ts: str, text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {"role": "user", "content": text, "type": "message"},
    }


def test_codex_adapter_rolls_oversize_namespace_file(tmp_path, monkeypatch):
    """The codex adapter rewrites the whole namespace file, so it must
    roll an oversize file away first — otherwise every import re-reads and
    re-writes 100+ MB and the file never shrinks."""
    import codex_adapter

    monkeypatch.setattr(_atomic, "ROTATE_BYTES", 1024)
    src = _codex_source(
        tmp_path / "codex-src",
        "2026/09/03/rollout-1.jsonl",
        [_codex_event("2026-09-03T10:00:00Z", "first import " + "x" * 1500)],
    )
    brain = tmp_path / ".agent"
    adapter = codex_adapter.CodexCliAdapter()

    adapter.migrate(src, brain, dry_run=False)
    ns_dir = brain / "memory" / "episodic" / "codex"
    first_pass = (ns_dir / CURRENT).read_bytes()
    first_lines = len(_lines(ns_dir / CURRENT))
    assert first_lines >= 1
    assert len(first_pass) > 1024, "fixture too small to trigger a roll"

    _codex_source(
        src,
        "2026/09/04/rollout-2.jsonl",
        [_codex_event("2026-09-04T10:00:00Z", "second import")],
    )
    adapter.migrate(src, brain, dry_run=False)

    rolls = _rolled_files(ns_dir)
    assert rolls, (
        f"oversize namespace file was not rolled; found "
        f"{[p.name for p in _episodic_glob(ns_dir)]}"
    )
    assert ROLLED_RE.match(rolls[0].name)
    assert rolls[0].read_bytes() == first_pass, "the roll must be the untouched first pass"
    assert len(_lines(ns_dir / CURRENT)) == _total_lines(ns_dir) - first_lines, (
        "the fresh current file must hold only the new import"
    )
    assert _total_lines(ns_dir) > first_lines, "second import produced no episodes"


def _session_events(tool_use_id: str, command: str, ts: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Bash",
                     "input": {"command": command}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": ts,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": "ok", "is_error": False},
                ],
            },
        },
    ]


def _write_session(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_claude_session_adapter_rolls_oversize_and_still_dedups(tmp_path, monkeypatch):
    """Rolling must not break idempotency.

    The adapter rebuilds its `seen` set from the episodic JSONL whenever
    the sidecar is missing. Once history lives in a rolled file, that
    preload has to glob — otherwise every rotation re-imports the whole
    backlog and duplicates it.
    """
    import claude_session_adapter as csa

    source = tmp_path / "src"
    brain = tmp_path / ".agent"
    brain.mkdir()
    session = source / "proj" / "session.jsonl"
    _write_session(session, _session_events("tu_1", "echo one", "2026-09-03T10:00:00Z"))

    argv = ["--source", str(source), "--dst", str(brain)]
    assert csa.main(argv) == 0

    ns_dir = brain / "memory" / "episodic" / "claude-sessions"
    assert len(_lines(ns_dir / CURRENT)) == 1

    # Force the next write to roll, then add a second pair to the source.
    monkeypatch.setattr(_atomic, "ROTATE_BYTES", 64)
    with session.open("a") as f:
        f.write("\n".join(json.dumps(e) for e in
                          _session_events("tu_2", "echo two", "2026-09-04T10:00:00Z")) + "\n")
    assert csa.main(argv) == 0

    assert _rolled_files(ns_dir), (
        f"oversize session file was not rolled; found "
        f"{[p.name for p in _episodic_glob(ns_dir)]}"
    )
    assert _total_lines(ns_dir) == 2

    # Sidecar loss: dedup now depends entirely on reading rolled + current.
    (ns_dir / "_imported.jsonl").unlink()
    assert csa.main(argv) == 0

    tool_ids = []
    for p in _episodic_glob(ns_dir):
        for ln in _lines(p):
            row = json.loads(ln)
            tool_ids.append(row.get("source", {}).get("tool_use_id"))
    assert sorted(tool_ids) == ["tu_1", "tu_2"], (
        f"dedup failed across the roll — episodes now {tool_ids}"
    )
