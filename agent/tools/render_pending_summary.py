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


def count_pending_per_namespace(brain_root: Path) -> dict[str, int]:
    """Count `*.json` files DIRECTLY under each namespace's candidates dir.
    Excludes `graduated/` and `rejected/` archive subdirs.

    Namespaces tracked:
      - default          → <brain>/memory/candidates/*.json
      - claude-sessions  → <brain>/memory/candidates/claude-sessions/*.json
      - codex            → <brain>/memory/candidates/codex/*.json
    """
    counts = {"default": 0, "claude-sessions": 0, "codex": 0}
    candidates_root = brain_root / "memory" / "candidates"
    if not candidates_root.is_dir():
        return counts
    # default = top-level *.json (non-recursive; excludes graduated/, rejected/)
    try:
        counts["default"] = sum(
            1 for p in candidates_root.glob("*.json") if p.is_file()
        )
    except OSError:
        pass
    for ns in ("claude-sessions", "codex"):
        ns_dir = candidates_root / ns
        if not ns_dir.is_dir():
            continue
        try:
            counts[ns] = sum(
                1 for p in ns_dir.glob("*.json") if p.is_file()
            )
        except OSError:
            pass
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


_SYNC_BLOCKED_MARKERS = (
    # sync.sh writes one of these lines when push is blocked OR fails
    "refusing to push",
    "push failed",
    "commit blocked",
    "trufflehog flagged",
)


def _check_sync_status(brain_root: Path) -> str:
    """Return 'ok', 'stale', 'blocked', or 'missing'.

    - missing: sync.log doesn't exist (sync never ran)
    - blocked: last meaningful line indicates push failed or was refused
      (Codex 2026-05-05 P2: the parser used to match only "refusing to
      push", so "commit succeeded but push failed..." and "commit blocked..."
      were silently classified as ok until the log went stale. All
      sync.sh failure markers are now matched.)
    - stale: last sync line is > 2 hours old
    - ok: otherwise
    """
    log = brain_root / "sync.log"
    if not log.is_file():
        return "missing"
    try:
        text = log.read_text()
    except OSError:
        return "missing"
    tail_lines = [ln for ln in text.splitlines()[-100:] if "sync:" in ln]
    if tail_lines and any(m in tail_lines[-1] for m in _SYNC_BLOCKED_MARKERS):
        return "blocked"
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
) -> str:
    """Build the markdown body. Returns a one-liner if everything's clean
    (so SessionStart hook can suppress chatter on healthy days)."""
    counts = count_pending_per_namespace(brain_root)
    total = sum(counts.values())
    drift_in_sync = bool(drift_report and drift_report.get("in_sync", True))
    if drift_report is None:
        drift_in_sync = True

    if total == 0 and drift_in_sync and sync_status == "ok":
        return _ALL_CLEAR_LINE

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []

    # Lead with a system-reminder directive that gets the model to
    # SURFACE the pending count to the user in chat. Without this,
    # the @-imported content sits in the model's context window
    # silently — the user never sees it because system-reminders
    # don't render in the chat UI. Mustafa 2026-05-04: "still didnt
    # come up" — third diagnostic round confirmed the data WAS being
    # injected, but the model wasn't proactively mentioning it.
    if total > 0:
        # Terse directive. Three rules baked in based on user feedback:
        #   1. Say exactly "brainstack: N pending - run `recall pending --review`"
        #      (Mustafa 2026-05-04: "just say to use recall pending --review")
        #   2. Tell the user to run it in THEIR terminal — do NOT run it on
        #      their behalf (Mustafa 2026-05-05: "i want the users to be
        #      able to acccept or reject"). The tool refuses to run without
        #      a TTY anyway, but the directive makes the rule explicit.
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
        parts.append(f"⚠️ sync {sync_status}")
    lines.append(" | ".join(parts))
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

    # Sync section
    if sync_status != "ok":
        lines.append("## Sync")
        if sync_status == "stale":
            lines.append("- Last sync > 2h ago. Hourly LaunchAgent may be stuck.")
        elif sync_status == "blocked":
            lines.append("- TruffleHog blocked the last push (verified secret in tree).")
            lines.append("- Run `~/.agent/tools/sync.sh` and inspect output.")
        elif sync_status == "missing":
            lines.append("- No sync.log yet (sync never ran).")
        lines.append("")

    # Triage instructions
    lines.append("## Triage")
    lines.append("- Claude Code: `/dream` skill (interactive review)")
    lines.append("- CLI: `python ~/.agent/tools/list_candidates.py`")
    lines.append("- Or: `recall pending --review`")
    lines.append("")

    return "\n".join(lines)


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

    sync_status = _check_sync_status(brain_root)

    body = compose_summary(brain_root, drift_report=drift_report, sync_status=sync_status)
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
        sync_status = _check_sync_status(brain_root)
        sys.stdout.write(compose_summary(
            brain_root, drift_report=drift_report, sync_status=sync_status
        ))
        return 0

    render(brain_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
