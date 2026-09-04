"""Provider CLI lookup beyond `shutil.which` — S5 slice C, requirement R6.

`ClaudeCodeProvider.is_available()` calls `shutil.which("claude")` and
gives up. Under launchd that PATH is `/opt/homebrew/bin:/usr/local/bin:
/usr/bin:/bin:...`, which does not contain `~/.local/bin`, so the nightly
dream cycle resolved no provider at all and logged
`llm_errors=provider_unavailable=3` — while the identical command
succeeded from the user's terminal. Two failures compounded: the lookup
was too narrow, and the skip reason ("claude CLI not on PATH — install
Claude Code") sent the user to reinstall a CLI they already had.

This file pins the fix:

  - `find_cli(name, home=...)` prefers PATH, then falls back to the usual
    per-user bin dirs, then to the newest nvm node version.
  - The unavailable reason names the binary, the PATH it searched, and the
    directories it searched — enough to diagnose without reading source.
  - That longer reason still contains the substring
    `LLMExtractor._classify_error` maps to `provider_unavailable`, so the
    dream-log error tag does not silently become `other`.
  - `resolve_provider()` end-to-end: an empty PATH plus a `claude` in
    `~/.local/bin` resolves, and argv[0] becomes that resolved path.

`FALLBACK_BIN_DIRS` is monkeypatched to the home-relative entries in the
provider tests so a real `/opt/homebrew/bin/claude` on the developer's
machine cannot make an unavailability assertion pass or fail by accident.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

# A name that cannot exist in a real bin dir, so the PATH-preference and
# nvm-ordering tests never see the developer's machine.
FAKE_CLI = "brainstack-fake-cli"

# Only home-relative dirs: keeps the reason-text tests hermetic.
HOME_ONLY_DIRS = ("~/.local/bin", "~/.claude/local")


@pytest.fixture
def base_mod():
    from llm_providers import base
    return base


@pytest.fixture
def providers_mod():
    import llm_providers
    return llm_providers


def _stub_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _clean_provider_env(monkeypatch, home: Path) -> None:
    """No PATH, no provider override, HOME pointed at the tmp tree."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("BRAIN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("BRAIN_CONFIG", raising=False)


# ---------------------------------------------------------------------------
# find_cli
# ---------------------------------------------------------------------------


def test_find_cli_prefers_path(base_mod, tmp_path, monkeypatch):
    """A binary on PATH wins, even when a fallback dir also has one. The
    user's shell and the scheduled job must agree on which one runs."""
    home = tmp_path / "home"
    on_path = _stub_exe(tmp_path / "real-bin" / FAKE_CLI)
    _stub_exe(home / ".local" / "bin" / FAKE_CLI)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(on_path.parent))

    found, searched = base_mod.find_cli(FAKE_CLI, home=home)
    assert found == str(on_path), (
        f"expected the PATH copy {on_path}, got {found!r} (searched {searched})"
    )


def test_find_cli_falls_back_to_local_bin(base_mod, tmp_path, monkeypatch):
    """The launchd case: nothing on PATH, the CLI sitting in ~/.local/bin."""
    home = tmp_path / "home"
    stub = _stub_exe(home / ".local" / "bin" / FAKE_CLI)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")

    found, searched = base_mod.find_cli(FAKE_CLI, home=home)
    assert found == str(stub), (
        f"~/.local/bin was not searched; got {found!r} (searched {searched})"
    )
    # The searched list is user-facing (it lands in the skip reason), so the
    # entries are expanded absolute paths, not a literal `~`.
    assert str(home / ".local" / "bin") in searched, searched


def test_find_cli_ignores_non_executable_file(base_mod, tmp_path, monkeypatch):
    """A stray non-executable file of the same name is not a CLI. Returning
    it would turn a clear "not installed" into a confusing exec failure."""
    home = tmp_path / "home"
    dud = home / ".local" / "bin" / FAKE_CLI
    dud.parent.mkdir(parents=True)
    dud.write_text("not executable\n")
    dud.chmod(0o644)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")

    found, _ = base_mod.find_cli(FAKE_CLI, home=home)
    assert found is None, f"returned a non-executable file: {found!r}"


def test_find_cli_picks_newest_nvm_version(base_mod, tmp_path, monkeypatch):
    """nvm installs put the CLI under a versioned dir. Ordering must be by
    parsed version, not string sort, or v9 beats v20."""
    home = tmp_path / "home"
    nvm = home / ".nvm" / "versions" / "node"
    for version in ("v9.11.2", "v18.20.0", "v20.20.0"):
        _stub_exe(nvm / version / "bin" / FAKE_CLI)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")

    dirs = base_mod.nvm_bin_dirs(home)
    assert dirs[:3] == [
        str(nvm / "v20.20.0" / "bin"),
        str(nvm / "v18.20.0" / "bin"),
        str(nvm / "v9.11.2" / "bin"),
    ], f"nvm dirs must be newest-first by parsed version; got {dirs}"

    found, _ = base_mod.find_cli(FAKE_CLI, home=home)
    assert found == str(nvm / "v20.20.0" / "bin" / FAKE_CLI), (
        f"expected the newest node version's copy; got {found!r}"
    )


