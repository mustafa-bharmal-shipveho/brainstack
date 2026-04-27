#!/usr/bin/env python3
"""Global Claude Code PostToolUse hook with precedence logic.

This is the entry point Claude Code invokes per tool call. It:

  1. Resolves the target brain location safely:
     - Default: ~/.agent/
     - If BRAIN_ROOT env var is set, validate it resolves to a path under
       $HOME (resolved real-path). If it doesn't, refuse and fall back to
       the default. This prevents env-poisoning RCE: a hostile project's
       .envrc / shell init that sets BRAIN_ROOT=/tmp/attacker can no longer
       redirect the wrapper into running attacker-controlled Python.
  2. Checks for `.agent-local-override` in $CLAUDE_PROJECT_DIR.
     If present, exits 0 immediately and (when AGENTIC_DEBUG=1 or the
     marker is in an unexpected location) logs the fire event so the user
     notices when logging silently turns off.
  3. Otherwise, dispatches to the vendored claude_code_post_tool.py with
     AGENT_ROOT pointing at the resolved brain.

Always exits 0 (a hook failure shouldn't break Claude Code's tool flow).
"""
import os
import subprocess
import sys
from pathlib import Path


def _expand(path: str) -> Path:
    """Expand ~ and resolve symlinks to real path."""
    return Path(os.path.expanduser(path)).resolve()


def _log_warning(msg: str) -> None:
    """Append a warning to ~/.agent/hook.log (best effort) and stderr."""
    sys.stderr.write(f"agentic-post-tool-global: {msg}\n")
    try:
        log = _expand("~/.agent/hook.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def resolve_brain_root() -> Path:
    """Pick the brain location, validating env-var input.

    Security: BRAIN_ROOT is attacker-controllable via shell env (e.g. a
    project's .envrc sourced by the user's shell). We must not trust it
    blindly — the wrapper exec's a Python script under the resolved
    brain root. Constraints:
      - resolved path must exist
      - resolved path must be under $HOME
      - resolved path must contain harness/hooks/claude_code_post_tool.py
        (otherwise it's not a brain dir at all)
    Falls back to ~/.agent on any failure, with a warning.
    """
    home = _expand("~")
    default = home / ".agent"

    explicit = os.environ.get("BRAIN_ROOT", "").strip()
    if not explicit:
        return default

    try:
        candidate = _expand(explicit)
    except (OSError, ValueError) as e:
        _log_warning(f"BRAIN_ROOT={explicit!r} could not be resolved: {e}; using default")
        return default

    # Must be under $HOME (real-path comparison, not string prefix —
    # Path.is_relative_to handles symlink-resolved paths correctly).
    try:
        if not candidate.is_relative_to(home):
            _log_warning(
                f"BRAIN_ROOT={candidate} is outside $HOME ({home}); refusing and using default"
            )
            return default
    except AttributeError:
        # is_relative_to is 3.9+; on older interpreters fall back to relative_to
        try:
            candidate.relative_to(home)
        except ValueError:
            _log_warning(f"BRAIN_ROOT={candidate} is outside $HOME; refusing")
            return default

    # Must look like a brain dir (has the vendored hook we'd execute)
    if not (candidate / "harness" / "hooks" / "claude_code_post_tool.py").exists():
        _log_warning(
            f"BRAIN_ROOT={candidate} does not contain harness/hooks/claude_code_post_tool.py; "
            "refusing and using default"
        )
        return default

    if candidate != default:
        _log_warning(f"BRAIN_ROOT override accepted: {candidate} (default would be {default})")

    return candidate


def has_local_override() -> tuple[bool, str]:
    """Return (override_active, marker_path_or_empty)."""
    project = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not project:
        return False, ""
    marker = Path(project) / ".agent-local-override"
    if marker.exists():
        return True, str(marker)
    return False, ""


def _log_override_fire(brain_root: Path, marker: str) -> None:
    """Append override-fire events to <brain>/override.log.

    Why: a `.agent-local-override` marker in a project root silently
    disables global logging for every tool call. A malicious or careless
    repo could ship that marker in its initial commit and the user would
    never notice their tool usage stopped being captured. Logging fire
    events to a separate file (not the noisy hook.log) gives the user a
    cheap audit trail — `tail override.log` shows every project that has
    suppressed logging today.

    Rate-limited at the file-size level: writes are bounded by override
    use, which is rare. We don't dedupe per-session because we want each
    session that hit the marker to appear in the log, not just one of them.

    Logs into the *resolved* BRAIN_ROOT, not a hardcoded ~/.agent. This
    matters when the user runs with a custom brain (BRAIN_ROOT env): the
    audit trail must follow the brain it's actually protecting.
    """
    try:
        log = brain_root / "override.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        # Cap at 1MB — beyond that the user has bigger problems than dedup.
        if log.exists() and log.stat().st_size > 1_000_000:
            return
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", "?")
        with log.open("a") as f:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"{ts}\t{cwd}\t{marker}\n")
    except OSError:
        pass


def main() -> int:
    override, marker = has_local_override()
    if override:
        # Resolve the brain BEFORE recording the override — we want the
        # audit log to live in the brain that *would have* received the
        # tool-call event, not always ~/.agent.
        brain = resolve_brain_root()
        _log_override_fire(brain, marker)
        if os.environ.get("AGENTIC_DEBUG", "").strip():
            _log_warning(f"local override active: {marker} (logging suppressed for this call)")
        return 0

    brain = resolve_brain_root()
    vendor_hook = brain / "harness" / "hooks" / "claude_code_post_tool.py"

    if not vendor_hook.exists():
        return 0

    payload = sys.stdin.read()
    try:
        result = subprocess.run(
            [sys.executable, str(vendor_hook)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stderr:
            sys.stderr.write(result.stderr)
    except (subprocess.TimeoutExpired, OSError) as e:
        _log_warning(f"hook dispatch failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
