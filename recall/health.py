"""Health-check catalogue for the brain (S5): `recall health` / `recall doctor --health`.

Each check is a pure function of an injected `HealthEnv` — no direct
`subprocess`, `launchctl`, or socket calls — so the whole catalogue is
unit-testable without touching the developer's machine. See
`tests/recall/test_health.py` for the fixture (`make_env`) and the
per-check contract (evidence shape, PASS/WARN/FAIL/SKIP rules).

`run_health` degrades a crashing check to a WARN `CheckResult` rather than
propagating, so one broken check never takes `recall health` (or the
SessionStart banner that reads its cached output) down with it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from recall.fsutil import atomic_write_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback, matches runtime.adapters config
    import tomli as tomllib  # type: ignore[no-redef]

SCHEMA_VERSION = 1

# Paths are relative to `brain_root`.
DREAM_STATUS_REL = Path("runtime/dream_status.json")
DAEMON_SOCKET_REL = Path("runtime/recall.sock")

IMPORTS_WARN_HOURS = 2.0
IMPORTS_FAIL_HOURS = 24.0
PUSH_FAIL_HOURS = 3.0
TRACKED_FILE_FAIL_BYTES = 50 * 1024 * 1024
LOG_WARN_BYTES = 20 * 1024 * 1024
DREAM_FAIL_HOURS = 36.0
HEALTH_STALE_HOURS = 26.0
# A local AF_UNIX connect either completes in microseconds or the socket is
# not being served. 0.5 s only bought a longer stall on the failure path.
DAEMON_CONNECT_TIMEOUT_S = 0.15
# How much of the (unbounded, append-only) sync.log the remote-error scan
# looks at. Matches the `[-400:]` slice `_sync_log_remote_error` applies.
_SYNC_LOG_TAIL_LINES = 400

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

_MB = 1024 * 1024

# One sync run in `sync.log` ends with one of these. Used to scope a
# backward scan to the most recent run only (mirrors the same tuple in
# `agent/tools/render_pending_summary.py`).
_RUN_TERMINAL_MARKERS: tuple = (
    "sync: pushed", "sync: no changes", "refusing to push",
    "commit blocked", "push failed", "skipping push",
)

# Git's own failure lines, matched at the START of a log line (after the
# `date -u` prefix sync.sh adds), ranked by how much they explain:
#   0 — GitHub rejected the push and said why.
#   1 — git itself gave up; the first `fatal:` names the cause, the ones
#       after it are boilerplate ("Could not read from remote repository").
#   2 — transport/auth failures and git's summary line, which carry no
#       cause of their own but still beat saying nothing.
# Client-side failures (dead remote, bad key, DNS) never print
# `remote: error:`, so matching that alone left every one of them silent.
_GIT_ERROR_PREFIXES: tuple = (
    ("remote: error:", "! [remote rejected]"),
    ("fatal:",),
    ("error:", "ssh:", "permission denied", "could not resolve"),
)

# `date -u +%FT%TZ` — the prefix sync.sh puts on every log line.
_LOG_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+")

_SETUP_LAUNCHD_FIX = "./install.sh --setup-launchd"

_STATUS_RANK = {"PASS": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}


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
    # Per-run memo for `_memory_sources`; see there for why. Not part of
    # the injected surface — callers build an env and never set it.
    _memory_sources_memo: Optional[list] = field(
        default=None, repr=False, compare=False
    )


def build_env(*, brain_root: Optional[Path] = None, cwd: Optional[Path] = None) -> HealthEnv:
    """Build a real `HealthEnv`: real subprocess runner, real clock, real
    `platform.system()`, real AF_UNIX connect."""
    import platform as _platform
    import socket as _socket
    import subprocess as _subprocess

    def _run(argv) -> tuple:
        try:
            proc = _subprocess.run(
                [str(a) for a in argv],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 - a broken tool is data, not a crash
            return (1, "", f"{exc!r}")
        return (proc.returncode, proc.stdout, proc.stderr)

    def _connect(path, timeout: float) -> bool:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    return HealthEnv(
        brain_root=Path(brain_root).expanduser() if brain_root else default_brain_root(),
        home=Path.home(),
        cwd=Path(cwd).expanduser() if cwd else Path.cwd(),
        now=datetime.now(timezone.utc),
        platform=_platform.system().lower(),
        run=_run,
        connect_unix=_connect,
    )


def default_brain_root() -> Path:
    """Brain root for a `HealthEnv` built with no explicit override.

    Delegates to `recall.config.brain_root()` — the one resolver — rather
    than keeping a second copy of the `$BRAIN_ROOT` / `resolve_brain_home()`
    fallback chain here. A prior local copy assumed `resolve_brain_home()`
    always ends in `memory/` and took `.parent` unconditionally; the XDG
    fallback (`$XDG_DATA_HOME/brain`) does not, so that copy silently
    diverged from the real resolver in that case.
    """
    from recall.config import brain_root as _resolve_brain_root

    return _resolve_brain_root()


# ---------------------------------------------------------------------------
# The catalogue — nine pure checks, in CHECKS order.
# ---------------------------------------------------------------------------


def check_imports_freshness(env: HealthEnv) -> CheckResult:
    """PASS/WARN/FAIL/SKIP on how stale the Claude-project memory mirror is."""
    sources = _memory_sources(env)
    if not sources:
        return CheckResult(
            "imports_freshness", "SKIP",
            "no ~/.claude/projects/*/memory files to mirror",
        )

    newest_src = max(sources, key=lambda p: _mtime_or_zero(p))
    src_mtime = _mtime_or_zero(newest_src)
    src_label = _tilde(newest_src, env.home)

    mirror_root = env.brain_root / "imports" / "claude" / "projects"
    mirror_files = _files_under(mirror_root)
    if not mirror_files:
        return CheckResult(
            "imports_freshness", "FAIL",
            f"newest {src_label} {_ts(src_mtime)}; "
            f"mirror imports/claude/projects: never mirrored",
            _setup_claude_extras_fix(env.brain_root),
        )

    dst_mtime = _newest_mtime(mirror_files) or 0.0
    lag_h = max(0.0, (src_mtime - dst_mtime) / 3600.0)
    evidence = (
        f"newest {src_label} {_ts(src_mtime)}; "
        f"newest mirror {_ts(dst_mtime)}; lag {lag_h:.0f}h"
    )
    if lag_h > IMPORTS_FAIL_HOURS:
        return CheckResult(
            "imports_freshness", "FAIL", evidence,
            _setup_claude_extras_fix(env.brain_root),
        )
    if lag_h > IMPORTS_WARN_HOURS:
        return CheckResult(
            "imports_freshness", "WARN", evidence,
            _setup_claude_extras_fix(env.brain_root),
        )
    return CheckResult("imports_freshness", "PASS", evidence)


def check_launch_agents(env: HealthEnv) -> CheckResult:
    """SKIP off-Darwin; else compares `launchctl list` against the plists
    on disk."""
    if env.platform != "darwin":
        return CheckResult(
            "launch_agents", "SKIP",
            f"launchd agents apply on darwin only (platform={env.platform})",
        )

    rc, stdout, _stderr = env.run(["launchctl", "list"])
    listed = _parse_launchctl_list(stdout) if rc == 0 else {}
    agents_dir = env.home / "Library" / "LaunchAgents"
    has_claude_sources = bool(_memory_sources(env))

    loaded: list = []
    missing: list = []
    fixes: list = []
    status = "PASS"

    for label, short in LAUNCH_AGENT_LABELS.items():
        plist = agents_dir / f"{label}.plist"
        if label in listed:
            _pid, last_exit = listed[label]
            if last_exit:
                loaded.append(f"{short}(last exit {last_exit})")
                status = _worse(status, "WARN")
                _add(fixes, f"launchctl kickstart -k gui/$UID/{label}")
            else:
                loaded.append(short)
            continue
        if plist.is_file():
            # Installed but not loaded — launchd forgot it or it was unloaded.
            missing.append(short)
            status = _worse(status, "FAIL")
            _add(fixes, f"launchctl bootstrap gui/$UID {plist}")
            continue
        if short in ("sync", "dream"):
            missing.append(short)
            status = _worse(status, "WARN")
            _add(fixes, _SETUP_LAUNCHD_FIX)
        elif short == "claude-extras" and has_claude_sources:
            missing.append(short)
            status = _worse(status, "WARN")
            _add(fixes, _setup_claude_extras_fix(env.brain_root))
        # auto-migrate / recall-daemon are optional: never mentioned when absent.

    # "user launchd:" because launchd is per-USER, not per-brain: run
    # against a sandbox brain root this check still describes the agents
    # loaded for whoever is logged in. Without the prefix the line reads
    # as a fact about the brain root printed above it.
    evidence = "user launchd: loaded: " + (", ".join(loaded) if loaded else "none")
    if missing:
        evidence += "; missing: " + ", ".join(missing)
    return CheckResult("launch_agents", status, evidence, "; ".join(fixes))


def check_brain_push(env: HealthEnv) -> CheckResult:
    """How far ahead of `origin/<upstream>` the brain repo is, and for how
    long."""
    brain = env.brain_root
    if not (brain / ".git").exists():
        return CheckResult("brain_push", "SKIP", f"{brain} is not a git repo")

    rc, stdout, _ = env.run(["git", "-C", str(brain), "remote"])
    remotes = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if rc != 0 or not remotes:
        return CheckResult(
            "brain_push", "SKIP", "no git remote configured (local-only brain)",
        )

    rc, stdout, _ = env.run(
        ["git", "-C", str(brain), "rev-parse", "--abbrev-ref", "@{u}"]
    )
    upstream = stdout.strip() if rc == 0 and stdout.strip() else "origin/main"

    rc, stdout, _ = env.run(
        ["git", "-C", str(brain), "rev-list", "--count", f"{upstream}..HEAD"]
    )
    if rc != 0:
        return CheckResult(
            "brain_push", "SKIP",
            f"cannot compare against {upstream} (no fetched upstream ref)",
        )
    try:
        ahead = int(stdout.strip() or "0")
    except ValueError:
        ahead = 0

    rc, stdout, _ = env.run(
        ["git", "-C", str(brain), "log", "-1", "--format=%ct", upstream]
    )
    last_push_ct: Optional[int] = None
    if rc == 0 and stdout.strip().isdigit():
        last_push_ct = int(stdout.strip())

    age_h = (
        (env.now.timestamp() - last_push_ct) / 3600.0
        if last_push_ct is not None else 0.0
    )

    def _with_push(text: str) -> str:
        if last_push_ct is None:
            return text
        return f"{text}; last push {_ts(last_push_ct)} ({_human_age(age_h)})"

    if ahead == 0:
        return CheckResult("brain_push", "PASS", _with_push(f"in sync with {upstream}"))

    evidence = _with_push(f"{ahead} commit{_s(ahead)} ahead of {upstream}")
    if last_push_ct is None or age_h <= PUSH_FAIL_HOURS:
        return CheckResult("brain_push", "PASS", evidence)

    remote_error = _sync_log_remote_error(
        "\n".join(_tail_lines(brain / "sync.log", _SYNC_LOG_TAIL_LINES)))
    if remote_error:
        evidence += f"; last remote error: {remote_error}"
    return CheckResult(
        "brain_push", "FAIL", evidence,
        f"see large_tracked_files; then {_brain_display(brain)}/tools/sync.sh",
    )


def check_large_tracked_files(env: HealthEnv) -> CheckResult:
    """Any git-tracked file over `TRACKED_FILE_FAIL_BYTES`."""
    brain = env.brain_root
    rc, stdout, _ = env.run(["git", "-C", str(brain), "ls-files", "-z"])
    if rc != 0:
        return CheckResult(
            "large_tracked_files", "SKIP", f"cannot list tracked files in {brain}",
        )

    sized: list = []
    for rel in stdout.split("\0"):
        rel = rel.strip()
        if not rel:
            continue
        try:
            size = (brain / rel).stat().st_size
        except OSError:
            continue
        sized.append((rel, size))

    return _size_threshold_result(
        "large_tracked_files", sized,
        limit_bytes=TRACKED_FILE_FAIL_BYTES,
        noun="tracked files",
        over_status="FAIL",
        max_listed=3,
        fix=f"git -C {brain} rm --cached <path>; "
            "./install.sh --upgrade adds the ignore rule",
    )


def check_log_sizes(env: HealthEnv) -> CheckResult:
    """Any log/episodic file over `LOG_WARN_BYTES` (rolled or current)."""
    from recall.stats import _log_files

    brain = env.brain_root
    episodic = brain / "memory" / "episodic"
    logs_dir = brain / "runtime" / "logs"

    # One base path per stream — `_log_files` anchors rolled siblings
    # against the base's own stem/suffix (`_is_rolled_sibling` in
    # recall/stats.py), so a directory-wide loose glob that also matched an
    # unrelated sibling stream sharing a stem prefix (e.g.
    # `AGENT_LEARNINGS_other.jsonl`) is no longer possible here.
    bases = [logs_dir / "events.log.jsonl", episodic / "AGENT_LEARNINGS.jsonl"]
    if episodic.is_dir():
        try:
            namespaces = sorted(
                p for p in episodic.iterdir() if p.is_dir() and p.name != "snapshots"
            )
        except OSError:
            namespaces = []
        bases.extend(ns / "AGENT_LEARNINGS.jsonl" for ns in namespaces)

    candidates: dict = {}
    for base in bases:
        for path in _log_files(base):
            if path.is_file():
                candidates[str(path)] = path

    sized: list = []
    for path in candidates.values():
        try:
            sized.append((_log_label(path, brain), path.stat().st_size))
        except OSError:
            continue

    return _size_threshold_result(
        "log_sizes", sized,
        limit_bytes=LOG_WARN_BYTES,
        noun="log file(s)",
        over_status="WARN",
        over_suffix=" (rolls on next write after upgrade)",
        fix="./install.sh --upgrade (adds rotation); "
            "rolled files land beside the current one",
    )


def _size_threshold_result(
    check_id: str,
    sized: list,
    *,
    limit_bytes: int,
    noun: str,
    over_status: str,
    fix: str,
    max_listed: "int | None" = None,
    over_suffix: str = "",
) -> CheckResult:
    """The shared shape of both size checks, over `[(label, bytes), ...]`.

    PASS quotes the count and the largest file; anything over `limit_bytes`
    flips to `over_status` and names the offenders largest-first, capped at
    `max_listed` with a `(+N more)` tail. The two checks differ only in the
    noun, the status they escalate to, the cap, and the fix — not in how
    they say any of it.
    """
    limit_mb = limit_bytes // _MB
    oversize = sorted(
        (t for t in sized if t[1] > limit_bytes), key=lambda t: t[1], reverse=True
    )
    if not oversize:
        largest = max((size for _label, size in sized), default=0)
        return CheckResult(
            check_id, "PASS",
            f"{len(sized)} {noun}, largest {largest / _MB:.1f} MB "
            f"(limit {limit_mb} MB)",
        )

    shown = oversize if max_listed is None else oversize[:max_listed]
    listed = ", ".join(f"{label} {size / _MB:.1f} MB" for label, size in shown)
    evidence = f"{listed} exceed{_verb_s(len(oversize))} {limit_mb} MB"
    hidden = len(oversize) - len(shown)
    if hidden > 0:
        evidence += f" (+{hidden} more)"
    return CheckResult(check_id, over_status, evidence + over_suffix, fix)


def check_dream_cycle(env: HealthEnv) -> CheckResult:
    """Freshness + `llm_errors` of the last dream cycle."""
    brain = env.brain_root
    ts: Optional[datetime] = None
    llm_errors: dict = {}
    ok = True
    error: Optional[str] = None

    status_path = brain / DREAM_STATUS_REL
    if status_path.is_file():
        data = _read_json(status_path)
        if isinstance(data, dict):
            ts = _parse_iso_z(str(data.get("ts") or ""))
            raw_errors = data.get("llm_errors")
            if isinstance(raw_errors, dict):
                llm_errors = {str(k): v for k, v in raw_errors.items()}
            ok = bool(data.get("ok", True))
            error = data.get("error") or None

    if ts is None:
        log_path = brain / "dream.log"
        if not log_path.is_file():
            return CheckResult(
                "dream_cycle", "SKIP",
                "dream never ran (no runtime/dream_status.json, no dream.log)",
            )
        try:
            ts = datetime.fromtimestamp(log_path.stat().st_mtime, timezone.utc)
        except OSError:
            return CheckResult(
                "dream_cycle", "SKIP", "dream never ran (dream.log unreadable)",
            )
        llm_errors = _parse_llm_errors(_last_line_containing(
            _read_text(log_path), "dream cycle:"
        ))

    age_h = (env.now - ts).total_seconds() / 3600.0
    evidence = f"last cycle {_ts(ts)} ({_human_age(age_h)})"
    if llm_errors:
        evidence += "; llm_errors=" + ",".join(
            f"{k}={v}" for k, v in llm_errors.items()
        )
    if error:
        evidence += f"; error={error}"

    fix = (
        "PATH in ~/Library/LaunchAgents/com.user.agent-dream.plist must include "
        "~/.local/bin; ./install.sh --setup-launchd rewrites it"
    )
    if age_h > DREAM_FAIL_HOURS:
        return CheckResult("dream_cycle", "FAIL", evidence, fix)
    if "provider_unavailable" in llm_errors:
        return CheckResult("dream_cycle", "FAIL", evidence, fix)
    if llm_errors or not ok:
        return CheckResult("dream_cycle", "WARN", evidence, fix)
    return CheckResult("dream_cycle", "PASS", evidence)


def check_drift(env: HealthEnv) -> CheckResult:
    """Compares the brain against the pinned brainstack repo via
    `check_freshness.detect_drift`."""
    repo = _brainstack_repo(env.brain_root)
    if repo is None:
        return CheckResult(
            "drift", "SKIP",
            "no brainstack repo to compare against "
            "(no <brain>/.brainstack-repo-path pin, recall not installed from a checkout)",
        )
    module = _load_check_freshness(repo)
    if module is None:
        return CheckResult(
            "drift", "SKIP", f"cannot import {repo}/agent/tools/check_freshness.py",
        )
    report = module.detect_drift(repo, env.brain_root)
    if report.get("in_sync", True):
        return CheckResult("drift", "PASS", f"in sync with {repo}")
    return CheckResult(
        "drift", "WARN",
        str(report.get("summary") or "drift detected"),
        f"./install.sh --upgrade from {repo}",
    )


def check_auto_recall_config(env: HealthEnv, *, config=None) -> CheckResult:
    """Whether `env.cwd`'s resolved runtime config leaves auto-recall on.

    `config=` lets a caller that has ALREADY loaded the runtime config for
    `env.cwd` (the SessionStart hook) hand it over instead of paying for a
    second three-file TOML parse. Omit it and the config is loaded here.
    """
    global_path = env.brain_root / "runtime" / "pyproject.toml"
    if not global_path.is_file():
        global_path = env.home / ".agent" / "runtime" / "pyproject.toml"
    if not _toml_auto_recall_enabled(global_path):
        return CheckResult(
            "auto_recall_config", "SKIP",
            f"auto-recall not enabled globally (no enable_auto_recall = true in {global_path})",
        )

    cfg = _load_runtime_config_at(env.cwd, config=config)
    if cfg is None:
        return CheckResult(
            "auto_recall_config", "SKIP",
            "runtime adapter not importable; cannot resolve the effective config",
        )
    # `cfg` is a `RuntimeConfig` — `_load_runtime_config_at` returns that or
    # `None`, never a duck type — so these are plain attributes. `getattr`
    # with a default here only hid a rename until someone read the evidence.
    resolved = cfg.config_path or global_path
    if cfg.enable_auto_recall:
        return CheckResult(
            "auto_recall_config", "PASS",
            f"auto-recall ON from {env.cwd} (resolved config {resolved})",
        )

    # S1 merges the layers per key, so a cwd `[tool.recall.runtime]` that
    # simply omits the key no longer shadows anything. The only way to be
    # OFF with the global set true is a layer that writes
    # `enable_auto_recall = false` outright. Name that layer, not the
    # highest one — the old "resolved config X shadows Y" pointed at a file
    # that may be entirely innocent.
    layers = [Path(p) for p in (cfg.config_layers or [resolved])]
    culprit = next((p for p in layers if _toml_auto_recall_value(p) is False), None)
    if culprit is None:
        return CheckResult(
            "auto_recall_config", "FAIL",
            f"auto-recall OFF from {env.cwd}: no config layer sets "
            f"enable_auto_recall = false, yet the merge of "
            f"{', '.join(str(p) for p in layers)} resolved it off",
            f"check {resolved} for a malformed [tool.recall.runtime] section",
        )
    via_env = os.environ.get("RECALL_RUNTIME_CONFIG")
    source = (
        f"{culprit} (via $RECALL_RUNTIME_CONFIG)"
        if via_env and Path(via_env) == culprit else str(culprit)
    )
    return CheckResult(
        "auto_recall_config", "FAIL",
        f"auto-recall OFF from {env.cwd}: {source} sets "
        f"enable_auto_recall = false, overriding "
        f"enable_auto_recall = true in {global_path}",
        f"set enable_auto_recall = true in {culprit} "
        f"([tool.recall.runtime]), or remove the key so the global value applies",
    )


def check_daemon(env: HealthEnv) -> CheckResult:
    """Whether the recall daemon is configured and reachable.

    Resolves the socket through `recall.config.daemon_socket_path`, the
    same order the hook, the CLI and the daemon itself use, with this
    env's brain root as the config tier. Health used to hardcode
    `<brain>/runtime/recall.sock`, so a daemon started with
    `$RECALL_DAEMON_SOCKET` pointing elsewhere was reported absent in the
    same minute `recall doctor` reported it running.
    """
    from recall.config import daemon_socket_path

    socket_path = daemon_socket_path(str(env.brain_root / DAEMON_SOCKET_REL))
    plist = (
        env.home / "Library" / "LaunchAgents" / "com.brainstack.recall-daemon.plist"
    )
    if not plist.is_file() and not socket_path.exists():
        return CheckResult(
            "daemon", "SKIP",
            f"recall daemon not configured (no plist, no socket at {socket_path})",
        )
    if env.connect_unix(socket_path, DAEMON_CONNECT_TIMEOUT_S):
        return CheckResult(
            "daemon", "PASS", f"socket {socket_path} accepting connections",
        )
    return CheckResult(
        "daemon", "FAIL", f"socket {socket_path} refused connection",
        "launchctl kickstart -k gui/$UID/com.brainstack.recall-daemon",
    )


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
        """FAIL if any check FAILed, else WARN if any WARNed, else PASS."""
        statuses = {c.status for c in self.checks}
        if "FAIL" in statuses:
            return "FAIL"
        if "WARN" in statuses:
            return "WARN"
        return "PASS"

    def counts(self) -> dict:
        """`{"PASS": n, "WARN": n, "FAIL": n, "SKIP": n}`."""
        out = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for check in self.checks:
            out[check.status] = out.get(check.status, 0) + 1
        return out

    def failures(self) -> list:
        """Checks with status == FAIL, in catalogue order."""
        return [c for c in self.checks if c.status == "FAIL"]

    def to_dict(self) -> dict:
        """JSON-serializable payload matching the `health.json` schema."""
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "brain_root": self.brain_root,
            "cwd": self.cwd,
            "status": self.status,
            "counts": self.counts(),
            "checks": [
                {
                    "id": c.id,
                    "status": c.status,
                    "evidence": c.evidence,
                    "fix": c.fix,
                }
                for c in self.checks
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HealthReport":
        """Inverse of `to_dict`."""
        checks = [
            CheckResult(
                id=str(c.get("id", "")),
                status=str(c.get("status", "SKIP")),
                evidence=str(c.get("evidence", "")),
                fix=str(c.get("fix", "") or ""),
            )
            for c in (d.get("checks") or [])
            if isinstance(c, dict)
        ]
        return cls(
            generated_at=str(d.get("generated_at", "")),
            brain_root=str(d.get("brain_root", "")),
            cwd=str(d.get("cwd", "")),
            checks=checks,
        )

    def render_human(self) -> str:
        """The `== recall health ==` text block."""
        width = max((len(c.id) for c in self.checks), default=0)
        lines = [
            f"== recall health ==  brain: {self.brain_root}  "
            f"generated: {self.generated_at}"
        ]
        for check in self.checks:
            lines.append(f"{check.status:<4}  {check.id:<{width}}  {check.evidence}")
            if check.fix:
                lines.append(f"      fix: {check.fix}")
        counts = self.counts()
        lines.append(
            f"overall: {self.status} ({counts['FAIL']} fail, {counts['WARN']} warn, "
            f"{counts['PASS']} pass, {counts['SKIP']} skip)"
        )
        return "\n".join(lines)


def run_health(env: HealthEnv) -> HealthReport:
    """Run every check in `CHECKS`. A check that raises degrades to a WARN
    `CheckResult` instead of taking the whole report down."""
    results: list = []
    for check in CHECKS:
        try:
            results.append(check(env))
        except Exception as exc:  # noqa: BLE001 - a broken check is a WARN, not a crash
            results.append(
                CheckResult(
                    id=getattr(check, "__name__", "unknown_check"),
                    status="WARN",
                    evidence=f"check crashed: {exc!r}",
                )
            )
    return HealthReport(
        generated_at=_ts_full(env.now),
        brain_root=str(env.brain_root),
        cwd=str(env.cwd),
        checks=results,
    )


def write_report(report: HealthReport, path: Path) -> None:
    """Atomic write (tmp + `os.replace`), creating `path.parent` if missing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(report.to_dict(), indent=2) + "\n")