# ---------------------------------------------------------------------------
# Unavailable reasons
# ---------------------------------------------------------------------------


def test_claude_unavailable_reason_names_binary_and_dirs(
    providers_mod, base_mod, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    _clean_provider_env(monkeypatch, home)
    monkeypatch.setattr(base_mod, "FALLBACK_BIN_DIRS", HOME_ONLY_DIRS)

    ok, reason = providers_mod.PROVIDERS["claude-code"].is_available()
    assert ok is False, f"claude should be unavailable here; reason={reason!r}"
    assert "claude" in reason
    assert "PATH=" in reason, (
        f"the reason must quote the PATH that was searched, since the "
        f"scheduled job's PATH differs from the user's shell: {reason!r}"
    )
    assert str(home / ".local" / "bin") in reason, (
        f"the reason must name the fallback dirs it searched: {reason!r}"
    )


def test_codex_unavailable_reason_names_binary_and_dirs(
    providers_mod, base_mod, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    _clean_provider_env(monkeypatch, home)
    monkeypatch.setattr(base_mod, "FALLBACK_BIN_DIRS", HOME_ONLY_DIRS)

    ok, reason = providers_mod.PROVIDERS["codex"].is_available()
    assert ok is False, f"codex should be unavailable here; reason={reason!r}"
    assert "codex" in reason
    assert "PATH=" in reason, reason
    assert str(home / ".local" / "bin") in reason, reason


def test_extractor_classifier_maps_new_reason_to_provider_unavailable(
    providers_mod, base_mod, tmp_path, monkeypatch
):
    """The longer reason must keep the substring `_classify_error` matches.
    Lose it and the dream-log tag degrades to `llm_errors=other=3`, which
    is what made this failure invisible for weeks."""
    import llm_extractor

    home = tmp_path / "home"
    home.mkdir()
    _clean_provider_env(monkeypatch, home)
    monkeypatch.setattr(base_mod, "FALLBACK_BIN_DIRS", HOME_ONLY_DIRS)

    _, claude_reason = providers_mod.PROVIDERS["claude-code"].is_available()
    _, codex_reason = providers_mod.PROVIDERS["codex"].is_available()

    classify = llm_extractor.LLMExtractor._classify_error
    for reason in (claude_reason, codex_reason):
        # `_classify_error` tests "schema" / "validation" / "timeout" /
        # "max_budget" BEFORE the provider check, so any of those words in
        # the reason would hijack the tag.
        low = reason.lower()
        for hijacker in ("schema", "validation", "timeout", "max_budget"):
            assert hijacker not in low, (
                f"the reason contains {hijacker!r}, which _classify_error "
                f"matches first: {reason!r}"
            )
        assert classify(Exception(reason)) == "provider_unavailable", (
            f"reason no longer classifies as provider_unavailable: {reason!r}"
        )

    aggregated = base_mod.ProviderNotAvailable(
        {"claude-code": claude_reason, "codex": codex_reason}
    )
    assert classify(aggregated) == "provider_unavailable"


# ---------------------------------------------------------------------------
# End-to-end resolution
# ---------------------------------------------------------------------------


def test_resolve_provider_uses_fallback_dir_binary(
    providers_mod, base_mod, tmp_path, monkeypatch
):
    """The launchd scenario end to end: empty PATH, `claude` in
    ~/.local/bin. The provider resolves, and argv[0] is that resolved path
    rather than the bare name (which the job could not exec)."""
    home = tmp_path / "home"
    stub = _stub_exe(home / ".local" / "bin" / "claude")
    _clean_provider_env(monkeypatch, home)
    monkeypatch.setattr(base_mod, "FALLBACK_BIN_DIRS", HOME_ONLY_DIRS)

    # Fresh instances: PROVIDERS holds module-level singletons whose
    # resolved-binary state would otherwise leak between tests.
    from llm_providers.claude_code import ClaudeCodeProvider
    from llm_providers.codex import CodexProvider
    monkeypatch.setattr(providers_mod, "PROVIDERS", {
        "claude-code": ClaudeCodeProvider(),
        "codex": CodexProvider(),
    })

    provider = providers_mod.resolve_provider()
    assert provider.name == "claude-code", (
        f"expected the claude provider to resolve from ~/.local/bin, got "
        f"{provider.name!r}"
    )

    cmd = provider._build_cmd(provider.default_model, None, 5.0)
    assert cmd[0].endswith("claude"), f"argv[0]={cmd[0]!r}"
    assert cmd[0] == str(stub), (
        f"argv[0] must be the resolved absolute path so a job with a minimal "
        f"PATH can exec it; got {cmd[0]!r}"
    )
