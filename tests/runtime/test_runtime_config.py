"""S1 — layered `[tool.recall.runtime]` config precedence + shared path helpers.

Today `RuntimeConfig.load()` picks ONE file and reads every key from it. A
project `pyproject.toml` that carries `[tool.recall.runtime]` for a single
key (say `log_dir`) therefore silently drops every key the user set in their
global `~/.agent/runtime/pyproject.toml` — `enable_auto_recall`,
`auto_recall_min_score`, the lot. That is exactly what the brainstack repo
itself does to anyone working inside a brainstack worktree.

S1 replaces "one file wins" with a per-key merge over an ordered layer list:

    $RECALL_RUNTIME_CONFIG  >  ./pyproject.toml  >  $BRAIN_ROOT/runtime/pyproject.toml  >  defaults

First layer that SETS a key wins that key; `budget` merges per sub-key; a
malformed value falls through to the next layer rather than crashing the hook.

Also covered here (slice A owns them): `recall.config.daemon_socket_path()`
and `recall.config.brain_root()`, the shared path helpers the daemon, the
hook and the CLI all resolve through, plus the two `RuntimeConfig`
properties that sit on top of them.

Every test is hermetic: `runtime_home` pins `BRAIN_ROOT`, `HOME`,
`Path.home` and the cwd at tmp dirs, and clears `RECALL_RUNTIME_CONFIG`,
`RECALL_DAEMON_SOCKET` and `BRAIN_HOME`. No test reads the developer's real
`~/.agent`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from recall import config as recall_config
from runtime.adapters.claude_code.config import RuntimeConfig

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# tests/runtime/test_runtime_config.py -> tests/runtime -> tests -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@dataclass
class RuntimeHome:
    """Handles on the fake $HOME / $BRAIN_ROOT / cwd built by `runtime_home`."""

    home: Path
    brain_root: Path
    project: Path
    global_path: Path
    cwd_path: Path
    env_config_path: Path
    monkeypatch: Any

    def write_global(self, body: str) -> Path:
        """Write $BRAIN_ROOT/runtime/pyproject.toml (the lowest file layer)."""
        self.global_path.write_text(body, encoding="utf-8")
        return self.global_path

    def write_cwd(self, body: str) -> Path:
        """Write ./pyproject.toml (the middle file layer)."""
        self.cwd_path.write_text(body, encoding="utf-8")
        return self.cwd_path

    def write_env_config(self, body: str) -> Path:
        """Write a file and point $RECALL_RUNTIME_CONFIG at it (top layer)."""
        self.env_config_path.write_text(body, encoding="utf-8")
        self.monkeypatch.setenv("RECALL_RUNTIME_CONFIG", str(self.env_config_path))
        return self.env_config_path

    def drop_global(self) -> None:
        self.global_path.unlink(missing_ok=True)


@pytest.fixture
def runtime_home(tmp_path: Path, monkeypatch) -> RuntimeHome:
    """A fake home + brain root + project cwd, fully isolated from the machine.

    `_discover_layers` resolves the global file through $BRAIN_ROOT and falls
    back to `~/.agent/runtime/pyproject.toml`, which reads $HOME (and
    USERPROFILE on Windows) rather than `Path.home()`. Pin all three, plus
    `Path.home` itself, or the test silently reads the developer's real
    ~/.agent and passes locally while failing on a clean runner. Same idiom as
    tests/runtime/test_auto_recall.py::TestPyprojectDiscovery.
    """
    home = tmp_path / "home"
    brain_root = home / ".agent"
    (brain_root / "runtime").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("BRAIN_ROOT", str(brain_root))
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)
    # The root conftest sets RECALL_DAEMON_SOCKET so no test touches the
    # developer's live socket; these tests assert on socket resolution, so
    # they must own the variable themselves.
    monkeypatch.delenv("RECALL_DAEMON_SOCKET", raising=False)
    monkeypatch.chdir(project)

    global_path = brain_root / "runtime" / "pyproject.toml"
    global_path.write_text("[tool.recall.runtime]\n", encoding="utf-8")

    return RuntimeHome(
        home=home,
        brain_root=brain_root,
        project=project,
        global_path=global_path,
        cwd_path=project / "pyproject.toml",
        env_config_path=tmp_path / "explicit-runtime.toml",
        monkeypatch=monkeypatch,
    )


def _resolved(p: Path | str | None) -> Path | None:
    """Compare paths through resolve(): macOS tmp dirs live behind /private."""
    return None if p is None else Path(p).resolve()


# ---------------------------------------------------------------------------
# per-key layered merge
# ---------------------------------------------------------------------------


class TestLayeredMerge:
    def test_cwd_partial_section_does_not_shadow_global_keys(self, runtime_home):
        """The bug S1 exists to kill: a project that sets ONE key must not
        reset every other key to the dataclass default."""
        runtime_home.write_global(
            "[tool.recall.runtime]\n"
            "enable_auto_recall = true\n"
            "auto_recall_min_score = 0.70\n"
        )
        runtime_home.write_cwd(
            "[tool.recall.runtime]\n"
            f'log_dir = "{runtime_home.project / "logs"}"\n'
        )

        cfg = RuntimeConfig.load()

        assert cfg.enable_auto_recall is True
        assert cfg.auto_recall_min_score == pytest.approx(0.70)
        assert cfg.log_dir == runtime_home.project / "logs"

    def test_loading_from_brainstack_worktree_returns_global_values(
        self, runtime_home, monkeypatch
    ):
        """Working inside the brainstack repo must not disable the user's own
        auto-recall. Requires the repo's own live runtime section to be gone."""
        repo_pyproject = _REPO_ROOT / "pyproject.toml"
        with repo_pyproject.open("rb") as f:
            repo_data = tomllib.load(f)
        repo_section = repo_data.get("tool", {}).get("recall", {}).get("runtime", {})
        assert repo_section == {}, (
            f"{repo_pyproject} still carries a live [tool.recall.runtime] table "
            "— it shadows the user's global ~/.agent config for anyone working "
            "in a brainstack worktree. Move the example into docs/runtime.md."
        )

        runtime_home.write_global(
            "[tool.recall.runtime]\n"
            "enable_auto_recall = true\n"
            "auto_recall_min_score = 0.70\n"
        )
        monkeypatch.chdir(_REPO_ROOT)

        cfg = RuntimeConfig.load()

        assert cfg.enable_auto_recall is True
        assert cfg.auto_recall_min_score == pytest.approx(0.70)
        assert _resolved(cfg.config_path) == _resolved(runtime_home.global_path)

    def test_env_override_layers_over_cwd_and_global(self, runtime_home):
        """$RECALL_RUNTIME_CONFIG is a layer, not a replacement: it wins the
        keys it sets and the lower layers still supply the rest."""
        runtime_home.write_env_config(
            "[tool.recall.runtime]\nauto_recall_k = 1\n"
        )
        runtime_home.write_cwd(
            "[tool.recall.runtime]\nauto_recall_k = 2\nauto_recall_min_chars = 20\n"
        )
        runtime_home.write_global(
            "[tool.recall.runtime]\n"
            "auto_recall_k = 3\n"
            "auto_recall_min_chars = 30\n"
            "enable_auto_recall = true\n"
            "auto_recall_budget_tokens = 77\n"
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_k == 1          # env
        assert cfg.auto_recall_min_chars == 20  # cwd
        assert cfg.enable_auto_recall is True   # global
        assert cfg.auto_recall_budget_tokens == 77  # global

    def test_cwd_key_beats_global_key(self, runtime_home):
        runtime_home.write_global("[tool.recall.runtime]\nauto_recall_min_score = 0.70\n")
        runtime_home.write_cwd("[tool.recall.runtime]\nauto_recall_min_score = 0.55\n")

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_min_score == pytest.approx(0.55)

    def test_budget_subtable_merges_per_key(self, runtime_home):
        """`budget` merges per sub-key across layers, then per-key defaults
        fill the rest — a cwd override of `hot` must not drop the global's
        `retrieved` nor the built-in `claude_md`/`scratchpad`."""
        runtime_home.write_global(
            "[tool.recall.runtime.budget]\nhot = 999\nretrieved = 222\n"
        )
        runtime_home.write_cwd(
            "[tool.recall.runtime.budget]\nhot = 111\n"
        )

        cfg = RuntimeConfig.load()

        assert cfg.budgets["hot"] == 111        # cwd
        assert cfg.budgets["retrieved"] == 222  # global
        assert cfg.budgets["claude_md"] == 4000    # dataclass default
        assert cfg.budgets["scratchpad"] == 10000  # dataclass default

    def test_malformed_value_falls_to_next_layer(self, runtime_home):
        """Lenient coercion per key. A junk value in a high layer must not
        crash the hook and must not win — the next layer supplies the key."""
        runtime_home.write_global(
            "[tool.recall.runtime]\n"
            "auto_recall_k = 9\n"
            "auto_recall_min_score = 0.42\n"
            "enable_auto_recall = false\n"
            "auto_recall_timeout_ms = 1234\n"
        )
        runtime_home.write_cwd(
            "[tool.recall.runtime]\n"
            'auto_recall_k = "banana"\n'
            'auto_recall_min_score = "high"\n'
            'enable_auto_recall = "maybe"\n'
            'auto_recall_timeout_ms = ["nope"]\n'
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_k == 9
        assert cfg.auto_recall_min_score == pytest.approx(0.42)
        assert cfg.enable_auto_recall is False
        assert cfg.auto_recall_timeout_ms == 1234

    def test_malformed_value_in_last_layer_falls_to_dataclass_default(
        self, runtime_home
    ):
        runtime_home.write_global('[tool.recall.runtime]\nauto_recall_k = "banana"\n')

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_k == 5

    def test_explicit_config_path_is_single_file(self, runtime_home):
        """`load(config_path=...)` stays a single-file read — no layering, no
        global merge. test_adapter_hooks.py relies on this."""
        explicit = runtime_home.env_config_path
        explicit.write_text(
            "[tool.recall.runtime]\n"
            f'log_dir = "{runtime_home.project / "explicit-logs"}"\n',
            encoding="utf-8",
        )
        runtime_home.write_global("[tool.recall.runtime]\nenable_auto_recall = true\n")
        runtime_home.write_cwd("[tool.recall.runtime]\nauto_recall_k = 42\n")

        cfg = RuntimeConfig.load(config_path=explicit)

        assert cfg.log_dir == runtime_home.project / "explicit-logs"
        assert cfg.enable_auto_recall is False  # global NOT consulted
        assert cfg.auto_recall_k == 5           # cwd NOT consulted
        assert _resolved(cfg.config_path) == _resolved(explicit)
        layers = [_resolved(p) for p in cfg.config_layers]
        assert _resolved(runtime_home.global_path) not in layers
        assert _resolved(runtime_home.cwd_path) not in layers

    def test_config_layers_lists_files_high_to_low(self, runtime_home):
        env_path = runtime_home.write_env_config(
            "[tool.recall.runtime]\nauto_recall_k = 1\n"
        )
        cwd_path = runtime_home.write_cwd("[tool.recall.runtime]\nauto_recall_k = 2\n")
        global_path = runtime_home.write_global(
            "[tool.recall.runtime]\nauto_recall_k = 3\n"
        )

        cfg = RuntimeConfig.load()

        assert [_resolved(p) for p in cfg.config_layers] == [
            _resolved(env_path),
            _resolved(cwd_path),
            _resolved(global_path),
        ]
        assert cfg.auto_recall_k == 1
        assert _resolved(cfg.config_path) == _resolved(env_path)

    def test_config_path_is_highest_layer_with_a_section(self, runtime_home):
        """A cwd pyproject with no `[tool.recall.runtime]` table contributes
        nothing, so `config_path` points at the global file that did."""
        runtime_home.write_cwd('[build-system]\nrequires = ["hatchling"]\n')
        runtime_home.write_global("[tool.recall.runtime]\nenable_auto_recall = true\n")

        cfg = RuntimeConfig.load()

        assert cfg.enable_auto_recall is True
        assert _resolved(cfg.config_path) == _resolved(runtime_home.global_path)


# ---------------------------------------------------------------------------
# new keys + defaults
# ---------------------------------------------------------------------------


class TestNewDefaults:
    def test_new_defaults(self, runtime_home):
        """With no file anywhere, load() yields the S1 dataclass defaults."""
        runtime_home.drop_global()

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_timeout_ms == 1500
        assert cfg.auto_recall_daemon_budget_ms == 800
        assert cfg.auto_recall_daemon_socket == "$BRAIN_ROOT/runtime/recall.sock"
        assert cfg.auto_recall_min_rerank is None
        assert cfg.auto_recall_dedup is True

    def test_new_defaults_on_bare_dataclass(self):
        cfg = RuntimeConfig()

        assert cfg.auto_recall_timeout_ms == 1500
        assert cfg.auto_recall_daemon_budget_ms == 800
        assert cfg.auto_recall_daemon_socket == "$BRAIN_ROOT/runtime/recall.sock"
        assert cfg.auto_recall_min_rerank is None
        assert cfg.auto_recall_dedup is True

    def test_existing_defaults_unchanged(self, runtime_home):
        runtime_home.drop_global()

        cfg = RuntimeConfig.load()

        assert cfg.enable_auto_recall is False
        assert cfg.auto_recall_k == 5
        assert cfg.auto_recall_budget_tokens == 1500
        assert cfg.auto_recall_min_chars == 8
        assert cfg.auto_recall_min_score == pytest.approx(0.0)

    def test_new_keys_are_readable_from_a_layer(self, runtime_home):
        runtime_home.write_global(
            "[tool.recall.runtime]\n"
            "auto_recall_timeout_ms = 2500\n"
            "auto_recall_daemon_budget_ms = 400\n"
            'auto_recall_daemon_socket = "/tmp/custom-recall.sock"\n'
            "auto_recall_min_rerank = 0.35\n"
            "auto_recall_dedup = false\n"
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_timeout_ms == 2500
        assert cfg.auto_recall_daemon_budget_ms == 400
        assert cfg.auto_recall_daemon_socket == "/tmp/custom-recall.sock"
        assert cfg.auto_recall_min_rerank == pytest.approx(0.35)
        assert cfg.auto_recall_dedup is False

    def test_min_rerank_negative_float_from_cwd_pyproject(self, runtime_home):
        """Cross-encoder scores are raw logits (mostly negative), so a
        calibrated threshold is commonly negative. A cwd pyproject.toml
        setting `auto_recall_min_rerank = -1.5` must load as -1.5, not be
        rejected as falsy/invalid."""
        runtime_home.write_cwd(
            "[tool.recall.runtime]\nauto_recall_min_rerank = -1.5\n"
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_min_rerank == pytest.approx(-1.5)

    def test_min_rerank_string_none_coerces_to_none(self, runtime_home):
        runtime_home.write_global(
            '[tool.recall.runtime]\nauto_recall_min_rerank = "none"\n'
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_min_rerank is None

    def test_min_rerank_string_null_coerces_to_none(self, runtime_home):
        runtime_home.write_global(
            '[tool.recall.runtime]\nauto_recall_min_rerank = "null"\n'
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_min_rerank is None

    def test_min_rerank_numeric_string_coerces_to_float(self, runtime_home):
        runtime_home.write_global(
            '[tool.recall.runtime]\nauto_recall_min_rerank = "-1.9547"\n'
        )

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_min_rerank == pytest.approx(-1.9547)

    def test_min_rerank_absent_stays_none(self, runtime_home):
        runtime_home.write_global("[tool.recall.runtime]\nauto_recall_k = 3\n")

        cfg = RuntimeConfig.load()

        assert cfg.auto_recall_min_rerank is None


# ---------------------------------------------------------------------------
# RuntimeConfig properties built on the shared helpers
# ---------------------------------------------------------------------------


class TestRuntimeConfigProperties:
    def test_injected_dir_is_under_log_dir(self, tmp_path):
        cfg = RuntimeConfig(log_dir=tmp_path / "logs")

        assert cfg.injected_dir == tmp_path / "logs" / "injected"

    def test_daemon_socket_path_property_expands_brain_root_literal(
        self, runtime_home
    ):
        cfg = RuntimeConfig(auto_recall_daemon_socket="$BRAIN_ROOT/runtime/recall.sock")

        assert _resolved(cfg.daemon_socket_path) == _resolved(
            runtime_home.brain_root / "runtime" / "recall.sock"
        )

    def test_daemon_socket_path_property_expands_tilde(self, runtime_home):
        cfg = RuntimeConfig(auto_recall_daemon_socket="~/custom/recall.sock")

        assert _resolved(cfg.daemon_socket_path) == _resolved(
            runtime_home.home / "custom" / "recall.sock"
        )

    def test_daemon_socket_path_property_honours_env_override(self, runtime_home):
        runtime_home.monkeypatch.setenv(
            "RECALL_DAEMON_SOCKET", str(runtime_home.home / "env.sock")
        )
        cfg = RuntimeConfig(auto_recall_daemon_socket="$BRAIN_ROOT/runtime/recall.sock")

        assert _resolved(cfg.daemon_socket_path) == _resolved(
            runtime_home.home / "env.sock"
        )


# ---------------------------------------------------------------------------
# recall.config.daemon_socket_path — one resolution order for hook, CLI, daemon
# ---------------------------------------------------------------------------


class TestDaemonSocketPathResolution:
    def test_env_wins_when_raw_is_none(self, runtime_home):
        runtime_home.monkeypatch.setenv(
            "RECALL_DAEMON_SOCKET", str(runtime_home.home / "env.sock")
        )

        got = recall_config.daemon_socket_path()

        assert _resolved(got) == _resolved(runtime_home.home / "env.sock")

    def test_env_wins_over_the_default_brain_root_literal(self, runtime_home):
        """The config default is the `$BRAIN_ROOT` literal, i.e. "unset". It
        must not stop `RECALL_DAEMON_SOCKET` from taking effect."""
        runtime_home.monkeypatch.setenv(
            "RECALL_DAEMON_SOCKET", str(runtime_home.home / "env.sock")
        )

        got = recall_config.daemon_socket_path("$BRAIN_ROOT/runtime/recall.sock")

        assert _resolved(got) == _resolved(runtime_home.home / "env.sock")

    def test_env_overrides_an_explicit_raw_path(self, runtime_home):
        """RECALL_DAEMON_SOCKET is documented as an override, and the root
        conftest guard relies on it beating whatever a config supplies."""
        runtime_home.monkeypatch.setenv(
            "RECALL_DAEMON_SOCKET", str(runtime_home.home / "env.sock")
        )

        got = recall_config.daemon_socket_path(str(runtime_home.home / "explicit.sock"))

        assert _resolved(got) == _resolved(runtime_home.home / "env.sock")

    def test_raw_wins_over_brain_root_default(self, runtime_home):
        got = recall_config.daemon_socket_path(str(runtime_home.home / "explicit.sock"))

        assert _resolved(got) == _resolved(runtime_home.home / "explicit.sock")

    def test_raw_expands_brain_root_literal(self, runtime_home):
        got = recall_config.daemon_socket_path("$BRAIN_ROOT/runtime/recall.sock")

        assert _resolved(got) == _resolved(
            runtime_home.brain_root / "runtime" / "recall.sock"
        )

    def test_raw_expands_tilde(self, runtime_home):
        got = recall_config.daemon_socket_path("~/sockets/recall.sock")

        assert _resolved(got) == _resolved(runtime_home.home / "sockets" / "recall.sock")

    def test_falls_back_to_brain_root(self, runtime_home):
        got = recall_config.daemon_socket_path()

        assert _resolved(got) == _resolved(
            runtime_home.brain_root / "runtime" / "recall.sock"
        )

    def test_falls_back_to_brain_home_parent(self, runtime_home, tmp_path):
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "agent" / "memory"))

        got = recall_config.daemon_socket_path()

        assert _resolved(got) == _resolved(
            tmp_path / "agent" / "runtime" / "recall.sock"
        )

    def test_falls_back_to_dot_agent(self, runtime_home):
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.delenv("BRAIN_HOME", raising=False)

        got = recall_config.daemon_socket_path()

        assert _resolved(got) == _resolved(
            runtime_home.home / ".agent" / "runtime" / "recall.sock"
        )

    def test_default_raw_literal_expands_via_brain_root_when_unset(self, runtime_home):
        """Regression: the runtime config's default `raw` is the literal
        `"$BRAIN_ROOT/runtime/recall.sock"`. Hooks do not export
        `$BRAIN_ROOT` (the normal Claude Code hook environment), so this
        must expand `$BRAIN_ROOT` via `recall.config.brain_root()` — which
        falls back through an on-disk `~/.agent/memory` to `~/.agent` —
        rather than leaving the literal `"$BRAIN_ROOT"` text in the path."""
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.delenv("BRAIN_HOME", raising=False)
        (runtime_home.home / ".agent" / "memory").mkdir(parents=True, exist_ok=True)

        got = recall_config.daemon_socket_path("$BRAIN_ROOT/runtime/recall.sock")

        assert "BRAIN_ROOT" not in str(got)
        assert _resolved(got) == _resolved(
            runtime_home.home / ".agent" / "runtime" / "recall.sock"
        )

    def test_default_raw_literal_expands_via_brain_home_parent(self, runtime_home, tmp_path):
        """Same default raw literal, but `$BRAIN_HOME` is set (no
        `$BRAIN_ROOT`): `brain_root()` resolves to `$BRAIN_HOME`'s parent."""
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "elsewhere" / "memory"))

        got = recall_config.daemon_socket_path("$BRAIN_ROOT/runtime/recall.sock")

        assert _resolved(got) == _resolved(
            tmp_path / "elsewhere" / "runtime" / "recall.sock"
        )

    def test_runtime_config_property_matches_default_raw_fallback(self, runtime_home):
        """`RuntimeConfig.daemon_socket_path` delegates to
        `recall.config.daemon_socket_path`, so it must resolve the same
        `~/.agent` fallback as the module-level function for the dataclass's
        own default literal."""
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.delenv("BRAIN_HOME", raising=False)
        (runtime_home.home / ".agent" / "memory").mkdir(parents=True, exist_ok=True)

        cfg = RuntimeConfig()

        assert _resolved(cfg.daemon_socket_path) == _resolved(
            recall_config.daemon_socket_path(cfg.auto_recall_daemon_socket)
        )
        assert _resolved(cfg.daemon_socket_path) == _resolved(
            runtime_home.home / ".agent" / "runtime" / "recall.sock"
        )


# ---------------------------------------------------------------------------
# recall.config.brain_root
# ---------------------------------------------------------------------------


class TestBrainRootResolution:
    def test_env_brain_root_wins(self, runtime_home):
        assert _resolved(recall_config.brain_root()) == _resolved(
            runtime_home.brain_root
        )

    def test_brain_root_expands_tilde(self, runtime_home):
        runtime_home.monkeypatch.setenv("BRAIN_ROOT", "~/.agent")

        assert _resolved(recall_config.brain_root()) == _resolved(
            runtime_home.home / ".agent"
        )

    def test_parent_of_brain_home_when_it_ends_in_memory(self, runtime_home, tmp_path):
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "agent" / "memory"))

        assert _resolved(recall_config.brain_root()) == _resolved(tmp_path / "agent")

    def test_brain_home_itself_when_it_does_not_end_in_memory(
        self, runtime_home, tmp_path
    ):
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "elsewhere" / "brain"))

        assert _resolved(recall_config.brain_root()) == _resolved(
            tmp_path / "elsewhere" / "brain"
        )

    def test_dot_agent_convention_when_no_env_is_set(self, runtime_home):
        """No BRAIN_ROOT, no BRAIN_HOME, but ~/.agent/memory exists on disk:
        resolve_brain_home() returns it, so brain_root() is its parent."""
        runtime_home.monkeypatch.delenv("BRAIN_ROOT", raising=False)
        runtime_home.monkeypatch.delenv("BRAIN_HOME", raising=False)
        (runtime_home.home / ".agent" / "memory").mkdir(parents=True, exist_ok=True)

        assert _resolved(recall_config.brain_root()) == _resolved(
            runtime_home.home / ".agent"
        )