def load_report(
    path: Path, *, now: Optional[datetime] = None, max_age_hours: float = HEALTH_STALE_HOURS
) -> Optional[HealthReport]:
    """Load `path` and return `None` if it's missing, corrupt, or older
    than `max_age_hours`.

    Pass `max_age_hours=0` to skip the age check entirely. That is how a
    caller tells "corrupt/missing" (nothing to say) apart from "stale"
    (say the hourly agent looks dead): load once with 0, and if that
    succeeds, compare `generated_at` yourself.
    """
    data = _read_json(Path(path))
    if not isinstance(data, dict):
        return None
    try:
        report = HealthReport.from_dict(data)
    except Exception:  # noqa: BLE001 - unusable payload is the same as no payload
        return None
    if _is_stale(report, now=now, max_age_hours=max_age_hours):
        return None
    return report


def read_report(
    path: Path, *, now: Optional[datetime] = None
) -> "tuple[Optional[HealthReport], bool]":
    """Parse `path` ONCE and return `(report, stale)`.

    `report` is `None` when the file is missing, unreadable, or unusable —
    three cases that mean the same thing to every caller: there is no
    report. `stale` is True when the report parsed but its `generated_at`
    is older than `HEALTH_STALE_HOURS` (an unparseable timestamp counts as
    stale: it is not evidence of freshness).

    `load_report` collapses missing, corrupt AND stale into a single
    `None`, which forced the SessionStart banner — a hook on the session
    open path — to read and parse health.json twice just to tell "say
    nothing" apart from "the hourly agent looks dead".
    """
    report = load_report(path, now=now, max_age_hours=0)
    if report is None:
        return None, False
    return report, _is_stale(report, now=now, max_age_hours=HEALTH_STALE_HOURS)


