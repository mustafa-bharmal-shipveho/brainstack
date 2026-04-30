"""Auto-migrate setup wizard + launchd installer.

This module is PR-D's chassis. It:

- Reads `<brain>/auto-migrate.json` (or builds one interactively).
- Generates a LaunchAgent plist via `plistlib` (NOT sed — paths can
  contain spaces, `&`, XML metacharacters).
- Installs the plist into `~/Library/LaunchAgents/` and registers it
  via the modern `launchctl bootout`/`bootstrap` API.
- Refuses to run under sudo (LaunchAgents are user-scoped).
- Backs up hand-edited plists with a `.bak.<ts>` suffix.

The single LaunchAgent invokes `migrate_dispatcher.py auto-migrate-all`
which iterates all enabled tools sequentially under a global fcntl lock.
That avoids the race conditions one-plist-per-tool would have on the
shared `_imported.jsonl` sidecar (Codex review's P1 finding).

CLI usage:

    python3 auto_migrate_install.py setup [flags]
        --enable cursor-plans,codex-cli  : enable named tools
        --disable cursor-plans            : remove from config
        --all                              : enable everything discoverable
        --none                             : disable all tools (config kept)
        --interactive                      : prompt y/N per tool (default if no flags)
        --dry-run                          : print plan, write nothing
        --print-plist                      : emit plist XML, don't install
        --brain-root PATH                  : override (default $BRAIN_ROOT or ~/.agent)
        --plist-dir PATH                   : override (default ~/Library/LaunchAgents)
        --interval N                       : seconds between runs (default 3600)

    python3 auto_migrate_install.py remove [flags]
        Tear down the LaunchAgent. Config file kept by default.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional, Union

# Path-relative imports (same shape as the other adapters).
_HERE = Path(__file__).resolve().parent
_BASE = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_BASE / "memory"))

from _atomic import atomic_write_text  # noqa: E402
from migrate_dispatcher import discover_candidates, Candidate  # noqa: E402

LABEL = "com.brainstack.auto-migrate"
DEFAULT_INTERVAL = 3600
SCHEMA_VERSION = 1
_CONFIG_FILENAME = "auto-migrate.json"

# Format strings the wizard skips from prompting (Claude is automatic via
# symlink; already-symlinked needs no further setup).
_SKIP_FORMATS = {
    "claude-code-flat",
    "claude-code-nested",
    "claude-code-mixed",
    "already-symlinked",
}


# `launchctl_bin` accepts either a string ("/usr/bin/launchctl") or a
# callable that takes argv and returns a CompletedProcess — the latter
# lets tests inject a FakeLaunchctl without touching real launchd.
LaunchctlInvoker = Union[str, Callable[[list[str]], subprocess.CompletedProcess]]


def _run_launchctl(launchctl_bin: LaunchctlInvoker, argv: list[str]) -> subprocess.CompletedProcess:
    if callable(launchctl_bin):
        return launchctl_bin([str(launchctl_bin), *argv] if False else ["launchctl", *argv])
    return subprocess.run(
        [launchctl_bin, *argv],
        capture_output=True, text=True,
    )


# ---- Discovery filtering ----


def discover_enabled_candidates(env: Optional[dict] = None) -> list[Candidate]:
    """`discover_candidates`, filtered to only the tools the wizard prompts about.

    Drops Claude (already automatic via symlink) and already-symlinked
    sources. Order preserved — matches discovery's deterministic output.
    """
    return [c for c in discover_candidates(env=env) if c.format not in _SKIP_FORMATS]


# ---- Config file ----


def read_config(brain_root: Path) -> dict:
    path = brain_root / _CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_config(brain_root: Path, config: dict) -> None:
    """Atomic write of the config file."""
    brain_root.mkdir(parents=True, exist_ok=True)
    atomic_write_text(brain_root / _CONFIG_FILENAME, json.dumps(config, indent=2) + "\n")


# ---- Plist generation ----


def generate_plist(
    brain_root: Path,
    python_abs: Path,
    interval_seconds: int = DEFAULT_INTERVAL,
) -> bytes:
    """Generate the LaunchAgent plist as bytes.

    Use `plistlib.dumps` (NOT sed) so paths with spaces, `&`, or XML
    metacharacters round-trip correctly.
    """
    log_path = str(brain_root / "auto-migrate.log")
    dispatcher_path = str(brain_root / "tools" / "migrate_dispatcher.py")
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            str(python_abs),
            dispatcher_path,
            "auto-migrate-all",
            "--brain-root", str(brain_root),
        ],
        "RunAtLoad": True,
        "StartInterval": int(interval_seconds),
        "EnvironmentVariables": {
            "BRAIN_ROOT": str(brain_root),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(plist)


# ---- launchctl helpers ----


def _euid() -> int:
    """EUID with test override hook."""
    sim = os.environ.get("AUTO_MIGRATE_SIMULATED_EUID")
    if sim is not None:
        try:
            return int(sim)
        except ValueError:
            pass
    return os.geteuid()


def _refuse_sudo() -> Optional[str]:
    """Return a refusal message if running as root, else None."""
    if _euid() == 0:
        return (
            "auto-migrate setup must NOT be run as root — LaunchAgents are "
            "user-scoped (`gui/$UID`). Re-run without sudo."
        )
    return None


# ---- Install / remove ----


@dataclass
class InstallResult:
    plist_path: Path
    label: str
    backed_up_path: Optional[Path] = None
    bootout_returncode: int = 0
    bootstrap_returncode: int = 0
    kickstart_returncode: int = 0
    dry_run: bool = False

    def get(self, key, default=None):
        # Convenience for tests that assume dict-like access.
        return getattr(self, key, default)


def install_plist(
    plist_bytes: bytes,
    plist_dir: Path,
    launchctl_bin: LaunchctlInvoker = "launchctl",
    uid: Optional[int] = None,
    dry_run: bool = False,
) -> InstallResult:
    """Write the plist to `plist_dir`, then `launchctl bootout` (tolerated)
    and `bootstrap` against `gui/<uid>`. Calls `kickstart` to fire the
    first run immediately.

    If a plist already exists at the target path AND its content differs
    from `plist_bytes`, save it as `.bak.<unix-ts>` first (Codex P2:
    don't silently overwrite hand-edits).

    Idempotent: re-running with byte-identical bytes performs the same
    bootout/bootstrap dance but doesn't create a backup of itself.
    """
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{LABEL}.plist"
    if uid is None:
        uid = _euid()

    backup_path: Optional[Path] = None
    if plist_path.exists():
        existing = plist_path.read_bytes()
        if existing != plist_bytes:
            ts = int(time.time())
            backup_path = plist_dir / f"{LABEL}.plist.bak.{ts}"
            backup_path.write_bytes(existing)

    if dry_run:
        return InstallResult(
            plist_path=plist_path,
            label=LABEL,
            backed_up_path=backup_path,
            dry_run=True,
        )

    plist_path.write_bytes(plist_bytes)

    # bootout — tolerate "not loaded" (typically returncode 36 on macOS).
    bootout = _run_launchctl(launchctl_bin, [
        "bootout", f"gui/{uid}/{LABEL}",
    ])
    # bootstrap — must succeed.
    bootstrap = _run_launchctl(launchctl_bin, [
        "bootstrap", f"gui/{uid}", str(plist_path),
    ])
    if bootstrap.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap failed (rc={bootstrap.returncode}):\n"
            f"stdout: {bootstrap.stdout}\nstderr: {bootstrap.stderr}"
        )
    # kickstart — fire first run immediately so the user sees something.
    kickstart = _run_launchctl(launchctl_bin, [
        "kickstart", "-k", f"gui/{uid}/{LABEL}",
    ])
    return InstallResult(
        plist_path=plist_path,
        label=LABEL,
        backed_up_path=backup_path,
        bootout_returncode=bootout.returncode,
        bootstrap_returncode=bootstrap.returncode,
        kickstart_returncode=kickstart.returncode,
        dry_run=False,
    )


def remove_plist(
    plist_dir: Path,
    launchctl_bin: LaunchctlInvoker = "launchctl",
    uid: Optional[int] = None,
) -> dict:
    """`launchctl bootout` + delete the plist. Tolerates 'not loaded' errors."""
    if uid is None:
        uid = _euid()
    plist_path = plist_dir / f"{LABEL}.plist"
    bootout = _run_launchctl(launchctl_bin, ["bootout", f"gui/{uid}/{LABEL}"])
    removed = False
    if plist_path.exists():
        plist_path.unlink()
        removed = True
    return {
        "plist_path": str(plist_path),
        "removed": removed,
        "bootout_returncode": bootout.returncode,
    }


# ---- Wizard / CLI ----


def _resolve_python_abs() -> Path:
    """Best-effort: prefer the env's `PYTHON_BIN`, else `sys.executable`."""
    bin_name = os.environ.get("PYTHON_BIN") or sys.executable
    p = Path(bin_name)
    if not p.is_absolute():
        # Try `which` to absolutify
        from shutil import which
        found = which(str(bin_name))
        if found:
            p = Path(found)
    return p


def _interactive_select(candidates: list[Candidate]) -> list[Candidate]:
    """Prompt y/N per candidate. Used when neither --enable / --disable /
    --all / --none was passed."""
    enabled: list[Candidate] = []
    for c in candidates:
        print(f"\nDetected: {c.format} at {c.path}  ({c.file_count} files, {c.size_bytes} bytes)")
        print(f"Enable hourly auto-migrate for {c.format}? [y/N] ", end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt, BrokenPipeError):
            print()
            return enabled
        if answer in ("y", "yes"):
            enabled.append(c)
    return enabled


def _build_config(
    enabled: list[Candidate],
    interval_seconds: int,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "interval_seconds": int(interval_seconds),
        "tools": [
            {"format": c.format, "source": str(c.path)}
            for c in enabled
        ],
    }


def main(
    argv: Optional[list[str]] = None,
    launchctl_bin: LaunchctlInvoker = "launchctl",
) -> int:
    p = argparse.ArgumentParser(prog="auto_migrate_install")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- setup ---
    sp_setup = sub.add_parser("setup", help="install/update the auto-migrate timer")
    sp_setup.add_argument("--enable", help="comma-separated list of formats to enable")
    sp_setup.add_argument("--disable", help="comma-separated list of formats to disable from current config")
    sp_setup.add_argument("--all", action="store_true", help="enable every discoverable tool")
    sp_setup.add_argument("--none", action="store_true", help="disable all tools (clears tools[] in config)")
    sp_setup.add_argument("--interactive", action="store_true", help="prompt y/N per tool")
    sp_setup.add_argument("--dry-run", action="store_true")
    sp_setup.add_argument("--print-plist", action="store_true", help="emit plist XML, don't install")
    sp_setup.add_argument("--brain-root", default=os.environ.get("BRAIN_ROOT") or os.path.expanduser("~/.agent"))
    sp_setup.add_argument("--plist-dir", default=os.path.expanduser("~/Library/LaunchAgents"))
    sp_setup.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)

    # --- remove ---
    sp_remove = sub.add_parser("remove", help="tear down the auto-migrate timer")
    sp_remove.add_argument("--brain-root", default=os.environ.get("BRAIN_ROOT") or os.path.expanduser("~/.agent"))
    sp_remove.add_argument("--plist-dir", default=os.path.expanduser("~/Library/LaunchAgents"))
    sp_remove.add_argument("--keep-config", action="store_true", default=True,
                           help="keep auto-migrate.json (default; pass --wipe-config to remove it)")
    sp_remove.add_argument("--wipe-config", dest="keep_config", action="store_false")

    args = p.parse_args(argv)
    refusal = _refuse_sudo()
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    if args.cmd == "remove":
        result = remove_plist(
            plist_dir=Path(args.plist_dir),
            launchctl_bin=launchctl_bin,
        )
        if not args.keep_config:
            cfg = Path(args.brain_root) / _CONFIG_FILENAME
            if cfg.is_file():
                cfg.unlink()
        print(json.dumps(result, indent=2))
        return 0

    # ---- setup ----
    brain_root = Path(args.brain_root)
    plist_dir = Path(args.plist_dir)

    # Decide which tools to enable.
    try:
        all_candidates = discover_enabled_candidates()
    except Exception as e:
        print(f"auto-migrate setup: discovery failed: {e}", file=sys.stderr)
        return 2

    valid_formats = {c.format for c in all_candidates} | {"cursor-plans", "codex-cli"}

    existing_cfg = read_config(brain_root)
    existing_tools: list[dict] = list(existing_cfg.get("tools", []))

    enabled: list[Candidate] = []
    if args.disable:
        names = [n.strip() for n in args.disable.split(",") if n.strip()]
        existing_tools = [t for t in existing_tools if t["format"] not in names]
        # Don't add anything new — disable just prunes.
    elif args.none:
        existing_tools = []
    elif args.all:
        enabled = list(all_candidates)
    elif args.enable:
        names = [n.strip() for n in args.enable.split(",") if n.strip()]
        for n in names:
            if n not in valid_formats:
                print(f"auto-migrate setup: unknown format {n!r}; valid: "
                      f"{sorted(valid_formats)}", file=sys.stderr)
                return 2
        enabled = [c for c in all_candidates if c.format in names]
    else:
        # No flag given → interactive mode (or fall through to print/dry-run).
        if not (args.print_plist or args.dry_run):
            enabled = _interactive_select(all_candidates)

    # Merge enabled into existing_tools, replacing same-format entries.
    if enabled:
        kept = [t for t in existing_tools if t["format"] not in {c.format for c in enabled}]
        for c in enabled:
            kept.append({"format": c.format, "source": str(c.path)})
        existing_tools = kept

    new_config = {
        "schema_version": SCHEMA_VERSION,
        "interval_seconds": int(args.interval),
        "tools": existing_tools,
    }

    plist_bytes = generate_plist(
        brain_root=brain_root,
        python_abs=_resolve_python_abs(),
        interval_seconds=args.interval,
    )

    # --print-plist short-circuits everything else.
    if args.print_plist:
        sys.stdout.write(plist_bytes.decode("utf-8"))
        return 0

    if args.dry_run:
        print("DRY RUN — would write the following:")
        print(f"\nConfig at {brain_root / _CONFIG_FILENAME}:")
        print(json.dumps(new_config, indent=2))
        print(f"\nPlist at {plist_dir / f'{LABEL}.plist'}:")
        print(plist_bytes.decode("utf-8"))
        return 0

    # Write the config first.
    write_config(brain_root, new_config)
    # Install (or update) the plist + register with launchd.
    result = install_plist(
        plist_bytes=plist_bytes,
        plist_dir=plist_dir,
        launchctl_bin=launchctl_bin,
    )
    print(json.dumps({
        "config_path": str(brain_root / _CONFIG_FILENAME),
        "plist_path": str(result.plist_path),
        "label": result.label,
        "backed_up_path": str(result.backed_up_path) if result.backed_up_path else None,
        "tools_enabled": [t["format"] for t in new_config["tools"]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
