# Roadmap

This is direction, not commitment: items move as evidence comes in. One thing
does not move: brainstack stays per-user and local-first. That is the center
of gravity every item below is weighed against.

## Now

- **Trust hardening.** Sanitized injection surfaces, per-document provenance
  on recalled context, review-gated `recall remember`, and a refreshed
  [privacy audit](docs/privacy-audit.md).
- **Minimal-first install.** `--minimal` as the on-ramp, a consent gate and a
  true `--dry-run` for the full install.
- **Honest docs.** Every README claim matched to implemented behavior.

## Next

- **CI.** A GitHub Actions workflow (lint + the hermetic test subset on Linux
  and macOS) is prepared as `make test-ci` / `make lint-ci`; wiring it as a
  `.github/workflows/ci.yml` that runs on push is the immediate next step.
- **Benchmarks.** A LongMemEval run, plus an auto-recall on/off A/B on real
  agent tasks measuring re-explanation turns saved. Publish the numbers
  whatever they say.
- **Distribution.** Publish `recall-brain` to PyPI so `uvx` works; publish
  `server.json` to the MCP Registry; submit to mcp.so, Smithery, Glama, and
  awesome-mcp-servers; ship a Claude Code plugin `marketplace.json`.
- **Per-prompt injection adapters for Codex CLI and Cursor.** Today those
  hosts get recall-first directives plus `recall-mcp`; adapters bring them
  the same every-prompt injection Claude Code has.

## Later

- **Opt-in team lesson sharing.** Requires per-lesson provenance, redaction
  review, and explicit per-item consent before anything is shared. No ambient
  team sync; per-user remains the default.
- **Universal Memory Protocol.** Evaluate as an export/import surface.
- **Native Windows**, after WSL2 support is documented.

## Not planned

- Knowledge graphs.
- Ingestion-connector breadth.
- A hosted service.