def _is_stale(
    report: HealthReport, *, now: Optional[datetime], max_age_hours: float
) -> bool:
    """True when `report.generated_at` is more than `max_age_hours` old.

    `max_age_hours` of 0 (or less) disables the check entirely; an
    unparseable `generated_at` is treated as stale rather than fresh.
    """
    if not max_age_hours or max_age_hours <= 0:
        return False
    ts = _parse_iso_z(report.generated_at)
    if ts is None:
        return True
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - ts).total_seconds() / 3600.0 > max_age_hours


# ---------------------------------------------------------------------------
# Small parsing / filesystem helpers shared by two or more checks.
# ---------------------------------------------------------------------------


def _is_health_log_line(line: str) -> bool:
    """True for sync.sh's own `health: ...` lines.

    The EXIT trap writes one on every exit path, so it always lands AFTER
    the run's terminal marker. Treat it as transparent — it belongs to no
    run, so it must neither end one nor count as its last line. Skipping
    it without moving the "last line" boundary is what broke the remote-
    error quote in production (2026-09-04 smoke test, Defect 1).
    """
    return _strip_log_timestamp(line).lower().startswith("health:")


def _is_sync_run_line(line: str) -> bool:
    """True for a line a sync run wrote itself.

    sync.sh prefixes every line of its own with `sync:`; git's stderr (the
    error lines, recognised by `_git_error_rank`) is the only other thing a
    run leaves in the log. Anything else — a traceback or a warning the EXIT
    trap's `recall health` printed un-prefixed — belongs to no run and must
    not anchor the backward scan, or the current run's own terminal marker
    ends the scan before it reaches the git stderr above it.
    """
    # Only the prefix: every terminal marker sync.sh writes is itself a
    # `sync:` line, and a stray un-prefixed line that merely CONTAINS a marker
    # phrase ("UserWarning: previous push failed, see log") must not end a
    # run (staff delta review, M1).
    return _strip_log_timestamp(line).lower().startswith("sync:")


