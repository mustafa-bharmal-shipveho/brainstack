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
import contextlib
import datetime
import json
import os

from archive import archive_stale_workspace
from cluster import _is_activity_log_claim
from decay import DECAY_DAYS, decay_old_entries
from promote import _env_bool, cluster_and_extract, write_candidates
from review_state import mark_rejected, write_review_queue_summary
from validate import heuristic_check

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


_NAMESPACE_RE = __import__("re").compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _resolve_brain_root(brain_root):
    """Brain-root resolution: explicit arg > BRAIN_ROOT env > ~/.agent."""
    if brain_root:
        return os.path.abspath(brain_root)
    env = os.environ.get("BRAIN_ROOT")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.expanduser("~/.agent"))


def _ns_paths(brain_root, namespace):
    """Compute the per-namespace paths used by the dream cycle.

    Backward compat: namespace=='default' uses the v0.1 top-level paths
    (no extra subdir) so existing brains don't need migration.
    """
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = _resolve_brain_root(brain_root)
    memory = os.path.join(root, "memory")
    if namespace == "default":
        episodic = os.path.join(memory, "episodic", "AGENT_LEARNINGS.jsonl")
        candidates = os.path.join(memory, "candidates")
        semantic = os.path.join(memory, "semantic")
        snapshots = os.path.join(memory, "episodic", "snapshots")
        working = os.path.join(memory, "working")
    else:
        episodic = os.path.join(memory, "episodic", namespace, "AGENT_LEARNINGS.jsonl")
        candidates = os.path.join(memory, "candidates", namespace)
        semantic = os.path.join(memory, "semantic", namespace)
        snapshots = os.path.join(memory, "episodic", namespace, "snapshots")
        working = os.path.join(memory, "working", namespace)
    review_queue = os.path.join(working, "REVIEW_QUEUE.md")
    return {
        "memory": memory,
        "episodic": episodic,
        "episodic_lock": episodic + ".lock",
        "candidates": candidates,
        "semantic": semantic,
        "snapshots": snapshots,
        "working": working,
        "review_queue": review_queue,
    }


EPISODIC_LOCK = EPISODIC + ".lock"


