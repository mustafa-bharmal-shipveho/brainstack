# Upstream tracking

This file tracks the pinned upstream commit, the vendored file list, and the
schema-compat status. Update on every "rebase vendored files" pass.

## Pinned upstream

- **Project**: codejunkie99/agentic-stack
- **URL**: https://github.com/codejunkie99/agentic-stack
- **Tag**: v0.11.2
- **Commit SHA**: `df806abace1a693e042844bf4ac0cccf9bb6270a`
- **Pinned on**: 2026-04-26
- **Last rebase**: 2026-04-26 (initial vendoring)

## Vendored file inventory (20 files)

Source path on left, our path on right. Modifications listed where they exist.

| Upstream | Our path | Modifications |
|---|---|---|
| `.agent/memory/auto_dream.py` | `tools/auto_dream.py` | none (path resolution is `__file__`-relative) |
| `.agent/memory/cluster.py` | `tools/cluster.py` | none |
| `.agent/memory/promote.py` | `tools/promote.py` | none |
| `.agent/memory/validate.py` | `tools/validate.py` | none |
| `.agent/memory/review_state.py` | `tools/review_state.py` | none |
| `.agent/memory/render_lessons.py` | `tools/render_lessons.py` | **modified**: `_bullet_for` extended for optional `why`/`how_to_apply` fields |
| `.agent/memory/decay.py` | `tools/decay.py` | none |
| `.agent/memory/archive.py` | `tools/archive.py` | none |
| `.agent/tools/graduate.py` | `tools/graduate.py` | none |
| `.agent/tools/reject.py` | `tools/reject.py` | none |
| `.agent/tools/reopen.py` | `tools/reopen.py` | none |
| `.agent/tools/list_candidates.py` | `tools/list_candidates.py` | none |
| `.agent/tools/data_layer_export.py` | `tools/data_layer_export.py` | none — full vendor (presentation rewrite deferred to v0.2 if needed) |
| `.agent/harness/hooks/claude_code_post_tool.py` | `hooks/claude_code_post_tool.py` | none |
| `.agent/harness/hooks/_episodic_io.py` | `hooks/_episodic_io.py` | none |
| `.agent/harness/hooks/_provenance.py` | `hooks/_provenance.py` | none |
| `.agent/harness/hooks/on_failure.py` | `hooks/on_failure.py` | none |
| `.agent/harness/hooks/post_execution.py` | `hooks/post_execution.py` | none |
| `.agent/harness/salience.py` | `hooks/salience.py` | none |
| `.agent/harness/text.py` | `hooks/text.py` | none |

## Schema-compat status

| Schema | Status | Test |
|---|---|---|
| lessons.jsonl | ✅ extended (backward compatible) | `tests/test_schema_compat.py::test_lessons_schema_extension` |
| candidates JSON | ✅ unchanged | `tests/test_schema_compat.py::test_candidates_unchanged` |
| episodic JSONL (`AGENT_LEARNINGS.jsonl`) | ✅ unchanged | `tests/test_schema_compat.py::test_episodic_unchanged` |
| data-layer schemas (`schemas/data-layer/*.json`) | ✅ vendored verbatim | n/a |

## Rebase process (for future upstream updates)

1. Fetch upstream: `cd /tmp/agentic-stack && git fetch && git checkout <new-tag>`
2. Diff every vendored file: `for f in tools/auto_dream.py tools/cluster.py ...; do diff <(cat /tmp/agentic-stack/<upstream-path>) <our-path>; done`
3. Re-apply our modifications (currently only `render_lessons.py::_bullet_for` extension)
4. Run `pytest tests/test_schema_compat.py` against new golden fixtures
5. If any test fails: investigate schema drift; either adapt our migrations or pin to the previous tag
6. Update this file: bump `Pinned on`, `Last rebase`, `Tag`, `Commit SHA`, modifications column
7. Commit: `chore(upstream): rebase vendored files to <new-tag>`
