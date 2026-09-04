"""Health-check catalogue for the brain (S5): `recall health` / `recall doctor --health`.

Each check is a pure function of an injected `HealthEnv` — no direct
`subprocess`, `launchctl`, or socket calls — so the whole catalogue is
unit-testable without touching the developer's machine. See
`tests/recall/test_health.py` for the fixture (`make_env`) and the
per-check contract (evidence shape, PASS/WARN/FAIL/SKIP rules).

Scaffold note: the nine `check_*` functions, the `HealthReport` model
methods, and the small parsing helpers below are signature-only stubs
(`raise NotImplementedError("scaffold")`). `run_health` degrades a
crashing check to a WARN `CheckResult` rather than propagating, so the
catalogue is safe to wire up incrementally — landing one real check at a
time never breaks `recall health` for the others.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

SCHEMA_VERSION = 1

# Paths are relative to `brain_root`.
HEALTH_JSON_REL = Path("runtime/health.json")
DREAM_STATUS_REL = Path("runtime/dream_status.json")
DAEMON_SOCKET_REL = Path("runtime/recall.sock")

IMPORTS_WARN_HOURS = 2.0
IMPORTS_FAIL_HOURS = 24.0
PUSH_FAIL_HOURS = 3.0
TRACKED_FILE_FAIL_BYTES = 50 * 1024 * 1024
LOG_WARN_BYTES = 20 * 1024 * 1024
DREAM_FAIL_HOURS = 36.0
HEALTH_STALE_HOURS = 26.0

# LaunchAgent label -> short name used in `launch_agents` evidence lines.
LAUNCH_AGENT_LABELS = {
    "com.user.agent-sync": "sync",
    "com.user.agent-dream": "dream",
    "com.brainstack.claude-extras": "claude-extras",
    "com.brainstack.auto-migrate": "auto-migrate",
    "com.brainstack.recall-daemon": "recall-daemon",
}

# (returncode, stdout, stderr), keyed by argv — the injected stand-in for
# `subprocess.run`.
Runner = Callable[[list], tuple]


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome. `status` is one of PASS|WARN|FAIL|SKIP."""

    id: str
    status: str
    evidence: str
    fix: str = ""


@dataclass
class HealthEnv:
    """Everything a check needs, injected — no direct OS/network calls."""

    brain_root: Path
    home: Path
    cwd: Path
    now: datetime
    platform: str
    run: Runner
    connect_unix: Callable[[Path, float], bool]


def build_env(*, brain_root: Optional[Path] = None, cwd: Optional[Path] = None) -> HealthEnv:
    """Build a real `HealthEnv` (real subprocess runner, real clock, real
    `platform.system()`, real `socket.connect`). Scaffold: signature only.
    """
    raise NotImplementedError("scaffold")


def default_brain_root() -> Path:
    """`$BRAIN_ROOT` if set, else `resolve_brain_home().parent`. Scaffold:
    signature only."""
    raise NotImplementedError("scaffold")


# ---------------------------------------------------------------------------
# The catalogue — nine pure checks, in CHECKS order.
# ---------------------------------------------------------------------------


def check_imports_freshness(env: HealthEnv) -> CheckResult:
    """PASS/WARN/FAIL/SKIP on how stale the Claude-project memory mirror is.
    Scaffold: signature only. See tests/recall/test_health.py."""
    raise NotImplementedError("scaffold")


def check_launch_agents(env: HealthEnv) -> CheckResult:
    """SKIP off-Darwin; else compares `launchctl list` against the plists
    on disk. Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def check_brain_push(env: HealthEnv) -> CheckResult:
    """How far ahead of `origin/<upstream>` the brain repo is, and for how
    long. Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def check_large_tracked_files(env: HealthEnv) -> CheckResult:
    """Any git-tracked file over `TRACKED_FILE_FAIL_BYTES`. Scaffold:
    signature only."""
    raise NotImplementedError("scaffold")