@contextlib.contextmanager
def _episodic_locked_path(episodic_path):
    """Sentinel-locked context for an arbitrary episodic JSONL path.

    Same locking semantics as `_episodic_locked()` but parameterized so
    namespaced runs can use it without mutating module-level state.
    """
    if fcntl is None:
        yield None
        return
    os.makedirs(os.path.dirname(episodic_path), exist_ok=True)
    sentinel = episodic_path + ".lock"
    fd = os.open(sentinel, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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


def _read_jsonl(path):
    """Parse one JSONL file, skipping blank and corrupt lines."""
    rows = []
    try:
        with open(path) as f:
            stream = f.read()
    except OSError:
        return rows
    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_entries_locked_path(_fd, episodic_path):
    """Read entries from an episodic stream — rolled files AND the current
    file (lock held by caller).

    Rotation moves history into `AGENT_LEARNINGS.<day>.jsonl` siblings, so
    a current-file-only read would silently shrink the clustering input to
    whatever landed since the last roll.

    Each entry carries a `_src` tag naming the file it came from. Rolled
    files are immutable, so only entries tagged with the current file may
    be written back; `_split_by_source` strips the tag before anything
    downstream (clustering, decay, candidates) can persist it.
    """
    from _atomic import episodic_files  # local import — module-init cycle

    entries = []
    for path in episodic_files(__import__("pathlib").Path(episodic_path)):
        if not path.is_file():
            continue
        tag = os.path.abspath(str(path))
        for row in _read_jsonl(path):
            if isinstance(row, dict):
                row["_src"] = tag
            entries.append(row)
    return entries


def _split_by_source(entries, episodic_path):
    """Strip the `_src` provenance tag; return (all_entries, current_only).

    Both lists hold the SAME entry objects, so `decay_old_entries` results
    over `current_only` stay identity-comparable with `all_entries`.

    Why the split matters: decay archives individual expired entries into
    `snapshots/`, but a rolled file is only ever archived WHOLE (by
    `_archive_expired_rolls`). Letting decay see rolled entries would copy
    them into snapshots on every nightly run while the rolled file kept
    them too — the same rows re-archived forever.
    """
    target = os.path.abspath(episodic_path)
    all_entries, current_only = [], []
    for entry in entries:
        is_current = True
        if isinstance(entry, dict) and "_src" in entry:
            is_current = entry.get("_src") == target
            entry = {k: v for k, v in entry.items() if k != "_src"}
        all_entries.append(entry)
        if is_current:
            current_only.append(entry)
    return all_entries, current_only


def _write_entries_locked_path(_fd, entries, episodic_path):
    """Atomically rewrite the CURRENT file of an episodic stream.

    Rolled siblings are immutable, so any entry still carrying a `_src` tag
    from another file is dropped rather than folded into the current file.
    """
    from _atomic import atomic_write_bytes  # local import — module-init cycle
    target = os.path.abspath(episodic_path)
    rows = []
    for entry in entries:
        if isinstance(entry, dict) and "_src" in entry:
            if entry.get("_src") != target:
                continue
            entry = {k: v for k, v in entry.items() if k != "_src"}
        rows.append(entry)
    payload = "".join(json.dumps(e) + "\n" for e in rows).encode("utf-8")
    atomic_write_bytes(episodic_path, payload)


def _load_entries_locked(_fd):
    """Read all entries from EPISODIC. The sentinel lock held by the caller
    means no appender will be writing while we read.

    The fd argument is preserved for backward compatibility with the
    pre-sentinel signature; it's now ignored (the sentinel is the lock,
    not the data file).

    Spans rolled files as well as the current one — see
    `_load_entries_locked_path` for the `_src` tagging that keeps the
    rewrite scoped to the current file.
    """
    return _load_entries_locked_path(_fd, EPISODIC)


def _write_entries_locked(_fd, entries):
    """Atomically rewrite EPISODIC.

    Two safety guarantees:
      - SIGKILL during write: temp+fsync+os.replace means the original file
        is intact until the rename, and the rename is atomic on POSIX/Windows.
      - Concurrent appenders: the sentinel lock held by the dream cycle
        blocks appenders from opening + writing AGENT_LEARNINGS.jsonl until
        we release. After release, appenders' open() lands on the new inode.

    Only the CURRENT file is rewritten; rolled siblings are immutable.
    """
    _write_entries_locked_path(_fd, entries, EPISODIC)


# Compatibility shims for any external caller that still imports the
# pre-refactor names. Internal callers in run_dream_cycle use the locked
# helpers directly so the lock spans the full cycle.
def _load_entries():
    with _episodic_locked() as fd:
        return _load_entries_locked(fd)


def _write_entries(entries):
    with _episodic_locked() as fd:
        _write_entries_locked(fd, entries)


def _sweep_activity_log_residue(candidates_dir):
    """Retroactively reject already-staged candidates whose claim matches
    the activity-log filter from cluster.py.

    The activity-log filter in promote.cluster_and_extract drops noise at
    staging time, but candidates staged BEFORE the filter shipped (PR #28,
    2026-05-06) or by a pre-filter dream run sit on disk forever — every
    subsequent dream run just bumps their `staged` timestamp without
    re-checking. This sweep closes that gap by running the same regex
    against every still-staged *.json on each dream run.

    DREAM_ACTIVITY_LOG_DISABLED=1 bypasses the filter so a forensic-mode
    run leaves both pipes untouched.

    Returns a list of {"id", "reason", "claim_prefix"} for telemetry.
    """
    swept = []
    if _env_bool("DREAM_ACTIVITY_LOG_DISABLED", False):
        return swept
    if not os.path.isdir(candidates_dir):
        return swept
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
        if cand.get("status") != "staged":
            continue
        is_activity, reason = _is_activity_log_claim(cand.get("claim"))
        if not is_activity:
            continue
        # _is_activity_log_claim returns e.g. "activity_log:edited" — drop
        # the outer namespace so the swept reason reads cleanly as
        # `activity_log_sweep:edited`, not `activity_log_sweep:activity_log:edited`.
        shape = reason.split(":", 1)[1] if ":" in reason else reason
        sweep_reason = f"activity_log_sweep:{shape}"
        try:
            mark_rejected(
                cand["id"], "auto_dream_sweep",
                sweep_reason, candidates_dir,
            )
        except (FileNotFoundError, KeyError):
            continue
        swept.append({
            "id": cand.get("id"),
            "reason": sweep_reason,
            "claim_prefix": (cand.get("claim") or "")[:80],
        })
    return swept


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


def _refresh_pending_summary(brain_root=None):
    """Best-effort regeneration of <brain>/PENDING_REVIEW.md after the
    dream cycle changes the candidate set. Called at the end of both
    run_dream_cycle() and run(). NEVER raises — any failure is swallowed
    so it can't break the dream cycle.

    Wired here (vs only in sync.sh) so the user's surfaces (Claude Code
    SessionStart hook, Cursor rules, shell banner) reflect the new
    candidate set within seconds of dreaming, not on the next hourly tick.
    """
    try:
        if brain_root is None:
            brain_root = _resolve_brain_root(None)
        # Lazy import — render_pending_summary lives in tools/, not memory/.
        # Path insertion is the same pattern dream_runner.py uses.
        import sys as _sys
        tools_dir = os.path.join(brain_root, "tools")
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        try:
            import render_pending_summary  # type: ignore
            render_pending_summary.render(__import__("pathlib").Path(brain_root))
        except Exception:
            pass  # never fail the dream cycle on render failure
    except Exception:
        pass


def _newest_entry_ts(path):
    """Newest parseable `timestamp` in a JSONL file, as an aware UTC
    datetime. `None` when the file holds no parseable timestamp — which is
    treated as "do not archive", the conservative choice."""
    newest = None
    for row in _read_jsonl(path):
        if not isinstance(row, dict):
            continue
        try:
            ts = datetime.datetime.fromisoformat(row.get("timestamp", ""))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        if newest is None or ts > newest:
            newest = ts
    return newest


def _archive_expired_rolls(episodic_path, snapshots_dir, now=None) -> int:
    """Move a rolled sibling of `episodic_path` into `snapshots_dir` once
    EVERY entry inside it is older than `decay.DECAY_DAYS`. Returns the
    count archived.

    This is what bounds disk for namespaces with no registered clusterer
    (codex, claude-sessions, digests): they never run decay, so without a
    whole-file sweep their rolled files accumulate forever. The whole-file
    granularity is deliberate — a rolled file is immutable, so it is
    archived intact or left alone, never partially rewritten.

    A roll with no parseable timestamps is left in place: an unreadable
    file is not evidence that its contents expired.
    """
    from _atomic import episodic_files  # local import — module-init cycle
    from pathlib import Path

    current = Path(episodic_path)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=DECAY_DAYS)

    archived = 0
    for roll in episodic_files(current):
        if roll.name == current.name or not roll.is_file():
            continue  # the current file is live, never archived here
        newest = _newest_entry_ts(roll)
        if newest is None or newest >= cutoff:
            continue
        try:
            os.makedirs(snapshots_dir, exist_ok=True)
            dest = os.path.join(snapshots_dir, roll.name)
            # Namespaces roll on the same calendar day, so keep a distinct
            # name rather than clobbering an existing archive.
            stem, suffix = os.path.splitext(roll.name)
            counter = 0
            while os.path.exists(dest):
                counter += 1
                dest = os.path.join(snapshots_dir, f"{stem}.{counter}{suffix}")
            os.replace(str(roll), dest)
            archived += 1
        except OSError:
            continue  # best-effort — never break the cycle on a disk error
    return archived