def _git_error_rank(line: str) -> Optional[int]:
    """Which `_GIT_ERROR_PREFIXES` tier `line` belongs to, or `None`."""
    low = _strip_log_timestamp(line).lower()
    for rank, prefixes in enumerate(_GIT_ERROR_PREFIXES):
        if low.startswith(prefixes):
            return rank
    return None


def _sync_log_remote_error(text: str) -> Optional[str]:
    """The most informative git failure line from the most recent sync run
    in `sync.log`, or `None`.

    Scoped to the last run: the scan walks backwards and stops at the
    PREVIOUS run's terminal marker, so a failure from five days ago is
    never quoted as if it were current. Among the candidates in that run,
    GitHub's `remote: error:` / `! [remote rejected]` wins; failing that
    the FIRST `fatal:` (the one that names the cause); failing that
    whatever transport error is left.

    `seen_run_line` — not "is this the physical last line?" — is what
    decides whether a terminal marker ends the scan. The EXIT trap's
    `health:` line always sits below the marker, so the index test broke
    on the current run's own marker and never reached the git stderr
    above it. Only a run's OWN lines (`sync:` or git stderr) set it: a
    stray un-prefixed line below the marker is not evidence a run exists.
    """
    lines = (text or "").splitlines()[-400:]
    found: list = []
    seen_run_line = False
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if _is_health_log_line(line):
            continue  # transparent: belongs to no run, ends no run
        rank = _git_error_rank(line)
        if rank is not None:
            found.append((rank, idx, line))
            seen_run_line = True
            continue
        if not _is_sync_run_line(line):
            continue  # stray stderr after the marker: belongs to no run
        if seen_run_line and any(m in line.lower() for m in _RUN_TERMINAL_MARKERS):
            break  # walked back into the previous run
        seen_run_line = True
    if not found:
        return None
    found.sort(key=lambda t: (t[0], t[1]))
    return _strip_log_timestamp(found[0][2])


