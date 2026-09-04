#!/usr/bin/env python3
"""Generates ~/.agent/PENDING_REVIEW.md — the single source of truth for
attention-grabbing items the user should triage:

  - pending candidate lessons in each namespace (default, claude-sessions, codex)
  - drift between brainstack repo and the brain (via check_freshness)
  - sync staleness (sync.log mtime + last-line "refusing to push")

The output file is consumed by three surfaces:
  - Claude Code SessionStart hook (agent/harness/hooks/session_start.py)
  - Cursor .cursorrules (agent/tools/render_cursor_rules.py)
  - Shell wrapper functions (templates/brainstack-shell-banner.sh)

Why this exists: 2026-05-04 audit found 21 candidates pending since 2026-05-01-02.
brainstack writes them silently and nothing surfaces the count. This file fixes
the consume-side gap.

CLI
---
    render_pending_summary.py [--brain DIR] [--print-only]

  --brain DIR     Brain root (default: $BRAIN_ROOT or ~/.agent)
  --print-only    Print summary to stdout, do NOT write the file
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "memory"))

from _atomic import atomic_write_text  # noqa: E402


# ---------- noise filter ----------------------------------------------

# Paths/substrings indicating brainstack's own test-suite or sandbox runs.
# Codex 2026-05-04 caught the 5,700-cluster of "FAILURE: secret op" from
# /tmp/sysadmin-test-home/.agent dominating the queue. Strict filter so the
# top of the user's review list is real signal.
_NOISE_PATH_PREFIXES = (
    "/tmp/",
    "/var/folders/",
    "/private/tmp/",
    "/private/var/folders/",
)
_NOISE_SUBSTRINGS = (
    "-test-",
    "-smoke-",
    "/sandbox/",
    "/sandbox-",
    "-sandbox/",
    "-sandbox-",
    "/test-fixtures/",
    "test_home/",
    "-test/",
    "-smoke/",
    # brainstack's own redaction-test loop emits "FAILURE in claude-code:
    # High-stakes op FAILED (secret): ..." for every TruffleHog test case.
    # On the maintainer's brain this produced a 5,700-cluster dominating
    # the queue — pure test infra, not a real lesson.
    "FAILED (secret)",
    "Detected potential secret",
)


def _is_noise_evidence(eid: str) -> bool:
    """One evidence_id (or claim/conditions blob) is noise if it contains
    a tmp/sandbox path prefix OR a test/smoke/sandbox substring. Bare ISO
    timestamps are never noise (they're the live-hook capture format).

    Codex 2026-05-05 P2: the path-prefix check used to be `startswith()`,
    which missed claims like "Command failed: cd /tmp/brainstack-run"
    (path is mid-string, not at the start). Switched to substring match
    so embedded tmp paths in claim text are caught. The user-facing
    consequence is that test-fixture clusters with /tmp/ in the claim
    string are now filtered from the top-5 review list."""
    if not isinstance(eid, str):
        return False
    if any(prefix in eid for prefix in _NOISE_PATH_PREFIXES):
        return True
    if any(sub in eid for sub in _NOISE_SUBSTRINGS):
        return True
    return False


def _is_noise_cluster(candidate: dict) -> bool:
    """A cluster is noise iff:

      (a) the `claim` field references a tmp/sandbox/test path
          (this catches the most common case — claims like
          "Command failed: BRAIN_ROOT=/tmp/sysadmin-test-home/...",
          "SANDBOX=/tmp/brainstack-cursor-smoke-$$", etc.); OR
      (b) every evidence_id is a noise path (legacy fallback for
          older candidate dicts where evidence ids carried paths
          instead of timestamps).

    Codex 2026-05-04 originally found this filter gap: candidates' real
    content lives in `claim` (and `conditions`), not in `evidence_ids`
    (which are usually bare ISO timestamps). Filtering on evidence_ids
    alone misses 100% of test-infra clusters in real brains.
    """
    # (a) claim-based check — strongest signal
    claim = str(candidate.get("claim") or "")
    conditions = " ".join(str(c) for c in (candidate.get("conditions") or []) if c)
    blob = claim + " " + conditions
    if _is_noise_evidence(blob):  # reuses the same path-pattern matcher
        return True

    # (b) evidence_id fallback for legacy schemas
    eids = candidate.get("evidence_ids", []) or []
    if not eids:
        return False
    # Path-shaped evidence (not bare ISO timestamps) — apply the all-noise rule
    path_eids = [e for e in eids if isinstance(e, str) and ("/" in e)]
    if path_eids:
        return all(_is_noise_evidence(e) for e in path_eids)
    return False


# ---------- counting --------------------------------------------------


def _review_state():
    """Lazy import — avoids circular import at module load."""
    import review_state  # noqa: WPS433
    return review_state


def count_pending_per_namespace(brain_root: Path) -> dict[str, int]:
    """Count staged candidates in each namespace's candidates dir.

    Delegates to `review_state.list_candidates(dir, status="staged")` —
    the SHARED definition consumed by the triage REPL
    (`triage_candidates.py`) and REVIEW_QUEUE.md
    (`write_review_queue_summary`). Going through a single function means
    these three surfaces CAN'T drift: if `list_candidates` says "1
    staged", banner and triage and review-queue all agree.

    Namespaces tracked:
      - default          → <brain>/memory/candidates/*.json
      - claude-sessions  → <brain>/memory/candidates/claude-sessions/*.json
      - codex            → <brain>/memory/candidates/codex/*.json
    """
    rs = _review_state()
    counts = {"default": 0, "claude-sessions": 0, "codex": 0}
    candidates_root = brain_root / "memory" / "candidates"
    if not candidates_root.is_dir():
        return counts
    counts["default"] = len(rs.list_candidates(str(candidates_root), status="staged"))
    for ns in ("claude-sessions", "codex"):
        ns_dir = candidates_root / ns
        if ns_dir.is_dir():
            counts[ns] = len(rs.list_candidates(str(ns_dir), status="staged"))
    return counts


def count_misplaced_per_namespace(brain_root: Path) -> dict[str, int]:
    """Count non-staged JSON files sitting at the top of each namespace's
    candidates dir. These are leaks — `mark_graduated`/`mark_rejected`
    moves write to a subdir then remove the top; an interrupted move (or
    an external tool writing to the wrong place) leaves a non-staged file
    at top.

    Surfacing the count in the banner means the next time this happens we
    SEE it instead of silently inflating pending counts and going stale.
    """
    rs = _review_state()
    counts = {"default": 0, "claude-sessions": 0, "codex": 0}
    candidates_root = brain_root / "memory" / "candidates"
    if not candidates_root.is_dir():
        return counts
    counts["default"] = len(rs.find_misplaced_candidates(str(candidates_root)))
    for ns in ("claude-sessions", "codex"):
        ns_dir = candidates_root / ns
        if ns_dir.is_dir():
            counts[ns] = len(rs.find_misplaced_candidates(str(ns_dir)))
    return counts


def _load_candidates(brain_root: Path) -> list[tuple[str, dict]]:
    """Load every pending candidate across all namespaces, paired with
    its source namespace label. Used to populate the top-N list."""
    out: list[tuple[str, dict]] = []
    candidates_root = brain_root / "memory" / "candidates"
    if not candidates_root.is_dir():
        return out
    for p in candidates_root.glob("*.json"):
        if p.is_file():
            try:
                out.append(("default", json.loads(p.read_text())))
            except (OSError, json.JSONDecodeError):
                continue
    for ns in ("claude-sessions", "codex"):
        ns_dir = candidates_root / ns
        if not ns_dir.is_dir():
            continue
        for p in ns_dir.glob("*.json"):
            if p.is_file():
                try:
                    out.append((ns, json.loads(p.read_text())))
                except (OSError, json.JSONDecodeError):
                    continue
    return out


# ---------- sync staleness -------------------------------------------


# Per-marker classification: (substring, reason). Order matters — first
# match wins, so put the most specific marker first.
_SYNC_BLOCKED_MARKERS: tuple[tuple[str, str], ...] = (
    # No secret scanner on PATH: sync.sh fails closed and the brain
    # silently never pushes. Most specific marker first so it isn't
    # shadowed by the generic ones below.
    ("no secret scanner installed", "blocked-noscanner"),
    # The gate itself could not run (missing scan_gate.py, scanner crash).
    # These MUST precede the generic "refusing to push" below, or they
    # render as "verified secret in your tree" and send the user hunting
    # for a secret that does not exist.
    ("scan_gate.py missing",      "blocked-scanner"),
    ("secret scan could not run", "blocked-scanner"),
    # We identified a risky file but could not pull it out of the commit.
    ("could not unstage",         "blocked-unstage"),
    # Trufflehog hit: a verified secret in the working tree.
    ("trufflehog flagged",       "blocked-trufflehog"),
    ("refusing to push",         "blocked-trufflehog"),
    # Pre-commit hook (redact.py etc.) blocked the local commit. Server-
    # side trufflehog never ran here — this is a client-side scrubber
    # refusing to let the commit through.
    ("commit blocked",           "blocked-precommit"),
    # Push failed AFTER the commit succeeded. Typically network /
    # DNS / GitHub-reachability problem — NOT a secret-scanner hit.
    # (Prior code lumped this into "blocked" and the banner falsely
    # claimed trufflehog found a secret — see 2026-05-20 user report.)
    ("push failed",              "blocked-network"),
)

# Lines that terminate one sync run in the log. Used to scope a backward
# scan to the most recent run only.
_RUN_TERMINAL_MARKERS: tuple[str, ...] = (
    "sync: pushed", "sync: no changes", "refusing to push",
    "commit blocked", "push failed", "skipping push",
)


def _last_run_quarantined(tail_lines: list[str]) -> bool:
    """True if the most recent sync run held files back from the commit.

    A run can push successfully AND still have skipped a file whose
    contents tripped the secret scanner. The terminal line then reads
    "sync: pushed", so the plain last-line check returns 'ok' and the
    partial sync is invisible. That silence is precisely the failure mode
    that let a month of memories sit unpushed — surface it instead.
    """
    for line in reversed(tail_lines[:-1]):
        low = line.lower()
        if any(m in low for m in _RUN_TERMINAL_MARKERS):
            break  # walked back into the previous run
        if "sync: held back" in low:
            return True
    return False


# sync.sh prefixes every log line with `date -u +%FT%TZ`. Stripping it
# lets us quote git's own output verbatim.
_LOG_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+")

_QUARANTINE_MARKER = "sync: quarantined (not pushed):"
_OVERSIZE_MARKER = "sync: oversize (not pushed):"
# `<path> (114294784 bytes > 52428800-byte limit)` -> `<path>`
_OVERSIZE_SUFFIX_RE = re.compile(r"\s*\(\d+\s+bytes\s*>.*\)\s*$")

# A health report older than this is evidence the hourly agent died, not
# evidence that the brain is fine. Matches recall.health.HEALTH_STALE_HOURS.
_HEALTH_STALE_HOURS = 26.0

# Remote errors that no amount of hourly retrying will fix.
_SIZE_REJECTION_RE = re.compile(r"file size limit|GH001|large files", re.IGNORECASE)


def _last_remote_error(tail_lines: list[str]) -> Optional[str]:
    """The `remote: error: ...` line git printed during the most recent
    sync run, or `None` if the tail carries no such line.

    Scoped to the last run: the scan walks backwards and stops at the
    PREVIOUS run's terminal marker, so a failure from five days ago is
    never quoted as if it were today's. `health:` lines are skipped —
    sync.sh writes them from the same exit trap, so they land after the
    run's terminal marker and would otherwise shadow the git output.
    """
    last = len(tail_lines) - 1
    for idx in range(last, -1, -1):
        line = tail_lines[idx]
        low = line.lower()
        if "health:" in low:
            continue
        if "remote: error:" in low:
            return _LOG_TS_RE.sub("", line).strip()
        if idx != last and any(m in low for m in _RUN_TERMINAL_MARKERS):
            break
    return None


def _held_back_paths(tail_lines: list[str]) -> dict[str, list[str]]:
    """Paths a sync run quarantined (secret hit) or held back (oversize),
    keyed `"secret"` / `"oversize"`.

    Two markers, two remedies: a quarantined file needs an allowlist entry
    or a scrub, an oversize file needs untracking. Returning them apart
    means the banner can give the right advice for each.
    """
    out: dict[str, list[str]] = {"secret": [], "oversize": []}
    last = len(tail_lines) - 1
    for idx in range(last, -1, -1):
        line = tail_lines[idx]
        if _QUARANTINE_MARKER in line:
            out["secret"].append(line.split(_QUARANTINE_MARKER, 1)[1].strip())
            continue
        if _OVERSIZE_MARKER in line:
            raw = line.split(_OVERSIZE_MARKER, 1)[1].strip()
            out["oversize"].append(_OVERSIZE_SUFFIX_RE.sub("", raw).strip())
            continue
        low = line.lower()
        if idx != last and any(m in low for m in _RUN_TERMINAL_MARKERS):
            break
    out["secret"].reverse()
    out["oversize"].reverse()
    return out


def _last_run_oversize(tail_lines: list[str]) -> bool:
    """True if the most recent sync run held back an oversize file."""
    return bool(_held_back_paths(tail_lines)["oversize"])


def _sync_log_tail(brain_root: Path, limit: int = 400) -> list[str]:
    """The last `limit` raw lines of `<brain>/sync.log` (git output
    included — unlike `_check_sync_status`, which keeps `sync:` lines
    only, the remote-error parser needs the untouched tail)."""
    log = brain_root / "sync.log"
    try:
        return log.read_text().splitlines()[-limit:]
    except OSError:
        return []


def _load_health(brain_root: Path) -> Optional[dict]:
    """Load `<brain>/runtime/health.json`, adding a `"stale"` bool (> 26h
    old). `None` if missing or unreadable."""
    path = brain_root / "runtime" / "health.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["stale"] = _health_age_hours(data) > _HEALTH_STALE_HOURS
    return data


def _health_age_hours(health: dict) -> float:
    """Hours since the report was generated. An unparseable timestamp is
    treated as infinitely old — we cannot vouch for a report we cannot
    date."""
    raw = str(health.get("generated_at") or "").strip()
    try:
        generated = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return float("inf")
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (now - generated).total_seconds() / 3600.0)


def _check_sync_status(brain_root: Path) -> str:
    """Return a precise sync-status string so the banner can render an
    accurate reason instead of a single misleading "TruffleHog blocked"
    line for every failure mode.

    Values:
      - 'missing'             — sync.log doesn't exist (sync never ran)
      - 'blocked-noscanner'   - no secret scanner installed; sync fails closed
      - 'blocked-trufflehog'  — trufflehog flagged a verified secret
      - 'blocked-precommit'   — local pre-commit hook (redact.py etc.) blocked commit
      - 'blocked-network'     — commit succeeded but push failed (remote unreachable)
      - 'blocked-scanner'     — the secret gate could not run (not a secret hit)
      - 'blocked-unstage'     — a risky file could not be removed from the commit
      - 'oversize'            — push succeeded but a >50 MB file was held back
      - 'quarantined'         — push succeeded but some file(s) were held back
      - 'stale'               — last sync line is > 2 hours old
      - 'ok'                  — last line is a successful push or no-op

    Callers can treat the three 'blocked-*' variants uniformly via
    `status.startswith("blocked")` if they don't care about the specific
    cause; `compose_summary` uses the full string to pick the right text.
    """
    log = brain_root / "sync.log"
    if not log.is_file():
        return "missing"
    try:
        text = log.read_text()
    except OSError:
        return "missing"
    tail_lines = [ln for ln in text.splitlines()[-100:] if "sync:" in ln]
    if tail_lines:
        last = tail_lines[-1].lower()
        for marker, reason in _SYNC_BLOCKED_MARKERS:
            if marker in last:
                return reason
        # Pushed, but not everything went. Never let this pass as 'ok'.
        # Oversize first: a size hold-back also logs a "held back" line, so
        # the quarantine check below would otherwise claim a secret hit and
        # send the user hunting for a credential that does not exist.
        if _last_run_oversize(tail_lines):
            return "oversize"
        if _last_run_quarantined(tail_lines):
            return "quarantined"
    try:
        mtime = log.stat().st_mtime
    except OSError:
        return "missing"
    age_seconds = datetime.datetime.now().timestamp() - mtime
    if age_seconds > 2 * 3600:
        return "stale"
    return "ok"


# ---------- compose / render -----------------------------------------


_ALL_CLEAR_LINE = "✅ all clear\n"


def compose_summary(
    brain_root: Path,
    drift_report: Optional[dict] = None,
    sync_status: str = "ok",
    *,
    sync_error: Optional[str] = None,
    held_back: Optional[dict] = None,
    health: Optional[dict] = None,
) -> str:
    """Build the markdown body. Returns a one-liner if everything's clean
    (so SessionStart hook can suppress chatter on healthy days).

    `sync_error`, `held_back`, and `health` are S5 additions (requirements
    2 and 3): quoting the actual git error behind a blocked-network sync,
    naming oversize-held-back paths, and rendering the `## Health` section
    from `runtime/health.json`. All three are optional, so existing callers
    that don't pass them behave exactly as before.
    """
    counts = count_pending_per_namespace(brain_root)
    total = sum(counts.values())
    misplaced = count_misplaced_per_namespace(brain_root)
    misplaced_total = sum(misplaced.values())
    drift_in_sync = bool(drift_report and drift_report.get("in_sync", True))
    if drift_report is None:
        drift_in_sync = True

    health_checks = (health or {}).get("checks") or []
    health_fail = [c for c in health_checks if c.get("status") == "FAIL"]
    health_warn = [c for c in health_checks if c.get("status") == "WARN"]
    health_stale = bool((health or {}).get("stale"))
    # A WARN has to break the one-liner or the section it belongs to would
    # never be read; a stale report is not evidence that anything is fine.
    health_dirty = bool(health_fail or health_warn or health_stale)

    oversize_paths = list((held_back or {}).get("oversize") or [])
    show_oversize = sync_status == "oversize" or bool(oversize_paths)

    if (total == 0 and drift_in_sync
            and sync_status == "ok" and misplaced_total == 0
            and not health_dirty):
        return _ALL_CLEAR_LINE

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []

    # Lead with a system-reminder directive that gets the model to
    # SURFACE the pending count to the user in chat. Without this,
    # the @-imported content sits in the model's context window
    # silently — the user never sees it because system-reminders don't
    # render in the chat UI. Dogfood testing confirmed the data WAS being
    # injected, but the model wasn't proactively mentioning it.
    if total > 0:
        # Terse directive. Three rules baked in based on dogfood feedback:
        #   1. Say exactly "brainstack: N pending - run `recall pending --review`"
        #   2. Tell the user to run it in THEIR terminal — do NOT run it on
        #      their behalf. The tool refuses to run without a TTY anyway,
        #      but the directive makes the rule explicit.
        #   3. Never auto-graduate / auto-reject. Each candidate decision
        #      belongs to the user; even an "obvious noise" cluster gets
        #      shown to the user and waits for their choice.
        directive_lines = [
            "<system-reminder>",
            f"BRAINSTACK: At the start of your first response, tell the user "
            f"\"brainstack: {total} pending - run `recall pending --review` "
            f"in your terminal\" (once per session, skip if user already "
            f"addressed it). Do NOT run `recall pending --review` on the "
            f"user's behalf via the Bash tool, and do NOT call graduate.py "
            f"or reject.py without per-candidate explicit consent. The "
            f"user makes each decision themselves.",
            "</system-reminder>",
            "",
        ]
        lines.extend(directive_lines)

    lines.append("# brainstack: pending review")
    lines.append("")
    lines.append(f"_Generated {now_iso}_")
    lines.append("")

    # Headline
    parts = []
    if total > 0:
        parts.append(f"**{total} candidates pending**")
    else:
        parts.append("**0 candidates pending**")
    if not drift_in_sync:
        parts.append("⚠️ drift detected")
    if sync_status != "ok":
        # Strip the leading "blocked-" prefix in the headline; the dedicated
        # Sync section below explains the specific cause. Headline reads
        # naturally as "⚠️ sync trufflehog" / "⚠️ sync network" / etc.
        headline_status = sync_status.removeprefix("blocked-") if sync_status.startswith("blocked-") else sync_status
        parts.append(f"⚠️ sync {headline_status}")
    if misplaced_total > 0:
        parts.append(f"⚠️ {misplaced_total} misplaced")
    if health_stale:
        parts.append("⚠️ health stale")
    if health_fail:
        parts.append(f"⚠️ health {len(health_fail)} fail")
    elif health_warn:
        parts.append(f"⚠️ health {len(health_warn)} warn")
    lines.append(" | ".join(parts))
    lines.append("")

    # Misplaced files section — surface the leak so the framework bug is
    # visible. `mark_graduated`/`mark_rejected` move files via "write dst,
    # remove src" and a crash leaves the src untouched (still staged), so
    # a misplaced file at top means SOMETHING ELSE wrote it: external
    # tooling, a manual edit, a buggy producer, or a backup restore. Auto-
    # fixing would mask the producer bug; flagging it makes it actionable.
    if misplaced_total > 0:
        lines.append("## Misplaced (non-staged file at top)")
        lines.append(
            f"- {misplaced_total} candidate file(s) sit at the top of "
            "`memory/candidates/` with status != staged. "
            "Likely cause: interrupted lifecycle move OR external write."
        )
        lines.append("- Inspect: `python ~/.agent/tools/list_candidates.py`")
        lines.append("- Move each to its proper subdir based on status field, "
                     "or delete after verifying it's duplicated in graduated/rejected.")
        lines.append("")

    # Per-namespace breakdown
    if total > 0:
        lines.append("## Candidates")
        lines.append(
            f"- default: {counts['default']}  "
            f"| claude-sessions: {counts['claude-sessions']}  "
            f"| codex: {counts['codex']}"
        )
        lines.append("")

        # Top 5 by cluster_size, noise-filtered
        all_cands = _load_candidates(brain_root)
        signal = [
            (ns, c) for ns, c in all_cands
            if c.get("status") == "staged" and not _is_noise_cluster(c)
        ]
        signal.sort(key=lambda nsc: nsc[1].get("cluster_size", 0), reverse=True)
        if signal:
            lines.append("### Top 5 by signal")
            for ns, c in signal[:5]:
                claim = (c.get("claim") or "?")[:90]
                cluster = c.get("cluster_size", "?")
                sal = c.get("canonical_salience", 0)
                lines.append(
                    f"- `cluster={cluster} sal={sal:.1f}` | {claim} *({ns})*"
                )
            lines.append("")
        else:
            lines.append("_(all candidates filtered as test-infra noise; "
                         "review queue empty after filter)_")
            lines.append("")

    # Drift section
    if not drift_in_sync and drift_report is not None:
        lines.append("## Drift")
        lines.append(f"- {drift_report.get('summary', 'drift detected')}")
        lines.append("- Run `./install.sh --upgrade` from the brainstack repo")
        lines.append("")

    # Sync section — message matches the actual sync.log marker so a
    # transient network failure doesn't trigger "verified secret in tree"
    # panic, and a real trufflehog hit isn't masked as "push failed".
    if sync_status != "ok" or show_oversize:
        lines.append("## Sync")
        if sync_status == "stale":
            lines.append("- Last sync > 2h ago. Hourly LaunchAgent may be stuck.")
        elif sync_status == "blocked-noscanner":
            lines.append("- No secret scanner (trufflehog or gitleaks) is installed, so sync.sh refuses to push. The brain is NOT syncing.")
            lines.append("- Install one: `./install.sh --install-scanner` (or `brew install trufflehog`), then re-run `~/.agent/tools/sync.sh`.")
        elif sync_status == "blocked-trufflehog":
            lines.append("- TruffleHog blocked the last push (verified secret in the working tree).")
            lines.append("- Run `~/.agent/tools/sync.sh` and inspect the secret hit; rewrite history if needed.")
        elif sync_status == "blocked-precommit":
            lines.append("- Local pre-commit hook (likely `redact.py`) refused the commit.")
            lines.append("- Run `~/.agent/tools/sync.sh` to see the offending pattern; adjust `redact-private.txt` or scrub the input.")
        elif sync_status == "blocked-network":
            lines.append("- Commit succeeded locally but the push failed — usually a network/remote-reachability issue, NOT a secret.")
            if sync_error:
                # Quote git verbatim. "push failed" alone kept a 107 MB
                # rejection looking like patience for five days (2026-09-04).
                lines.append("- Last remote error:")
                lines.append(f"  `{sync_error}`")
                if _SIZE_REJECTION_RE.search(sync_error):
                    lines.append(
                        "- A tracked file exceeds GitHub's limit; see "
                        "`recall health` (large_tracked_files). Retrying hourly "
                        "will never clear this."
                    )
            else:
                lines.append("- The push failed (no git error captured in sync.log).")
            lines.append("- The brain repo is committed locally; the next hourly sync will retry. Run `~/.agent/tools/sync.sh` manually to retry now.")
        elif sync_status == "blocked-scanner":
            lines.append("- The secret scanner could not run, so sync.sh refused to push. This is NOT a secret in your tree — the gate itself is broken.")
            lines.append("- Usually a half-finished upgrade: re-run `./install.sh` to restore `~/.agent/tools/scan_gate.py`, then `~/.agent/tools/sync.sh`.")
        elif sync_status == "blocked-unstage":
            lines.append("- A risky file was identified but could not be removed from the commit, so the push was refused rather than risk publishing it.")
            lines.append("- See the `could not unstage` line in `~/.agent/sync.log` for the path; check for an unusual filename or a locked index (`.git/index.lock`).")
        elif sync_status == "quarantined":
            lines.append("- Last sync pushed, but held back one or more files whose contents tripped the secret scanner. **Those memories are NOT on the remote.**")
            lines.append("- See the `quarantined (not pushed)` lines in `~/.agent/sync.log` for the exact paths.")
            lines.append("- If a hit is a false positive, add a regex for it to `~/.agent/.secret-scan-allowlist.txt`; if it is a real secret, scrub the file. Either way the next sync picks it up automatically.")
        elif sync_status == "missing":
            lines.append("- No sync.log yet (sync never ran).")
        if show_oversize:
            n = len(oversize_paths) or 1
            listed = ", ".join(f"`{p}`" for p in oversize_paths) or "see `~/.agent/sync.log`"
            lines.append(
                f"- Last sync held back {n} file(s) larger than 50 MB "
                f"(GitHub rejects >100 MB): {listed}"
            )
            lines.append(
                "- Untrack: `git -C ~/.agent rm --cached <path>`; "
                "`./install.sh --upgrade` adds the ignore rule."
            )
        lines.append("")

    # Health section — FAIL and WARN only. PASS/SKIP checks belong in the
    # full `recall health` report, not in a banner the user reads at the
    # start of every session.
    if health_dirty:
        lines.append("## Health")
        if health_stale:
            age = _health_age_hours(health or {})
            age_text = f"{int(age)}h" if age != float("inf") else "an unknown number of hours"
            lines.append(
                f"- health report is {age_text} old; the hourly sync LaunchAgent "
                "may be stuck (`launchctl list | grep agent-sync`)"
            )
        else:
            generated = str((health or {}).get("generated_at") or "unknown")
            lines.append(f"_from runtime/health.json generated {generated}_")
        for check in health_fail + health_warn:
            lines.append(
                f"- {check.get('status')} `{check.get('id')}` — "
                f"{check.get('evidence', '')}"
            )
            fix = str(check.get("fix") or "").strip()
            if fix:
                lines.append(f"  - fix: {fix}")
        lines.append("- Full report: `recall health`")
        lines.append("")

    # Triage instructions
    lines.append("## Triage")
    lines.append("- Claude Code: `/dream` skill (interactive review)")
    lines.append("- CLI: `python ~/.agent/tools/list_candidates.py`")
    lines.append("- Or: `recall pending --review`")
    lines.append("")

    return "\n".join(lines)


def _status_inputs(brain_root: Path) -> dict:
    """Everything `compose_summary` needs from sync.log + health.json.

    One helper so `render()` and `--print-only` cannot drift apart: the
    two used to duplicate the sync-status call, and every new signal
    (sync_error, held_back, health) doubled the chance of one of them
    silently rendering a stale banner.
    """
    tail = _sync_log_tail(brain_root)
    return {
        "sync_status": _check_sync_status(brain_root),
        "sync_error": _last_remote_error(tail) if tail else None,
        "held_back": _held_back_paths(tail) if tail else None,
        "health": _load_health(brain_root),
    }


def render(brain_root: Path) -> Path:
    """Write <brain>/PENDING_REVIEW.md atomically. Returns the path written."""
    # Lazy import — check_freshness lives in the brain's tools/ at runtime.
    drift_report: Optional[dict] = None
    try:
        # Preferred: import from the brain's tools/ directly (when running
        # from ~/.agent/tools/render_pending_summary.py)
        sys.path.insert(0, str(brain_root / "tools"))
        import check_freshness  # type: ignore  # noqa: WPS433
        repo = check_freshness._default_repo_dir(brain_root)
        if repo is not None:
            drift_report = check_freshness.detect_drift(repo, brain_root)
    except Exception:
        drift_report = None  # silently skip drift check on failure

    body = compose_summary(
        brain_root, drift_report=drift_report, **_status_inputs(brain_root)
    )
    out = brain_root / "PENDING_REVIEW.md"
    atomic_write_text(out, body)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="render_pending_summary")
    p.add_argument(
        "--brain",
        default=os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")),
        help="Brain root (default: $BRAIN_ROOT or ~/.agent)",
    )
    p.add_argument(
        "--print-only", action="store_true",
        help="Print summary to stdout; do NOT write the file"
    )
    args = p.parse_args(argv)

    brain_root = Path(args.brain).expanduser()
    if not brain_root.is_dir():
        sys.stderr.write(f"render_pending_summary: brain not found: {brain_root}\n")
        return 2

    if args.print_only:
        # Compose without writing — same code path as render but skip
        # atomic_write_text.
        drift_report = None
        try:
            sys.path.insert(0, str(brain_root / "tools"))
            import check_freshness  # type: ignore
            repo = check_freshness._default_repo_dir(brain_root)
            if repo is not None:
                drift_report = check_freshness.detect_drift(repo, brain_root)
        except Exception:
            pass
        sys.stdout.write(compose_summary(
            brain_root, drift_report=drift_report, **_status_inputs(brain_root)
        ))
        return 0

    render(brain_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
