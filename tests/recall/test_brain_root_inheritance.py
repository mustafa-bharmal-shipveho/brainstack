"""Tests for BRAIN_ROOT inheritance in resolve_brain_home().

When recall is integrated into brainstack, the user's single config knob is
$BRAIN_ROOT (set by brainstack's install.sh). recall must inherit this without
the user touching a recall-specific config or env var.

Precedence under test:
  1. $BRAIN_HOME (explicit override; standalone recall users)
  2. $BRAIN_ROOT/memory (brainstack-integrated default)
  3. $XDG_DATA_HOME/brain (XDG fallback)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.config import resolve_brain_home


def _clear_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_brain_home_takes_precedence_over_brain_root(monkeypatch, tmp_path):
    """If BRAIN_HOME and BRAIN_ROOT are both set, BRAIN_HOME wins.

    Standalone recall users may have BRAIN_HOME pointing somewhere bespoke; if
    they later install brainstack and it sets BRAIN_ROOT in their shell, we don't
    want to silently relocate the brain.
    """
    explicit = tmp_path / "explicit-home"
    brain_root = tmp_path / "brainstack-root"
    monkeypatch.setenv("BRAIN_HOME", str(explicit))
    monkeypatch.setenv("BRAIN_ROOT", str(brain_root))

    resolved = resolve_brain_home()

    assert resolved == explicit


def test_brain_root_used_when_brain_home_unset(monkeypatch, tmp_path):
    """The brainstack-installed user case: BRAIN_ROOT set, BRAIN_HOME unset.

    recall should land inside $BRAIN_ROOT/memory automatically — no recall-specific
    setup required.
    """
    brain_root = tmp_path / "brainstack-root"
    _clear_env(monkeypatch, "BRAIN_HOME")
    monkeypatch.setenv("BRAIN_ROOT", str(brain_root))

    resolved = resolve_brain_home()

    assert resolved == brain_root / "memory"


def test_xdg_fallback_when_neither_set(monkeypatch, tmp_path):
    """Pure-XDG fallback for users who haven't run brainstack and haven't set
    BRAIN_HOME — recall lands in $XDG_DATA_HOME/brain (default ~/.local/share/brain).
    """
    xdg_data = tmp_path / "xdg-data"
    _clear_env(monkeypatch, "BRAIN_HOME", "BRAIN_ROOT")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    resolved = resolve_brain_home()

    assert resolved == xdg_data / "brain"


def test_brain_root_with_tilde_is_expanded(monkeypatch):
    """Tilde and env-var expansion still works inside BRAIN_ROOT (matches the
    same expansion behavior BRAIN_HOME has had since v0.1).
    """
    _clear_env(monkeypatch, "BRAIN_HOME")
    monkeypatch.setenv("BRAIN_ROOT", "~/.agent")

    resolved = resolve_brain_home()

    assert "~" not in str(resolved)
    assert resolved == Path.home() / ".agent" / "memory"


def test_default_config_uses_resolved_brain_home(monkeypatch, tmp_path):
    """Sanity: default_config() composes resolve_brain_home() into the source
    path, so the BRAIN_ROOT inheritance flows through to the auto-generated
    config the user never has to open.
    """
    from recall.config import default_config

    brain_root = tmp_path / "brainstack-root"
    _clear_env(monkeypatch, "BRAIN_HOME")
    monkeypatch.setenv("BRAIN_ROOT", str(brain_root))

    cfg = default_config()

    assert len(cfg.sources) == 1
    assert cfg.sources[0].path == str(brain_root / "memory")
