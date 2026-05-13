"""Tests for the systemic $BRAIN_ROOT-resolution fix in recall/config.py.

Background: SourceConfig used raw `os.path.expandvars` which silently
left `$BRAIN_ROOT` unsubstituted when the env var was unset, producing
garbage paths (`'/cwd/$BRAIN_ROOT/memory'`) that `discover_documents`
returned 0 files for — with no error to indicate why. The fix
delegates to the canonical `resolve_brain_home()` and refuses to leave
any `$VAR` placeholder unresolved.

This is a framework property: the precedence ("where is the brain?")
is defined ONCE in `resolve_brain_home()` and every consumer
(SourceConfig included) inherits it. Adding a new env var (e.g.
`$BRAINSTACK_HOME`) is a one-line change in `resolve_brain_home`,
not a hunt across every callsite.
"""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from recall.config import (  # noqa: E402
    SourceConfig,
    resolve_brain_home,
    resolve_source_path,
)


# --- $BRAIN_ROOT resolution ------------------------------------------

def test_brain_root_set_resolves_to_env_value(monkeypatch, tmp_path):
    """The straightforward case: env var set → it wins."""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "myroot"))
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    out = resolve_source_path("$BRAIN_ROOT/memory")
    assert out == str(tmp_path / "myroot" / "memory")


def test_brainstack_default_directory_used_when_env_unset(monkeypatch, tmp_path):
    """When neither BRAIN_HOME nor BRAIN_ROOT is set, but ~/.agent/memory
    exists (the brainstack convention), resolve_brain_home() prefers
    that over the XDG default. Otherwise a fresh shell hits 'Indexed 0
    documents' silently because the config uses `$BRAIN_ROOT/memory`
    literals and the env isn't propagated from the installer.

    This is the brainstack-detection fallback. Pinned so future
    refactors don't regress to the XDG-only behavior."""
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / ".agent" / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    out = resolve_brain_home()
    assert out == fake_home / ".agent" / "memory"


def test_brainstack_default_not_used_when_dir_absent(monkeypatch, tmp_path):
    """If ~/.agent/memory does NOT exist, fall through to the XDG
    default. Keeps standalone recall users (no brainstack install)
    working as before."""
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Note: ~/.agent/memory deliberately NOT created.
    monkeypatch.setenv("HOME", str(fake_home))
    out = resolve_brain_home()
    assert str(out).endswith(".local/share/brain")


def test_brain_root_unset_falls_back_to_resolve_brain_home_parent(monkeypatch):
    """Critical: env unset MUST NOT leave `$BRAIN_ROOT` in the path.
    Falls back to the parent of resolve_brain_home() — same value the
    env would have if the user followed install.sh conventions.

    Bug repro before fix: out was '/cwd/$BRAIN_ROOT/memory'."""
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    out = resolve_source_path("$BRAIN_ROOT/memory")
    expected = str(resolve_brain_home().parent / "memory")
    assert out == expected
    # And the fallback MUST be an absolute path with no $VAR leftover.
    assert "$" not in out
    assert os.path.isabs(out)


def test_brace_form_brain_root_also_resolves(monkeypatch, tmp_path):
    """Both `$BRAIN_ROOT` and `${BRAIN_ROOT}` POSIX forms must work."""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "myroot"))
    out = resolve_source_path("${BRAIN_ROOT}/memory")
    assert out == str(tmp_path / "myroot" / "memory")


# --- $BRAIN_HOME resolution ------------------------------------------

def test_brain_home_set_resolves_directly(monkeypatch, tmp_path):
    """BRAIN_HOME is the direct brain dir (different from BRAIN_ROOT)."""
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "brain"))
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    out = resolve_source_path("$BRAIN_HOME")
    assert out == str(tmp_path / "brain")


def test_brain_home_unset_falls_back_to_resolve_brain_home(monkeypatch):
    """Same systemic fix applies to BRAIN_HOME as BRAIN_ROOT."""
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    out = resolve_source_path("$BRAIN_HOME/lessons")
    expected = str(resolve_brain_home()) + "/lessons"
    assert out == expected
    assert "$" not in out


# --- Unknown variables produce loud errors ---------------------------