def _parse_launchctl_list(stdout: str) -> dict:
    """Parse `launchctl list` output into `{label: (pid, last_exit)}`."""
    out: dict = {}
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            fields = line.split()
        if len(fields) < 3:
            continue
        pid_raw, exit_raw, label = fields[0].strip(), fields[1].strip(), fields[2].strip()
        if label == "Label" or not label:
            continue
        out[label] = (_int_or_none(pid_raw), _int_or_none(exit_raw))
    return out


def _memory_sources(env: HealthEnv) -> list:
    """`_claude_project_memory_files(env.home)`, computed once per env.

    Two checks need it, and it stats every file under every project
    directory the user has ever opened in Claude Code — the single most
    expensive thing `run_health` does. Memoised on the env, which lives
    exactly one health run, so the answer cannot go stale within a report.
    """
    if env._memory_sources_memo is None:
        env._memory_sources_memo = _claude_project_memory_files(env.home)
    return env._memory_sources_memo


def _tail_lines(path: Path, limit: int) -> list[str]:
    """The last `limit` lines of `path`, read from the END of the file.

    `sync.log` is append-only and unbounded; reading all of it to keep the
    final 400 lines meant every `recall health` paid for every byte the
    agent had ever logged. Seeking back in blocks costs the tail only.

    `[]` for a missing or unreadable file — the caller treats an absent log
    exactly like an empty one.
    """
    block = 65536
    chunks: list[bytes] = []
    try:
        with Path(path).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            newlines = 0
            # `> limit`, not `>= limit`: a file whose tail starts mid-line
            # needs one extra newline before the first WHOLE line is in
            # hand, and the final slice discards the partial one anyway.
            while pos > 0 and newlines <= limit:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step)
                newlines += chunk.count(b"\n")
                chunks.append(chunk)
    except OSError:
        return []
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


