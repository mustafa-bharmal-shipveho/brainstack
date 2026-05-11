#!/usr/bin/env python3
"""Runs once at the start of a Claude Code session. Regenerates PENDING_REVIEW.md
so the user always sees the latest pending count + drift + sync status in their
CLAUDE.md imports.

This is cheap (< 100ms) and runs before any system-reminder sections are
composed, so the pending count is fresh in every session.
"""
import os
import sys
from pathlib import Path

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

# Lazy import
try:
    import render_pending_summary
    brain_root = Path(os.environ.get("BRAIN_ROOT", Path.home() / ".agent"))
    render_pending_summary.render(brain_root)
except Exception as e:
    # Silently skip on any error (missing brain, import failure, etc).
    # The session continues; pending review just stays stale.
    pass
