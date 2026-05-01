"""Idempotent installer for runtime hooks into Claude Code's settings.json.

Claude Code reads `~/.claude/settings.json` (or a per-project equivalent).
The hooks system is a JSON object keyed by event name. We add entries that
invoke this Python package's adapter via the ABSOLUTE path to the
adapter's hooks.py file. Phase 6 power-user review caught the bug: using
`python -m runtime.adapters.claude_code.hooks` is unreliable when the
user's session has a different `runtime` package on sys.path (very
common — many projects have a `runtime/` dir). Absolute path resolution
sidesteps the shadowing.

Design constraints:
  - Idempotent: re-running install_claude_code_hooks must be a no-op.
  - Non-destructive: existing hooks for the same events are preserved.
    We add our entries alongside, marked with a stable identifier so we
    can detect already-installed instances.
  - Safe: never silently overwrite an existing settings.json. If a parse
    fails, we report and refuse to write.
  - Robust to module-name shadowing.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Identifier embedded in our installed hook commands so we can detect them
# during install/uninstall without ambiguity.
_INSTALL_MARKER = "# brainstack-runtime"


def _resolve_hooks_script() -> str:
    """Absolute path to runtime/adapters/claude_code/hooks.py."""
    return str(Path(__file__).parent / "hooks.py")


def _resolve_pkg_root() -> str:
    """Directory containing the `runtime/` package — the path that needs to
    be on PYTHONPATH for `runtime.*` imports inside hooks.py to resolve."""
    # __file__ -> .../runtime/adapters/claude_code/installer.py
    return str(Path(__file__).resolve().parents[3])


_HOOKS_SCRIPT = _resolve_hooks_script()
_PKG_ROOT = _resolve_pkg_root()


def _hook_cmd(event: str) -> str:
    """Build a robust hook command that survives module shadowing.

    Setting PYTHONPATH ensures `from runtime.* import` resolves to OUR
    runtime package (installed alongside this file) regardless of the
    user's cwd or any `runtime/` directory in their project. Calling the
    script via absolute path bypasses `-m runtime` resolution entirely.
    """
    return (
        f"PYTHONPATH={_PKG_ROOT} {sys.executable} {_HOOKS_SCRIPT} {event}  {_INSTALL_MARKER}"
    )


_HOOK_TEMPLATES: dict[str, str] = {
    "SessionStart":     _hook_cmd("SessionStart"),
    "UserPromptSubmit": _hook_cmd("UserPromptSubmit"),
    "PostToolUse":      _hook_cmd("PostToolUse"),
    "Stop":             _hook_cmd("Stop"),
}

# PostToolUse uses a matcher to fire only on tools that produce useful items
_HOOK_MATCHERS: dict[str, str] = {
    "PostToolUse": "Read|Glob|Grep|Bash|Edit|Write",
}


@dataclass
class HookInstallReport:
    settings_path: Path
    added: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        lines = [f"settings: {self.settings_path}"]
        if self.error:
            return f"{lines[0]}\nerror: {self.error}"
        if self.added:
            lines.append(f"installed hooks for: {', '.join(self.added)}")
        if self.already_present:
            lines.append(f"already present:      {', '.join(self.already_present)}")
        if not (self.added or self.already_present):
            lines.append("(no changes)")
        return "\n".join(lines)


def install_claude_code_hooks(
    *,
    settings_path: Path,
    dry_run: bool = False,
) -> HookInstallReport:
    report = HookInstallReport(settings_path=settings_path)

    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            report.error = f"failed to read or parse {settings_path}: {e}"
            return report
    else:
        existing = {}

    if not isinstance(existing, dict):
        report.error = f"{settings_path} did not contain a JSON object"
        return report

    hooks = existing.setdefault("hooks", {}) if not dry_run else dict(existing.get("hooks") or {})
    if not isinstance(hooks, dict):
        report.error = "settings.json 'hooks' is not an object"
        return report

    for event, command in _HOOK_TEMPLATES.items():
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        if _is_already_installed(entries):
            report.already_present.append(event)
            continue
        new_entry = _entry_for(event, command)
        entries.append(new_entry)
        hooks[event] = entries
        report.added.append(event)

    if not dry_run:
        existing["hooks"] = hooks
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(existing, indent=2, sort_keys=True))

    return report


def _entry_for(event: str, command: str) -> dict:
    matcher = _HOOK_MATCHERS.get(event)
    inner = {"type": "command", "command": command}
    if matcher:
        return {"matcher": matcher, "hooks": [inner]}
    return {"hooks": [inner]}


def _is_already_installed(entries: list) -> bool:
    """Detect by the install marker so reinstalls are no-ops."""
    for e in entries:
        if not isinstance(e, dict):
            continue
        for h in e.get("hooks", []) or []:
            if isinstance(h, dict) and _INSTALL_MARKER in str(h.get("command", "")):
                return True
    return False


__all__ = ["HookInstallReport", "install_claude_code_hooks"]