def _claude_project_memory_files(home: Path) -> list:
    """Every `~/.claude/projects/<slug>/memory/*` file, skipping projects
    whose `memory/` is a symlink into the brain itself.

    `claude_misc_adapter` skips those too: a symlinked memory dir IS the
    brain, so counting it as a mirror source produces a permanent lag
    against the brain's own files.
    """
    projects = Path(home) / ".claude" / "projects"
    if not projects.is_dir():
        return []
    out: list = []
    for project in sorted(projects.iterdir()):
        memory = project / "memory"
        if memory.is_symlink() or not memory.is_dir():
            continue
        out.extend(_files_under(memory))
    return out


def _newest_mtime(paths) -> Optional[float]:
    """The newest `st_mtime` across `paths`, or `None` if empty/missing."""
    newest: Optional[float] = None
    for path in paths:
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _load_runtime_config_at(cwd: Path, *, config=None):
    """`RuntimeConfig.load()` as resolved from `cwd` (chdir guarded by
    try/finally).

    `config=` short-circuits the load. `RuntimeConfig.load()` parses up to
    three TOML files, and the SessionStart hook has already done exactly
    that for exactly this directory; passing it back in is the difference
    between one parse per session open and two. The CALLER owns the
    precondition that `config` was resolved from `cwd`.
    """
    if config is not None:
        return config
    try:
        from runtime.adapters.claude_code.config import RuntimeConfig
    except ImportError:
        return None
    previous = os.getcwd()
    try:
        os.chdir(cwd)
        return RuntimeConfig.load()
    except OSError:
        return None
    finally:
        try:
            os.chdir(previous)
        except OSError:
            pass


