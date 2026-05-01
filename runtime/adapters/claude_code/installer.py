"""Idempotent installer for runtime hooks into Claude Code's settings.json.

Claude Code reads `~/.claude/settings.json` (or a per-project equivalent).
The hooks system is a JSON object keyed by event name. We add entries that
invoke this Python package's adapter via `python -m runtime.adapters.claude_code.hooks <event>`.

Design constraints:
  - Idempotent: re-running install_claude_code_hooks must be a no-op.
  - Non-destructive: existing hooks for the same events are preserved.
    We add our entries alongside, marked with a stable identifier so we
    can detect already-installed instances.
  - Safe: never silently overwrite an existing settings.json. If a parse
    fails, we report and refuse to write.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Identifier embedded in our installed hook commands so we can detect them
# during install/uninstall without ambiguity.
_INSTALL_MARKER = "# brainstack-runtime"

_HOOK_TEMPLATES: dict[str, str] = {
    "SessionStart":     f"{sys.executable} -m runtime.adapters.claude_code.hooks SessionStart {_INSTALL_MARKER}",
    "UserPromptSubmit": f"{sys.executable} -m runtime.adapters.claude_code.hooks UserPromptSubmit {_INSTALL_MARKER}",
    "PostToolUse":      f"{sys.executable} -m runtime.adapters.claude_code.hooks PostToolUse {_INSTALL_MARKER}",
    "Stop":             f"{sys.executable} -m runtime.adapters.claude_code.hooks Stop {_INSTALL_MARKER}",
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