def _archive_rolls_all_namespaces(episodic_root, now=None) -> int:
    """Sweep expired rolls for the default namespace AND every
    `memory/episodic/<ns>/` dir, archiving into that namespace's own
    `snapshots/` so two namespaces rolling on the same day cannot collide.

    Runs for namespaces with no registered clusterer on purpose — that is
    the whole point (see `_archive_expired_rolls`).

    LOCKING. Each non-default namespace is swept while holding ITS OWN
    `<AGENT_LEARNINGS.jsonl>.lock` sentinel, because the sweep moves that
    namespace's files and its adapters read exactly those files under the
    same sentinel: `codex_adapter` and `claude_session_adapter` glob
    `AGENT_LEARNINGS*.jsonl` to preload their dedup set. A roll vanishing
    mid-preload silently shrinks that set and the next import duplicates
    the history it covered. The default namespace's sentinel is
    DELIBERATELY not re-acquired here: `run_dream_cycle` holds it for the
    whole read-modify-write window, and flock is per open file
    description, so a second acquire from the same process would deadlock.
    """
    total = 0
    default_current = os.path.join(episodic_root, "AGENT_LEARNINGS.jsonl")
    total += _archive_expired_rolls(
        default_current, os.path.join(episodic_root, "snapshots"), now=now)
    try:
        names = sorted(os.listdir(episodic_root))
    except OSError:
        return total
    for name in names:
        ns_dir = os.path.join(episodic_root, name)
        if name == "snapshots" or not os.path.isdir(ns_dir):
            continue
        ns_current = os.path.join(ns_dir, "AGENT_LEARNINGS.jsonl")
        try:
            with _episodic_locked_path(ns_current):
                total += _archive_expired_rolls(
                    ns_current,
                    os.path.join(ns_dir, "snapshots"),
                    now=now,
                )
        except OSError:
            # Cannot open that namespace's sentinel (read-only dir, fd
            # exhaustion). Skip it rather than sweep it unlocked — the
            # cost is bounded disk, the alternative is a duplicated
            # import. Best-effort, like the archive itself.
            continue
    return total