def _human_age(hours: float) -> str:
    """Render an hour count as `"3h ago"` / `"5d ago"` style text.

    Hours stay hours up to a week: "125h ago" says "five days of failed
    pushes" more loudly than "5d ago" does.
    """
    if hours < 0:
        hours = 0.0
    if hours < 168:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


# ---------------------------------------------------------------------------
# Private leaf helpers (not part of the plan's exported surface).
# ---------------------------------------------------------------------------


def _worse(current: str, candidate: str) -> str:
    return candidate if _STATUS_RANK.get(candidate, 0) > _STATUS_RANK.get(current, 0) else current


def _add(items: list, value: str) -> None:
    if value and value not in items:
        items.append(value)


def _brain_display(brain_root: Path) -> str:
    """`~/.agent` when the brain really lives there, else the literal path.

    Fix hints are shell commands the user pastes. Hardcoding `~/.agent`
    told a sandbox user to run `git -C ~/.agent rm --cached` against a
    repository that was not theirs (2026-09-04 smoke test)."""
    path = Path(brain_root)
    try:
        if path.resolve() == Path("~/.agent").expanduser().resolve():
            return "~/.agent"
    except OSError:  # pragma: no cover - resolve() on a broken mount
        pass
    return str(path)


def _setup_claude_extras_fix(brain_root: Path) -> str:
    return (
        "./install.sh --setup-claude-extras (then wait <=1h or run "
        f"{_brain_display(brain_root)}/tools/sync_claude_extras.py)"
    )


