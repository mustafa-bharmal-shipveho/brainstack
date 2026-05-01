# Phase 0: Empirical research

Artifacts here are research, not shipped runtime code. They answer empirical
questions about Claude Code's hook system before any spec is written:

- `harness/` — hook telemetry harness (sub-phase 0a)
- `payload/` — tool-event payload samples (sub-phase 0b)
- `concurrency/` — flock + atomic-write smoke tests (sub-phase 0c)
- `phase0-empirical.md` — final writeup + go/no-go (sub-phase 0d)

This directory is intentionally not part of the runtime package. Once Phase 0
is closed, it stays as a record of what we found.