# The counters the status file records, and the token each is printed
# under — used only when reading them back out of `dream.log` text.
_SUMMARY_INT_FIELDS = {
    "staged": "staged",
    "kept": "kept",
    "archived": "archived",
    "consolidate_claims": "consolidate_claims",
    "llm_calls": "llm_calls",
}
_LLM_ERRORS_RE = __import__("re").compile(r"llm_errors=([\w=,]+)")


def _cycle_counters(*, staged=0, kept=0, archived=0, consolidate_claims=0,
                    llm_calls=0, llm_errors=None):
    """The status file's counter block, built from numbers the cycle
    already holds. Both entry points tally these as they go, so nothing
    has to be recovered from the text of the line they printed."""
    return {
        "staged": int(staged),
        "kept": int(kept),
        "archived": int(archived),
        "consolidate_claims": int(consolidate_claims),
        "llm_calls": int(llm_calls),
        "llm_errors": {str(k): int(v) for k, v in (llm_errors or {}).items()},
    }


def _collect_llm_counters(extractors):
    """Sum `llm_calls` / `llm_errors` across every extractor in play.

    Walks the same chain as the summary-line rendering below (Hybrid →
    its `primary` / `fallback`) and asks each for `error_counters()` —
    the structured twin of `error_summary()`. An extractor that predates
    the accessor simply contributes nothing.
    """
    calls = 0
    errors = {}
    for ex in extractors:
        for candidate in (ex, getattr(ex, "fallback", None),
                          getattr(ex, "primary", None)):
            if candidate is None:
                continue
            fn = getattr(candidate, "error_counters", None)
            if not callable(fn):
                continue
            data = fn() or {}
            calls += int(data.get("llm_calls") or 0)
            for tag, count in (data.get("llm_errors") or {}).items():
                errors[tag] = errors.get(tag, 0) + int(count)
    return calls, errors


def _parse_summary_counters(summary_line):
    """Recover the counter block from a printed `dream cycle:` line.

    Fallback only: the live cycle passes its own tally to
    `_write_cycle_status`. This exists for the one caller that has
    nothing but the text — a `dream.log` line read back after the fact.
    """
    import re as _re

    out = {key: 0 for key in _SUMMARY_INT_FIELDS.values()}
    for token, field in _SUMMARY_INT_FIELDS.items():
        match = _re.search(rf"\b{token}=(-?\d+)", summary_line or "")
        if match:
            out[field] = int(match.group(1))
    errors = {}
    match = _LLM_ERRORS_RE.search(summary_line or "")
    if match:
        for pair in match.group(1).split(","):
            tag, _, count = pair.partition("=")
            if not tag:
                continue
            try:
                errors[tag] = int(count)
            except ValueError:
                continue
    out["llm_errors"] = errors
    return out


