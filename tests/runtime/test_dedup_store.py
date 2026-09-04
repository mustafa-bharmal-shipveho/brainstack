"""Per-session injected-doc store.

Auto-recall fires on every substantive prompt. Without a memory of what it
already showed, a long session re-injects the same three documents twenty
times: pure context burn, zero new information, and a hit rate that looks
great while telling the user nothing.

`SessionDedupStore` keeps `{doc path -> sha256 of body}` for one session in
one small JSON file under `<log_dir>/injected/`. Keying on the body hash
rather than the path alone is deliberate — a memory the user just edited
must reach the model again.

Failure policy is fail-open everywhere: a corrupt file behaves as empty
(the worst case is one redundant injection, never a lost prompt), and the
write is atomic so a hook killed mid-write cannot leave a half file behind.

Tests cover: record→split round trip, changed-sha re-injection, corrupt
file tolerance, mtime-based pruning, session-id sanitization, atomicity.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest


def _candidate(path: str, body: str):
    """A `RecallCandidate` as `auto_recall.normalize_results` builds them.
    The store reads only `.path` and `.content_sha256`, but constructing
    the real type keeps the two modules' contract pinned together."""
    from runtime.adapters.claude_code.auto_recall import RecallCandidate
    return RecallCandidate(
        path=path,
        source="brain",
        title=Path(path).stem,
        score=0.8,
        rerank_score=None,
        body=body,
        frontmatter={},
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "injected"


def _store(root: Path, session_id: str = "session-abc"):
    from runtime.adapters.claude_code.dedup import SessionDedupStore
    return SessionDedupStore(root, session_id)


class TestRecordAndSplit:

    def test_record_then_split_marks_duplicate(self, store_root: Path):
        store = _store(store_root)
        first = _candidate("/brain/memory/a.md", "body A")
        second = _candidate("/brain/memory/b.md", "body B")

        fresh, duplicates = store.split([first, second])
        assert [c.path for c in fresh] == [
            "/brain/memory/a.md", "/brain/memory/b.md"]
        assert duplicates == []

        store.record([first, second])

        # A brand-new store object reads the same file — the hook is a new
        # process on every prompt, so nothing may live in memory.
        reopened = _store(store_root)
        fresh, duplicates = reopened.split([first, second])
        assert fresh == []
        assert [c.path for c in duplicates] == [
            "/brain/memory/a.md", "/brain/memory/b.md"]

    def test_changed_sha_is_fresh(self, store_root: Path):
        store = _store(store_root)
        store.record([_candidate("/brain/memory/a.md", "version one")])

        edited = _candidate("/brain/memory/a.md", "version two")
        fresh, duplicates = _store(store_root).split([edited])
        assert [c.path for c in fresh] == ["/brain/memory/a.md"]
        assert duplicates == []

    def test_record_merges_across_fires(self, store_root: Path):
        """Each fire records only what IT injected; the file accumulates."""
        _store(store_root).record([_candidate("/brain/memory/a.md", "A")])
        _store(store_root).record([_candidate("/brain/memory/b.md", "B")])

        loaded = _store(store_root).load()
        assert set(loaded) == {"/brain/memory/a.md", "/brain/memory/b.md"}

    def test_sessions_do_not_share_state(self, store_root: Path):
        _store(store_root, "session-one").record(
            [_candidate("/brain/memory/a.md", "A")])
        fresh, duplicates = _store(store_root, "session-two").split(
            [_candidate("/brain/memory/a.md", "A")])
        assert [c.path for c in fresh] == ["/brain/memory/a.md"]
        assert duplicates == []


class TestFailureTolerance:

    def test_corrupt_file_treated_as_empty(self, store_root: Path):
        """A truncated or hand-edited file must not break the prompt. Load
        returns empty, and the next write overwrites the garbage."""
        store = _store(store_root)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json at all", encoding="utf-8")

        assert store.load() == {}
        cand = _candidate("/brain/memory/a.md", "A")
        fresh, duplicates = store.split([cand])
        assert [c.path for c in fresh] == ["/brain/memory/a.md"]
        assert duplicates == []

        store.record([cand])
        data = json.loads(store.path.read_text(encoding="utf-8"))
        assert data["injected"] == {"/brain/memory/a.md": cand.content_sha256}

    def test_missing_file_loads_empty(self, store_root: Path):
        store = _store(store_root)
        assert not store.path.exists()
        assert store.load() == {}

    def test_atomic_write_no_tmp_left(self, store_root: Path):
        """`os.replace` of a sibling tmp file. If the tmp name leaked, the
        injected dir would fill with debris the pruner does not recognize."""
        store = _store(store_root)
        store.record([_candidate("/brain/memory/a.md", "A")])

        names = sorted(p.name for p in store_root.iterdir())
        assert names == [store.path.name], names


class TestSafeId:

    def test_safe_id_sanitizes_session_id(self):
        """Session ids arrive from Claude Code's payload — untrusted enough
        that a `../` in one must not escape the injected dir."""
        from runtime.adapters.claude_code.dedup import _safe_id

        assert _safe_id("abc-123_XY.z") == "abc-123_XY.z"
        assert "/" not in _safe_id("../../etc/passwd")
        assert _safe_id("a/b") == "a_b"
        assert _safe_id("") == "unknown"
        assert len(_safe_id("x" * 400)) <= 128

    def test_store_path_uses_safe_id(self, store_root: Path):
        store = _store(store_root, "sess/../1")
        assert store.path.parent == store_root
        assert store.path.name.endswith(".json")
        assert "/" not in store.path.name


class TestPrune:

    def test_prune_removes_only_older_than_7_days(self, store_root: Path):
        """Sessions are short-lived; their stores are not. Prune keeps the
        dir from growing without bound, but must never delete a store for a
        session that could still be running."""
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        now = time.time()
        for session in ("old", "recent"):
            _store(store_root, session).record(
                [_candidate("/brain/memory/a.md", "A")])

        old_path = _store(store_root, "old").path
        recent_path = _store(store_root, "recent").path
        eight_days = now - 8 * 86400
        six_days = now - 6 * 86400
        os.utime(old_path, (eight_days, eight_days))
        os.utime(recent_path, (six_days, six_days))

        removed = SessionDedupStore.prune(store_root, max_age_days=7, now=now)
        assert removed == 1
        assert not old_path.exists()
        assert recent_path.exists()

    def test_prune_on_missing_dir_is_a_noop(self, tmp_path: Path):
        from runtime.adapters.claude_code.dedup import SessionDedupStore
        assert SessionDedupStore.prune(tmp_path / "nope", now=time.time()) == 0

    def test_prune_throttled_to_once_per_interval(self, store_root: Path, monkeypatch):
        """`prune` runs on EVERY auto-recall fire, and it globs + stats the
        whole shared injected dir. On a busy machine with hundreds of stale
        stores that is real syscall cost paid per prompt for a job that only
        needs doing occasionally. A marker file caps it at once an hour."""
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        now = time.time()
        _store(store_root, "old").record([_candidate("/brain/memory/a.md", "A")])
        old_path = _store(store_root, "old").path
        eight_days = now - 8 * 86400
        os.utime(old_path, (eight_days, eight_days))

        globs: list[str] = []
        real_glob = Path.glob

        def counting_glob(self, pattern, *a, **kw):
            globs.append(pattern)
            return real_glob(self, pattern, *a, **kw)

        monkeypatch.setattr(Path, "glob", counting_glob)

        assert SessionDedupStore.prune(store_root, now=now) == 1
        assert not old_path.exists()
        assert len(globs) == 1, globs

        # Second fire seconds later: no glob, no stat storm, no work.
        assert SessionDedupStore.prune(store_root, now=now + 60) == 0
        assert len(globs) == 1, f"pruned again inside the interval: {globs}"

    def test_prune_runs_again_after_the_interval(self, store_root: Path, monkeypatch):
        """Throttling must not turn into never-pruning: once the interval
        has passed the next fire does the full pass again."""
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        store_root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        SessionDedupStore.prune(store_root, now=now)

        globs: list[str] = []
        real_glob = Path.glob

        def counting_glob(self, pattern, *a, **kw):
            globs.append(pattern)
            return real_glob(self, pattern, *a, **kw)

        monkeypatch.setattr(Path, "glob", counting_glob)

        # Inside the interval: skipped.
        SessionDedupStore.prune(store_root, now=now + 3599)
        assert globs == []

        # A stale store written after the first prune must still be caught.
        _store(store_root, "old").record([_candidate("/brain/memory/a.md", "A")])
        old_path = _store(store_root, "old").path
        eight_days = now - 8 * 86400
        os.utime(old_path, (eight_days, eight_days))

        assert SessionDedupStore.prune(store_root, now=now + 3601) == 1
        assert len(globs) == 1, globs
        assert not old_path.exists()

    def test_prune_marker_is_not_itself_a_store(self, store_root: Path):
        """The marker must not be mistaken for a session store: it is not
        matched by the `*.json` sweep, and it is never counted as removed."""
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        store_root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        assert SessionDedupStore.prune(store_root, now=now) == 0
        marker = store_root / ".last_prune"
        assert marker.exists()

        # Ancient marker, ancient dir: the marker survives its own prune.
        ancient = now - 900 * 86400
        os.utime(marker, (ancient, ancient))
        assert SessionDedupStore.prune(store_root, now=now) == 0
        assert marker.exists()
        assert _store(store_root, "x").load() == {}

    def test_prune_interval_can_be_disabled(self, store_root: Path):
        """`min_interval_s=0` is the escape hatch for callers that want the
        old unconditional behaviour (and for the pinned 7-day tests)."""
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        store_root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        SessionDedupStore.prune(store_root, now=now)
        _store(store_root, "old").record([_candidate("/brain/memory/a.md", "A")])
        old_path = _store(store_root, "old").path
        eight_days = now - 8 * 86400
        os.utime(old_path, (eight_days, eight_days))

        assert SessionDedupStore.prune(
            store_root, now=now, min_interval_s=0) == 1
        assert not old_path.exists()