def _s(count: int) -> str:
    """Plural suffix for a NOUN: "1 commit", "2 commits"."""
    return "" if count == 1 else "s"


def _verb_s(count: int) -> str:
    """Plural suffix for a VERB — the mirror of `_s`: one file "exceeds",
    two files "exceed". These lines are read by a human in the session
    banner every day, and "1 commits ahead" / "51 MB exceed" costs the
    whole check its credibility (2026-09-04 smoke test)."""
    return "s" if count == 1 else ""


def _int_or_none(raw: str) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _files_under(root: Path) -> list:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _tilde(path: Path, home: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(home))
    except ValueError:
        return str(path)


def _ts(moment) -> str:
    """`2026-09-04T13:00Z` from an epoch float or an aware datetime."""
    if isinstance(moment, datetime):
        dt = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(float(moment), timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _ts_full(moment: datetime) -> str:
    dt = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _strip_log_timestamp(line: str) -> str:
    return _LOG_TS_RE.sub("", line).strip()


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _last_line_containing(text: str, needle: str) -> str:
    for line in reversed((text or "").splitlines()):
        if needle in line:
            return line
    return ""


def _parse_llm_errors(line: str) -> dict:
    """`llm_errors=provider_unavailable=2,rate_limited=1` -> dict."""
    match = re.search(r"llm_errors=([\w=,]+)", line or "")
    if not match:
        return {}
    out: dict = {}
    for pair in match.group(1).split(","):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        if key:
            out[key] = _int_or_none(value) if value else 1
    return out


def _log_label(path: Path, brain: Path) -> str:
    """Short, unambiguous name for a log file in the `log_sizes` evidence."""
    try:
        return str(path.relative_to(brain / "memory"))
    except ValueError:
        return path.name


def _toml_auto_recall_value(path: Path) -> Optional[bool]:
    """`enable_auto_recall` as written in one config layer, or `None` when
    the layer does not mention the key.

    Tri-state on purpose. S1 merges the layers PER KEY, so "absent" and
    "false" are different facts: absent falls through to the global value,
    false overrides it. Only the layer that wrote `false` is worth naming
    in the FAIL evidence.
    """
    if not Path(path).is_file():
        return None
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = data.get("tool", {}).get("recall", {}).get("runtime", {})
    if not isinstance(section, dict) or "enable_auto_recall" not in section:
        return None
    return bool(section.get("enable_auto_recall"))


def _toml_auto_recall_enabled(path: Path) -> bool:
    return _toml_auto_recall_value(path) is True


def _brainstack_repo(brain_root: Path) -> Optional[Path]:
    """The brainstack checkout to compare the brain against: the
    `.brainstack-repo-path` pin first, then the checkout `recall` itself
    was installed from (editable installs only)."""
    pin = Path(brain_root) / ".brainstack-repo-path"
    if pin.is_file():
        try:
            candidate = Path(pin.read_text(encoding="utf-8").strip()).expanduser()
        except OSError:
            candidate = None
        if candidate and (candidate / "agent" / "tools" / "check_freshness.py").is_file():
            return candidate
    import recall as _recall

    try:
        candidate = Path(_recall.__file__).resolve().parents[1]
    except (AttributeError, IndexError, OSError, TypeError):
        return None
    if (candidate / "agent" / "tools" / "check_freshness.py").is_file():
        return candidate
    return None


def _load_check_freshness(repo: Path):
    import importlib.util

    path = Path(repo) / "agent" / "tools" / "check_freshness.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "_brainstack_health_check_freshness", path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - an unimportable repo is a SKIP, not a crash
        return None
    return module
