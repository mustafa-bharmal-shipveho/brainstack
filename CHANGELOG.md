# Changelog

## v0.0.1 — Scaffold (2026-04-26)

- Initial repo skeleton: `tools/`, `hooks/`, `adapters/claude-code/`, `schemas/`, `templates/`, `docs/`, `tests/`, `examples/`, `memory_seed/`
- LICENSE (Apache 2.0)
- NOTICE (attribution to codejunkie99/agentic-stack v0.11.2)
- UPSTREAM.md (vendored file inventory, pinned commit, rebase process)
- README.md (pitch + quickstart placeholder)

## v0.1.0 — Lean MVP + dashboard (planned)

Will include:
- `install.sh` targeting `~/.agent/` globally
- Vendored dream cycle from upstream agentic-stack v0.11.2 (20 files, 3,683 lines)
- Lessons.jsonl schema extension for `why` / `how_to_apply` fields
- Clean-room: redact.py, sync.sh, migrate.py, hooks/agentic_post_tool_global.py
- Claude Code adapter (settings.json snippet, manual-merge instructions)
- Data-layer dashboard exporter (vendored from upstream)
- Documentation pass: architecture, memory-model, dream-cycle, claude-code-setup, git-sync, redaction-policy, hook-precedence
- Privacy audit (gitleaks + trufflehog + manual `git grep` + fresh-account smoke install)
