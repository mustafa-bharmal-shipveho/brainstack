"""Tests for source_ts.normalize_source_ts — the producer-agnostic
comparable-timestamp seam.

The consolidator NEVER branches on `source`. To make supersession ordering
work across heterogeneous producers (Slack float-strings, Gmail ISO,
calendar ISO, hand-rolled producers), this module parses any reasonable
representation and produces a single epoch-seconds float.

These tests pin the cascade behavior so a future tweak doesn't silently
accept microseconds-since-epoch (which would always win every
comparison) or reject historical fixtures (which are convenient in tests).
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import source_ts  # noqa: E402


# Slack-style float-string seconds-since-epoch.
def test_slack_float_string_parses_as_epoch_s():
    f, label = source_ts.normalize_source_ts("1700000000.000100")
    assert label == "float-epoch-s"
    assert 1700000000 <= f < 1700000001


def test_iso_8601_with_z_suffix_parses_as_iso():
    # 3.9's fromisoformat doesn't accept "Z"; the module normalizes it.
    f, label = source_ts.normalize_source_ts("2026-05-12T10:00:00Z")
    assert label == "iso"
    # 2026-05-12T10:00:00Z is 1778148000 epoch seconds.
    assert 1.77e9 < f < 1.79e9


def test_iso_8601_without_tz_treated_as_utc():
    f, label = source_ts.normalize_source_ts("2026-05-12T10:00:00")
    assert label == "iso"
    assert 1.77e9 < f < 1.79e9


def test_iso_with_offset_parses():
    f, label = source_ts.normalize_source_ts("2026-05-12T03:00:00-07:00")
    assert label == "iso"
    assert 1.77e9 < f < 1.79e9


# Critical: microseconds-since-epoch must NOT pass as seconds. Codex P1.6.
def test_microseconds_since_epoch_falls_through_to_iso():
    # 1778524091273169 microseconds = year ~56370 if interpreted as seconds.
    # The bounds check should reject it. With a fallback_iso, we land on iso.
    f, label = source_ts.normalize_source_ts(
        "1778524091273169", fallback_iso="2026-05-12T00:00:00Z",
    )
    assert label == "kernel-ts"


def test_microseconds_rescue_path_for_numeric_value():
    # Numeric microseconds (not string): the module's scale-rescue path
    # accepts these explicitly. This is a single source-agnostic helper,
    # not source-specific code.
    us = int(1778524091.273169 * 1e6)
    f, label = source_ts.normalize_source_ts(us)
    assert label == "float-epoch-us"
    assert 1.77e9 < f < 1.79e9


def test_nanoseconds_rescue_path_for_numeric_value():
    ns = int(1778524091.273169 * 1e9)
    f, label = source_ts.normalize_source_ts(ns)
    assert label == "float-epoch-ns"
    assert 1.77e9 < f < 1.79e9


def test_year_1970_epoch_zero_accepted():
    f, label = source_ts.normalize_source_ts("0")
    assert label == "float-epoch-s"
    assert f == 0.0


def test_year_3000_iso_rejected_floats():
    # As a float-string it's out of EPOCH_MAX bound; ISO parse also exceeds
    # EPOCH_MAX so we expect failure (no fallback provided).
    with pytest.raises(source_ts.SourceTsRangeError):
        source_ts.normalize_source_ts("3000-01-01T00:00:00Z")


def test_falls_back_to_kernel_ts_when_source_ts_unparseable():
    f, label = source_ts.normalize_source_ts(
        "garbage", fallback_iso="2026-05-12T10:00:00Z",
    )
    assert label == "kernel-ts"
    assert 1.77e9 < f < 1.79e9


def test_unparseable_value_with_no_fallback_raises():
    with pytest.raises(source_ts.SourceTsRangeError):
        source_ts.normalize_source_ts("not a timestamp")


def test_none_value_with_no_fallback_raises():
    with pytest.raises(source_ts.SourceTsRangeError):
        source_ts.normalize_source_ts(None)


def test_none_value_with_kernel_fallback_uses_fallback():
    f, label = source_ts.normalize_source_ts(
        None, fallback_iso="2026-05-12T10:00:00Z",
    )
    assert label == "kernel-ts"
    assert 1.77e9 < f < 1.79e9


def test_numeric_seconds_accepted_directly():
    f, label = source_ts.normalize_source_ts(1700000000.5)
    assert label == "float-epoch-s"
    assert f == 1700000000.5


def test_no_source_branching_in_module():
    """Read source_ts.py and assert no branching on producer names."""
    src = (REPO_ROOT / "agent" / "memory" / "source_ts.py").read_text()
    for name in ("slack", "gmail", "agentry", "discord", "calendar", "teams"):
        # Allow appearance in docstrings/comments, but no equality test.
        # Conservative heuristic: the string `"slack"` followed/preceded
        # by `==` or `in` would be a branch.
        for bad in (f'"{name}" ==', f'== "{name}"',
                    f"'{name}' ==", f"== '{name}'",
                    f'in ["{name}"', f'in ("{name}"',
                    f"in ['{name}'", f"in ('{name}'"):
            assert bad not in src, f"forbidden producer branch: {bad!r}"