def test_unresolved_unknown_var_raises_clear_error(monkeypatch):
    """The silent-failure mode was: '$NONEXISTENT/memory' resolved to
    a literal directory name. The fix raises ValueError with a clear
    user-actionable message instead."""
    monkeypatch.delenv("NONEXISTENT", raising=False)
    with pytest.raises(ValueError) as exc:
        resolve_source_path("$NONEXISTENT/memory")
    msg = str(exc.value).lower()
    assert "$nonexistent" in msg or "nonexistent" in msg
    assert "set" in msg  # "Set the variable in your shell..."


def test_unresolved_brace_form_var_also_raises(monkeypatch):
    monkeypatch.delenv("FOOBAR", raising=False)
    with pytest.raises(ValueError):
        resolve_source_path("${FOOBAR}/x")


# --- Standard ~/path expansion still works ---------------------------

def test_tilde_expansion_preserved(monkeypatch):
    out = resolve_source_path("~/somedir")
    assert "~" not in out
    assert out.startswith("/")


def test_known_env_var_other_than_brain_still_works(monkeypatch, tmp_path):
    """Non-brain env vars resolve via standard `os.path.expandvars`."""
    monkeypatch.setenv("CUSTOM_PATH", str(tmp_path / "stuff"))
    out = resolve_source_path("$CUSTOM_PATH/memory")
    assert out == str(tmp_path / "stuff" / "memory")


# --- SourceConfig wired through the new resolver --------------------

def test_sourceconfig_with_brain_root_unset_resolves_correctly(monkeypatch):
    """End-to-end: a SourceConfig constructed with the DEFAULT
    config path (`$BRAIN_ROOT/memory`) must work from any shell,
    regardless of whether BRAIN_ROOT is exported.

    This is the systemic bug: before the fix, `recall reindex` from
    a fresh shell reported 'Indexed 0 documents' silently."""
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    s = SourceConfig(name="brain", path="$BRAIN_ROOT/memory",
                 glob="**/*.md", frontmatter="auto-memory")
    # MUST NOT contain `$BRAIN_ROOT` anymore.
    assert "$" not in s.resolved_path
    assert os.path.isabs(s.resolved_path)


def test_sourceconfig_resolved_path_picks_up_env_changes(monkeypatch, tmp_path):
    """The `resolved_path` PROPERTY (not _resolved_path) re-resolves on
    each access. A long-running process that sees BRAIN_ROOT changed
    between calls picks up the new value."""
    s = SourceConfig(name="brain", path="$BRAIN_ROOT/memory",
                 glob="**/*.md", frontmatter="auto-memory")
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "first"))
    assert s.resolved_path == str(tmp_path / "first" / "memory")
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "second"))
    assert s.resolved_path == str(tmp_path / "second" / "memory")


def test_sourceconfig_with_unresolved_var_raises_at_resolved_path_access(monkeypatch):
    """SourceConfig construction tolerates unresolved $VARs (so config
    migration / introspection works on operator-defined env-var
    literals). But the moment a caller actually USES `.resolved_path`
    to read from disk, it raises LOUDLY — preventing the silent
    'Indexed 0 documents' mode.

    Two-stage so a misconfigured config doesn't crash `load_config`
    (e.g. for `recall doctor`), but every actual filesystem operation
    fails visibly."""
    monkeypatch.delenv("MYTYPO", raising=False)
    s = SourceConfig(name="bad", path="$MYTYPO/memory",
                     glob="**/*.md", frontmatter="auto-memory")
    # Construction succeeds — `.path` is preserved.
    assert s.path == "$MYTYPO/memory"
    # Accessing `.resolved_path` raises with a clear message.
    with pytest.raises(ValueError, match=r"unresolved environment variable"):
        _ = s.resolved_path


# --- Framework property: registry is single-source-of-truth ----------

def test_adding_a_new_brain_env_var_requires_only_resolve_brain_home_edit():
    """Documentation test: there should be exactly one place that
    decides 'where is the brain home' (resolve_brain_home in
    recall/config.py). SourceConfig delegates to it via
    resolve_source_path, which substitutes $BRAIN_HOME/$BRAIN_ROOT
    using the canonical resolver. Adding a new var is a 1-line edit.

    This test pins that property by asserting resolve_source_path
    consults resolve_brain_home (via call-site inspection)."""
    from recall import config as cfg_mod
    import inspect
    src = inspect.getsource(cfg_mod.resolve_source_path)
    assert "resolve_brain_home" in src, (
        "resolve_source_path must delegate to resolve_brain_home(); "
        "do not duplicate brain-home precedence logic in the resolver."
    )
