"""Staging-only dream cycle. Mechanical work, no reasoning.

Responsibilities (in order):
  1. load episodic entries
  2. cluster + extract → structured patterns
  3. stage candidates (lifecycle metadata baked in)
  4. heuristic prefilter (length + exact-duplicate; obvious junk goes to rejected/)
  5. decay old episodes + archive stale workspace
  6. write REVIEW_QUEUE.md summary so the next host session sees the backlog

Never:
  - subjective validation (host agent reviews via CLI tools)
  - promotion to LESSONS.md (graduate.py does that)
  - git commit (unattended repo writes are dangerous on a host hook)
"""
import contextlib, json, os
from promote import cluster_and_extract, write_candidates
from validate import heuristic_check
from review_state import mark_rejected, write_review_queue_summary
from decay import decay_old_entries
from archive import archive_stale_workspace

# fcntl is POSIX-only. On Windows the dream cycle is best-effort: concurrent
# writers there are rare (no shutdown hook = no parallel exits), and the lack
# of locking matches the existing _episodic_io.py fallback.
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

ROOT = os.path.abspath(os.path.dirname(__file__))
EPISODIC = os.path.join(ROOT, "episodic/AGENT_LEARNINGS.jsonl")
CANDIDATES = os.path.join(ROOT, "candidates")
SEMANTIC = os.path.join(ROOT, "semantic")
REVIEW_QUEUE = os.path.join(ROOT, "working/REVIEW_QUEUE.md")
PROMOTION_THRESHOLD = 7.0
CLUSTER_SIMILARITY = 0.3


EPISODIC_LOCK = EPISODIC + ".lock"


@contextlib.contextmanager
def _episodic_locked():
    """Hold an exclusive flock across the dream-cycle read-modify-write window.

    The lock is taken on a SENTINEL sibling file (`AGENT_LEARNINGS.jsonl.lock`),
    NOT the data file itself. This decouples lock identity from data-file
    inode identity so the atomic rewrite (`os.replace` in
    `_write_entries_locked`) can swap the data file's inode without
    invalidating in-flight appenders' locks. Locking the data file directly
    causes silent data loss because:
      - dream cycle locks data file → appender opens data file, blocks on flock
      - dream cycle calls os.replace(tmp, data) → data file is now a new inode
      - dream cycle releases lock on the (now-orphan) old inode
      - appender's flock acquires on the orphan inode and writes there
      - appender's bytes are unreachable from the path; file is "the new inode"
    With sentinel locking, the appender's open()-then-write happens only
    after sentinel-lock is released, by which point os.replace has completed
    and open() on the data path resolves to the new inode unambiguously.

    Yields the lock file descriptor for callers that need to coordinate
    further (currently only used as a sentinel — readers/writers should
    use `_load_entries_locked()` / `_write_entries_locked()` to do their
    own opens of EPISODIC).
    On Windows (no fcntl) yields None and falls back to best-effort.
    """
    if fcntl is None:
        yield None
        return
    os.makedirs(os.path.dirname(EPISODIC), exist_ok=True)
    fd = os.open(EPISODIC_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_entries_locked(_fd):
    """Read all entries from EPISODIC. The sentinel lock held by the caller
    means no appender will be writing while we read.

    The fd argument is preserved for backward compatibility with the
    pre-sentinel signature; it's now ignored (the sentinel is the lock,
    not the data file).
    """
    entries = []
    if not os.path.exists(EPISODIC):
        return entries
    try:
        with open(EPISODIC) as f:
            stream = f.read()
    except OSError:
        return entries
    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _write_entries_locked(_fd, entries):
    """Atomically rewrite EPISODIC.

    Two safety guarantees:
      - SIGKILL during write: temp+fsync+os.replace means the original file
        is intact until the rename, and the rename is atomic on POSIX/Windows.
      - Concurrent appenders: the sentinel lock held by the dream cycle
        blocks appenders from opening + writing AGENT_LEARNINGS.jsonl until
        we release. After release, appenders' open() lands on the new inode.
    """
    from _atomic import atomic_write_bytes  # local import to avoid module-init cycles
    payload = "".join(json.dumps(e) + "\n" for e in entries).encode("utf-8")
    atomic_write_bytes(EPISODIC, payload)


# Compatibility shims for any external caller that still imports the
# pre-refactor names. Internal callers in run_dream_cycle use the locked
# helpers directly so the lock spans the full cycle.
def _load_entries():
    with _episodic_locked() as fd:
        return _load_entries_locked(fd)


def _write_entries(entries):
    with _episodic_locked() as fd:
        _write_entries_locked(fd, entries)


def _heuristic_prefilter(candidates_dir, semantic_dir):
    """Move obvious junk (too-short, exact duplicate) to rejected/ automatically.

    Anything subjective — "is this really a useful lesson?" — is the host
    agent's call, not this function's.
    """
    if not os.path.isdir(candidates_dir):
        return 0
    lessons_path = os.path.join(semantic_dir, "LESSONS.md")
    existing = open(lessons_path).read() if os.path.exists(lessons_path) else ""
    rejected = 0
    for fname in sorted(os.listdir(candidates_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(candidates_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                cand = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        check = heuristic_check(cand, existing)
        if not check["passed"]:
            reason = ", ".join(check["reasons"])
            # Record the specific lesson(s) that triggered the duplicate
            # rejection so write_candidates can check whether THIS blocker
            # is still there, not just whether LESSONS.md as a whole changed.
            mark_rejected(cand["id"], "heuristic_prefilter", reason,
                          candidates_dir,
                          duplicate_claims=check.get("duplicates", []))
            rejected += 1
    return rejected


def run_dream_cycle():
    # Hold the lock across the FULL read-modify-write window. Any
    # append_jsonl() call from another harness blocks until we release.
    # Without this, an append landing between read and rewrite would be
    # truncated away.
    with _episodic_locked() as fd:
        entries = _load_entries_locked(fd)
        if not entries:
            # Still refresh the review queue — candidates may have been staged
            # in a previous cycle and the host agent loads REVIEW_QUEUE.md
            # into every session via build_context, so a stale/missing file
            # hides real work.
            pending = write_review_queue_summary(CANDIDATES, REVIEW_QUEUE)
            print(f"dream cycle: no entries (queue has {pending} pending)")
            return

        patterns = cluster_and_extract(entries, threshold=CLUSTER_SIMILARITY)
        promotable = {k: p for k, p in patterns.items()
                      if p.get("canonical_salience", 0) >= PROMOTION_THRESHOLD}

        staged = write_candidates(promotable, CANDIDATES)
        prefiltered = _heuristic_prefilter(CANDIDATES, SEMANTIC)

        kept, archived = decay_old_entries(
            entries, archive_dir=os.path.join(ROOT, "episodic/snapshots"))
        _write_entries_locked(fd, kept)
        archive_stale_workspace(
            working_dir=os.path.join(ROOT, "working"),
            archive_dir=os.path.join(ROOT, "episodic/snapshots"))

        pending = write_review_queue_summary(CANDIDATES, REVIEW_QUEUE)

    print(
        f"dream cycle: patterns={len(patterns)} staged={staged} "
        f"prefiltered_out={prefiltered} pending_review={pending} "
        f"archived={len(archived)} kept={len(kept)}"
    )


if __name__ == "__main__":
    run_dream_cycle()
