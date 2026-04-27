"""Test config.

Previously the vendored upstream hook (`claude_code_post_tool.py`) used 3.10+
syntax (`re.Pattern | None`) without `from __future__ import annotations`, so
hook precedence tests had to be skipped on Python < 3.10. The hook now has
the future import, so this file is intentionally minimal — kept only as a
hook for future test-collection logic.
"""