def check_log_sizes(env: HealthEnv) -> CheckResult:
    """Any log/episodic file over `LOG_WARN_BYTES` (rolled or current).
    Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def check_dream_cycle(env: HealthEnv) -> CheckResult:
    """Freshness + `llm_errors` of the last dream cycle. Scaffold:
    signature only."""
    raise NotImplementedError("scaffold")


def check_drift(env: HealthEnv) -> CheckResult:
    """Compares the brain against the pinned brainstack repo via
    `check_freshness.detect_drift`. Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def check_auto_recall_config(env: HealthEnv) -> CheckResult:
    """Whether `env.cwd`'s resolved runtime config leaves auto-recall on.
    Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def check_daemon(env: HealthEnv) -> CheckResult:
    """Whether the recall daemon is configured and reachable. Scaffold:
    signature only."""
    raise NotImplementedError("scaffold")


CHECKS: tuple = (
    check_imports_freshness,
    check_launch_agents,
    check_brain_push,
    check_large_tracked_files,
    check_log_sizes,
    check_dream_cycle,
    check_drift,
    check_auto_recall_config,
    check_daemon,
)


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class HealthReport:
    """The full `recall health` result: one `CheckResult` per catalogue
    entry, plus the run metadata needed to render or persist it."""

    generated_at: str
    brain_root: str
    cwd: str
    checks: list

    @property
    def status(self) -> str:
        """FAIL if any check FAILed, else WARN if any WARNed, else PASS.
        Scaffold: signature only."""
        raise NotImplementedError("scaffold")

    def counts(self) -> dict:
        """`{"PASS": n, "WARN": n, "FAIL": n, "SKIP": n}`. Scaffold:
        signature only."""
        raise NotImplementedError("scaffold")

    def failures(self) -> list:
        """Checks with status == FAIL, in catalogue order. Scaffold:
        signature only."""
        raise NotImplementedError("scaffold")

    def to_dict(self) -> dict:
        """JSON-serializable payload matching the `health.json` schema.
        Scaffold: signature only."""
        raise NotImplementedError("scaffold")

    @classmethod
    def from_dict(cls, d: dict) -> "HealthReport":
        """Inverse of `to_dict`. Scaffold: signature only."""
        raise NotImplementedError("scaffold")

    def render_human(self) -> str:
        """The `== recall health ==` text block. Scaffold: signature
        only."""
        raise NotImplementedError("scaffold")


def run_health(env: HealthEnv) -> HealthReport:
    """Run every check in `CHECKS`. A check that raises degrades to a WARN
    `CheckResult` (id = the check function's `__name__`, evidence =
    `f"check crashed: {exc!r}"`) instead of taking the whole report down.
    Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def write_report(report: HealthReport, path: Path) -> None:
    """Atomic write (tmp + `os.replace`), creating `path.parent` if
    missing. Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def load_report(
    path: Path, *, now: Optional[datetime] = None, max_age_hours: float = HEALTH_STALE_HOURS
) -> Optional[HealthReport]:
    """Load `path` and return `None` if it's missing, corrupt, or older
    than `max_age_hours`. Scaffold: signature only."""
    raise NotImplementedError("scaffold")


# ---------------------------------------------------------------------------
# Small parsing / filesystem helpers shared by two or more checks.
# ---------------------------------------------------------------------------


def _sync_log_remote_error(text: str) -> Optional[str]:
    """The `remote: error: ...` line from the most recent sync run in
    `sync.log`, or `None`. Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def _parse_launchctl_list(stdout: str) -> dict:
    """Parse `launchctl list` output into `{label: (pid, last_exit)}`.
    Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def _claude_project_memory_files(home: Path) -> list:
    """Every `~/.claude/projects/<slug>/memory/*` file, skipping projects
    whose `memory/` is a symlink into the brain itself. Scaffold:
    signature only."""
    raise NotImplementedError("scaffold")


def _newest_mtime(paths) -> Optional[float]:
    """The newest `st_mtime` across `paths`, or `None` if empty/missing.
    Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def _rolled_and_current(path: Path) -> list:
    """`<stem>*<suffix>` siblings of `path` (rolled + current). Scaffold:
    signature only."""
    raise NotImplementedError("scaffold")


def _load_runtime_config_at(cwd: Path):
    """`RuntimeConfig.load()` as resolved from `cwd` (chdir guarded by
    try/finally). Scaffold: signature only."""
    raise NotImplementedError("scaffold")


def _human_age(hours: float) -> str:
    """Render an hour count as `"3h ago"` / `"5d ago"` style text.
    Scaffold: signature only."""
    raise NotImplementedError("scaffold")
