"""Test config.

The vendored upstream hook (`claude_code_post_tool.py`) uses 3.10+ syntax
(`re.Pattern | None`) without `from __future__ import annotations`, so end-to-end
hook tests cannot run on Python < 3.10. Other test modules (redact, migrate,
schema) are 3.9-compatible.
"""
import sys

import pytest


def pytest_collection_modifyitems(config, items):
    if sys.version_info >= (3, 10):
        return
    skip_old = pytest.mark.skip(
        reason="hook tests need Python >= 3.10 (vendored upstream hook syntax)"
    )
    for item in items:
        if "test_hook_precedence" in item.nodeid:
            item.add_marker(skip_old)
