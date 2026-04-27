# mustafa-agentic-stack

**A global, harness-portable agent brain for one human, with cron-staged dream cycle and laptop-loss durability.**

Inspired by [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack), targeting a different design point: **one global brain at `~/.agent/`** (not per-project), with first-class git sync to a private GitHub repo so a laptop crash doesn't nuke years of accumulated lessons.

## What's different from upstream agentic-stack

| | upstream | this project |
|---|---|---|
| Brain location | per-project `.agent/` | global `~/.agent/` |
| Memory layers | working / episodic / semantic / personal | same |
| Dream cycle | cron + Stop hook | launchd (macOS) |
| Multi-harness | 10 adapters | Claude Code only at v0.1 (more in v0.2+) |
| Sync | unspecified | git → private GitHub repo, hourly |
| Redaction | unspecified | gitleaks + trufflehog + custom regex pre-commit |
| Onboarding | wizard | manual + docs |
| Distribution | brew formula | none at v0.1 |

## Quickstart

```bash
git clone https://github.com/mustafa-bharmal-shipveho/mustafa-agentic-stack.git
cd mustafa-agentic-stack
./install.sh
```

The installer creates `~/.agent/` with the 4-layer memory scaffolding, copies tools and hooks, and prints manual-merge instructions for `~/.claude/settings.json`.

For migration from a flat memory directory:

```bash
./install.sh --migrate ~/path/to/old/memory/
```

For the Claude Code hook setup, see [`docs/claude-code-setup.md`](docs/claude-code-setup.md).
For private GitHub repo sync, see [`docs/git-sync.md`](docs/git-sync.md).

## Architecture (v0.1)

```
~/.agent/
├── memory/
│   ├── working/       # ephemeral session state
│   ├── episodic/      # tool-call history (AGENT_LEARNINGS.jsonl)
│   ├── semantic/      # graduated lessons (lessons.jsonl + LESSONS.md)
│   ├── personal/      # profile, preferences, references, notes
│   ├── candidates/    # staged-by-dream-cycle, awaiting review
│   └── MEMORY.md      # human-readable index
├── tools/             # auto_dream, graduate, reject, list_candidates, ...
├── hooks/             # Claude Code PostToolUse + Stop entry points
└── .git/              # pushed to private remote (default: <your-account>/private-brain-repo)
```

## Status

- **v0.1**: Lean MVP. Claude Code only. Personal use first; public after fresh-install audit.
- **v0.2+**: Cursor / Codex / Windsurf adapters; flywheel exporter; onboarding wizard.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for attribution to the upstream agentic-stack project.

## Use at your own risk

This is personal infrastructure shared as-is. Issues and PRs welcome but no support obligations are implied.