def _write_cycle_status(brain_root, namespace, summary_line, *,
                        ok=True, error=None, counters=None) -> None:
    """Write `runtime/dream_status.json` — the cycle's machine-readable
    receipt (schema in plans/guards.md).

    Before this existed the only trace of a cycle was an untimestamped line
    in `dream.log`, which is why a month of runs failing with
    `llm_errors=provider_unavailable=3` went unnoticed: nothing could tell
    "ran an hour ago, clean" from "last ran in July". The health check
    reads this file for both freshness and LLM-error state.

    `counters` is the cycle's own tally (see `_cycle_counters`). Both
    entry points hold those numbers already, so they hand them over
    rather than round-tripping them through the printed line; only a
    caller that has nothing but `dream.log` text leaves it None and gets
    the regex fallback. Never raises — a status-write failure must not
    fail the cycle it is reporting on.
    """
    try:
        from _atomic import atomic_write_json  # local import — module-init cycle

        payload = {
            "schema_version": 1,
            "ts": datetime.datetime.now(datetime.timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "namespace": namespace,
            "ok": bool(ok),
            "summary": summary_line or "",
            "error": error,
        }
        payload.update(
            _cycle_counters(**counters) if counters is not None
            else _parse_summary_counters(summary_line)
        )
        root = _resolve_brain_root(brain_root)
        atomic_write_json(os.path.join(root, "runtime", "dream_status.json"), payload)
    except Exception:  # pragma: no cover — best-effort receipt
        pass


_LINT_SUBDIRS = ("memory", "imports")
_LINT_TIMEOUT_S = 600


def _lint_step_inprocess(brain_root):
    """Run `recall lint --mark` in this interpreter. Raises on any problem
    so `_lint_step` can fall back or report."""
    from pathlib import Path

    from recall.lint import (
        find_flagged_files,
        lint_dirs,
        mark_needs_review,
        unmark_needs_review,
    )

    root = Path(brain_root)
    findings = lint_dirs(root, subdirs=_LINT_SUBDIRS)
    with_findings = {Path(f.file).resolve() for f in findings}
    marked = mark_needs_review(sorted(with_findings))

    # Auto-clear: a memory that was flagged before but lints clean now
    # (the dead path came back, the wikilink target was created) loses the
    # flag, so `needs_review` tracks current state instead of accreting.
    scoped = [root / sub for sub in _LINT_SUBDIRS]
    stale_flags = [
        f for f in find_flagged_files(root)
        if f.resolve() not in with_findings
        and any(_is_within(f, d) for d in scoped)
    ]
    cleared = unmark_needs_review(stale_flags)

    return (f" lint_findings={len(findings)} lint_files={len(with_findings)} "
            f"lint_marked={len(marked)} lint_cleared={len(cleared)}")


def _is_within(path, directory) -> bool:
    """True if `path` is inside `directory` (both may be relative)."""
    try:
        path.resolve().relative_to(directory.resolve())
    except (ValueError, OSError):
        return False
    return True


def _lint_step_subprocess(brain_root):
    """Run the lint through the pinned install's venv.

    The launchd job runs the install-time `python3`, not the repo venv, so
    `import recall` can fail there. `<brain>/.brainstack-repo-path` pins
    the install root precisely so this fallback has an interpreter that
    can. Raises on any problem so `_lint_step` can report it.

    Exit code 1 is SUCCESS here. `recall lint` deliberately exits 1 when
    residual findings remain (recall/cli.py: `raise typer.Exit(code=1)`),
    having already written the findings JSON to stdout — it is a CI
    signal, not a crash. Treating it as failure reported `lint_error=` on
    exactly the nights the brain had something stale, i.e. every night
    the step was doing its job. Only rc >= 2, stdout that is empty or not
    JSON, or a timeout is a real error.
    """
    import subprocess

    pin = os.path.join(brain_root, ".brainstack-repo-path")
    with open(pin) as f:
        repo_root = f.read().strip()
    if not repo_root:
        raise RuntimeError(".brainstack-repo-path is empty")
    python = os.path.join(repo_root, ".venv", "bin", "python")
    proc = subprocess.run(
        [python, "-m", "recall.cli", "lint", "--mark", "--json",
         "--brain", str(brain_root)],
        capture_output=True, text=True, timeout=_LINT_TIMEOUT_S,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"recall lint exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"recall lint exited {proc.returncode} with empty stdout: "
            f"{proc.stderr.strip()[:200]}")
    try:
        findings = json.loads(stdout)
    except ValueError as exc:
        # rc=1 is only benign when the JSON contract held. A traceback
        # that happens to exit 1 must not be counted as zero findings.
        raise RuntimeError(
            f"recall lint exited {proc.returncode} with unparseable stdout "
            f"({exc}): {stdout[:200]}") from exc
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    return f" lint_findings={len(findings)} lint_via=subprocess"


def _lint_step(brain_root) -> str:
    """Run `recall lint --mark` over `memory/` and `imports/`, returning
    the fields to append to the `dream cycle:` line.

    `recall lint --mark` is the only mechanism that demotes a stale memory,
    and nothing else runs it on a schedule — folding it in here makes
    staleness a background property of the brain instead of a chore.

    Both dirs are linted in ONE pass so known wikilink keys are computed
    brain-wide: a plan under `imports/` linking `[[a-lesson]]` in `memory/`
    is a live link, and linting the trees separately would report it broken.

    Never raises. Lint matters less than consolidation, decay and the
    review queue, so every failure degrades to a ` lint_error=` field.
    `BRAINSTACK_DREAM_LINT=0` disables the step entirely. The leading space
    belongs to the returned string — it is concatenated onto the summary.
    """
    if os.environ.get("BRAINSTACK_DREAM_LINT", "").strip() == "0":
        return ""
    try:
        try:
            return _lint_step_inprocess(brain_root)
        except (ImportError, NotImplementedError):
            # This interpreter cannot run the lint (no `recall` on the
            # path, or a build where `lint_dirs` is not wired up yet).
            return _lint_step_subprocess(brain_root)
    except Exception as exc:
        return f" lint_error={exc!r}"


def run_dream_cycle():
    # Hold the lock across the FULL read-modify-write window. Any
    # append_jsonl() call from another harness blocks until we release.
    # Without this, an append landing between read and rewrite would be
    # truncated away.
    with _episodic_locked() as fd:
        # Bound disk before reading: a rolled file whose every entry has
        # expired moves to snapshots/ wholesale, for this namespace and
        # every unregistered one. Doing it first also keeps expired rolls
        # out of clustering and out of decay's per-entry archive.
        _archive_rolls_all_namespaces(os.path.join(ROOT, "episodic"))

        tagged = _load_entries_locked(fd)
        if not tagged:
            # Still refresh the review queue — candidates may have been staged
            # in a previous cycle and the host agent loads REVIEW_QUEUE.md
            # into every session via build_context, so a stale/missing file
            # hides real work.
            pending = write_review_queue_summary(CANDIDATES, REVIEW_QUEUE)
            summary = f"dream cycle: no entries (queue has {pending} pending)"
            print(summary)
            _write_cycle_status(None, "default", summary, counters={})
            _refresh_pending_summary()
            return

        # Clustering sees rolled history; decay and the rewrite see only
        # the current file, because rolled files are immutable.
        entries, current_entries = _split_by_source(tagged, EPISODIC)

        burst_telemetry = []
        activity_log_telemetry = []
        patterns = cluster_and_extract(
            entries, threshold=CLUSTER_SIMILARITY,
            telemetry=burst_telemetry,
            activity_log_telemetry=activity_log_telemetry,
        )
        promotable = {k: p for k, p in patterns.items()
                      if p.get("canonical_salience", 0) >= PROMOTION_THRESHOLD}

        staged = write_candidates(promotable, CANDIDATES)
        swept = _sweep_activity_log_residue(CANDIDATES)
        prefiltered = _heuristic_prefilter(CANDIDATES, SEMANTIC)

        kept, archived = decay_old_entries(
            current_entries, archive_dir=os.path.join(ROOT, "episodic/snapshots"))
        _write_entries_locked(fd, kept)
        archive_stale_workspace(
            working_dir=os.path.join(ROOT, "working"),
            archive_dir=os.path.join(ROOT, "episodic/snapshots"))

        pending = write_review_queue_summary(CANDIDATES, REVIEW_QUEUE)

    # Consolidate observation-shaped episodic events into the claim
    # store (default namespace). Best-effort: any failure here logs +
    # continues — must NOT break the nightly dream cycle. The claim
    # consolidator acquires its own claims-log sentinel lock independently
    # of the episodic lock.
    consolidate_summary = ""
    claims_asserted = 0
    llm_calls = 0
    llm_errors = {}
    try:
        import consolidate  # local import — avoids cycles at module init
        import topic_keys
        resolved_brain = _resolve_brain_root(None)
        extractors = topic_keys.default_extractors(
            brain_root=resolved_brain, namespace="default",
        )
        cresult = consolidate.run_consolidation(
            resolved_brain,
            namespace="default",
            extractors=extractors,
        )
        claims_asserted = cresult.claims_asserted
        consolidate_summary = (
            f" consolidate_events={cresult.events_conforming} "
            f"consolidate_claims={cresult.claims_asserted} "
            f"consolidate_supersedes={cresult.supersedes_appended} "
            f"consolidate_retracts={cresult.retracts_appended} "
            f"projected={cresult.projection_written}"
        )
        # Surface any LLM-call errors so they appear in dream.log
        # instead of being silently swallowed. Walks every extractor
        # (Hybrid → fallback is the LLMExtractor) and asks for its
        # error_summary if it has one.
        for ex in extractors:
            for candidate in (ex, getattr(ex, "fallback", None),
                              getattr(ex, "primary", None)):
                if candidate is None:
                    continue
                summary_fn = getattr(candidate, "error_summary", None)
                if callable(summary_fn):
                    s = summary_fn()
                    if s:
                        consolidate_summary += " " + s
        llm_calls, llm_errors = _collect_llm_counters(extractors)
    except Exception as exc:  # pragma: no cover — best-effort
        consolidate_summary = f" consolidate_error={exc!r}"

    lint_summary = _lint_step(_resolve_brain_root(None))

    summary = (
        f"dream cycle: patterns={len(patterns)} staged={staged} "
        f"prefiltered_out={prefiltered} pending_review={pending} "
        f"archived={len(archived)} kept={len(kept)} "
        f"burst_skipped={len(burst_telemetry)} "
        f"activity_log_skipped={len(activity_log_telemetry)} "
        f"activity_log_swept={len(swept)}"
        f"{consolidate_summary}"
        f"{lint_summary}"
    )
    print(summary)
    _write_cycle_status(None, "default", summary, counters={
        "staged": staged,
        "kept": len(kept),
        "archived": len(archived),
        "consolidate_claims": claims_asserted,
        "llm_calls": llm_calls,
        "llm_errors": llm_errors,
    })
    _refresh_pending_summary()


def run(brain_root=None, namespace="default", dry_run=False):
    """Namespaced dream cycle. v0.2 entry point.

    Resolves paths under the given namespace (with backward-compat for
    "default"), runs cluster-extract-stage-prefilter-decay-archive, and
    returns a structured result dict.

    dry_run=True skips writes (useful for diagnostics) — but still reports
    what would be written.
    """
    paths = _ns_paths(brain_root, namespace)
    episodic = paths["episodic"]
    candidates_dir = paths["candidates"]
    semantic_dir = paths["semantic"]
    snapshots_dir = paths["snapshots"]
    working_dir = paths["working"]
    review_queue = paths["review_queue"]

    os.makedirs(os.path.dirname(episodic), exist_ok=True)
    os.makedirs(candidates_dir, exist_ok=True)
    os.makedirs(semantic_dir, exist_ok=True)
    os.makedirs(working_dir, exist_ok=True)

    result = {
        "namespace": namespace,
        "candidates_written": 0,
        "rejected": 0,
        "decayed": 0,
    }

    with _episodic_locked_path(episodic) as fd:
        if not dry_run:
            _archive_expired_rolls(episodic, snapshots_dir)
        tagged = _load_entries_locked_path(fd, episodic)
        entries, current_entries = _split_by_source(tagged, episodic)
        if not entries:
            if not dry_run:
                from review_state import write_review_queue_summary
                try:
                    write_review_queue_summary(candidates_dir, review_queue)
                except Exception:
                    pass
            # Even on the empty-entries early-return path, refresh
            # PENDING_REVIEW.md — there may be staged candidates from a
            # prior cycle (still pending review) whose drift/sync state
            # changed. Without this the surfaces stay stale until the
            # next hourly sync (Codex 2026-05-04 P2).
            if not dry_run:
                _write_cycle_status(
                    brain_root, namespace, "dream cycle: no entries",
                    counters={})
                _refresh_pending_summary(brain_root)
            return result

        burst_telemetry = []
        activity_log_telemetry = []
        patterns = cluster_and_extract(
            entries, threshold=CLUSTER_SIMILARITY,
            telemetry=burst_telemetry,
            activity_log_telemetry=activity_log_telemetry,
        )
        promotable = {k: p for k, p in patterns.items()
                      if p.get("canonical_salience", 0) >= PROMOTION_THRESHOLD}

        if dry_run:
            result["candidates_written"] = len(promotable)
            result["burst_skipped"] = len(burst_telemetry)
            result["activity_log_skipped"] = len(activity_log_telemetry)
            return result

        staged = write_candidates(promotable, candidates_dir)
        swept = _sweep_activity_log_residue(candidates_dir)
        prefiltered = _heuristic_prefilter(candidates_dir, semantic_dir)

        # Decay and the rewrite see only the current file — rolled files
        # are immutable and are archived whole by `_archive_expired_rolls`.
        kept, archived = decay_old_entries(current_entries,
                                           archive_dir=snapshots_dir)
        _write_entries_locked_path(fd, kept, episodic)
        archive_stale_workspace(working_dir=working_dir,
                                archive_dir=snapshots_dir)

        from review_state import write_review_queue_summary
        try:
            write_review_queue_summary(candidates_dir, review_queue)
        except Exception:
            pass

        result["candidates_written"] = staged
        result["rejected"] = prefiltered
        result["decayed"] = len(archived)
        result["burst_skipped"] = len(burst_telemetry)
        result["activity_log_skipped"] = len(activity_log_telemetry)
        result["activity_log_swept"] = len(swept)

    # Consolidate observation-shaped episodic events into the claim
    # store. Best-effort: an extraction error here must NOT break the
    # nightly dream cycle.
    try:
        import consolidate  # local import — avoids cycles at module init
        import topic_keys
        resolved_brain = _resolve_brain_root(brain_root)
        cresult = consolidate.run_consolidation(
            resolved_brain,
            namespace=namespace,
            extractors=topic_keys.default_extractors(
                brain_root=resolved_brain, namespace=namespace,
            ),
        )
        result["consolidate_events"] = cresult.events_conforming
        result["consolidate_claims"] = cresult.claims_asserted
        result["consolidate_supersedes"] = cresult.supersedes_appended
        result["consolidate_retracts"] = cresult.retracts_appended
        result["consolidate_projection_written"] = cresult.projection_written
    except Exception as exc:  # pragma: no cover — best-effort
        result["consolidate_error"] = str(exc)

    result["lint_summary"] = _lint_step(_resolve_brain_root(brain_root))

    # Same shape as the `dream cycle:` line run_dream_cycle() prints, so
    # a human reading either surface sees one format regardless of which
    # entry point ran. Not printed — run() returns its result instead of
    # logging. The counters come from `result`, not from this text.
    _write_cycle_status(brain_root, namespace, (
        f"dream cycle: staged={result['candidates_written']} "
        f"kept={len(kept)} archived={len(archived)} "
        f"consolidate_claims={result.get('consolidate_claims', 0)}"
        f"{result['lint_summary']}"
    ), counters={
        "staged": result["candidates_written"],
        "kept": len(kept),
        "archived": len(archived),
        "consolidate_claims": result.get("consolidate_claims", 0),
    })
    _refresh_pending_summary(brain_root)
    return result


if __name__ == "__main__":
    run_dream_cycle()
