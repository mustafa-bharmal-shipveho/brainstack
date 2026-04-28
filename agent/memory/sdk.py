"""Public Python SDK for the v0.2 namespaced brain.

External consumers (e.g. a separate TypeScript runtime + python bridge,
or other agent frameworks) use this module to read + write the brain
through a typed surface, without depending on internal layout details.

Backward compatibility: passing namespace="default" maps to v0.1 paths
(no extra subdir under episodic/ semantic/ candidates/) so existing
brains don't need migration.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional


# --- Path resolution -------------------------------------------------

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
DEFAULT_NS = "default"

# Schema versioning. Writes always stamp CURRENT_SCHEMA. Reads accept any
# version up to KNOWN_MAX_SCHEMA; rows with a newer schema_version are
# dropped from query_semantic results (and a warning is logged once per
# process) so a fresh process running an old SDK against a brain that has
# been upgraded does not silently misinterpret data.
CURRENT_SCHEMA = 1
KNOWN_MAX_SCHEMA = 1
_warned_about_future_schema = False

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARNESS_HOOKS = os.path.normpath(os.path.join(_HERE, "..", "harness", "hooks"))


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str):
        raise ValueError(f"namespace must be str, got {type(namespace).__name__}")
    # Allow "default" verbatim as the v0.1 backward-compat sentinel.
    if namespace == DEFAULT_NS:
        return namespace
    if not _NAMESPACE_RE.match(namespace):
        raise ValueError(
            f"invalid namespace {namespace!r}: must match ^[a-z][a-z0-9_-]{{0,31}}$"
        )
    return namespace


def _resolve_brain_root(brain_root: Optional[str]) -> str:
    """Explicit arg > BRAIN_ROOT env > ~/.agent."""
    if brain_root:
        return os.path.abspath(brain_root)
    env = os.environ.get("BRAIN_ROOT")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.expanduser("~/.agent"))


def _episodic_path(namespace: str, brain_root: Optional[str]) -> str:
    root = _resolve_brain_root(brain_root)
    if namespace == DEFAULT_NS:
        return os.path.join(root, "memory", "episodic", "AGENT_LEARNINGS.jsonl")
    return os.path.join(root, "memory", "episodic", namespace,
                        "AGENT_LEARNINGS.jsonl")


def _semantic_dir(namespace: str, brain_root: Optional[str]) -> str:
    root = _resolve_brain_root(brain_root)
    if namespace == DEFAULT_NS:
        return os.path.join(root, "memory", "semantic")
    return os.path.join(root, "memory", "semantic", namespace)


def _candidates_dir(namespace: str, brain_root: Optional[str]) -> str:
    root = _resolve_brain_root(brain_root)
    if namespace == DEFAULT_NS:
        return os.path.join(root, "memory", "candidates")
    return os.path.join(root, "memory", "candidates", namespace)


def _policy_path(namespace: str, brain_root: Optional[str]) -> str:
    """Where this namespace stores its policy file.

    Prefers .yaml; downstream readers fall back to .json if .yaml absent.
    """
    sem = _semantic_dir(namespace, brain_root)
    return os.path.join(sem, "policy.yaml")


# --- Episodic append -------------------------------------------------

def _import_episodic_io():
    """Bring `_episodic_io.append_jsonl` into scope.

    The hook module lives under agent/harness/hooks/ and is intentionally
    not a package, so we add its dir to sys.path on first use.
    """
    if _HARNESS_HOOKS not in sys.path:
        sys.path.insert(0, _HARNESS_HOOKS)
    import _episodic_io  # type: ignore[import-not-found]
    return _episodic_io


def append_episodic(
    namespace: str,
    event: dict,
    brain_root: Optional[str] = None,
) -> dict:
    """Sentinel-locked append of `event` to this namespace's episodic JSONL.

    Stamps `schema_version: 1` and `ts` (ISO 8601 UTC) if missing. Returns
    the stamped event so callers can pass it to other systems.
    """
    _validate_namespace(namespace)
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    if "schema_version" not in event:
        event["schema_version"] = CURRENT_SCHEMA
    if "ts" not in event:
        event["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = _episodic_path(namespace, brain_root)
    io = _import_episodic_io()
    io.append_jsonl(path, event)
    return event


# --- Semantic query --------------------------------------------------

def _read_jsonl(path: str) -> List[dict]:
    """Read a JSONL file, drop unparseable lines, and filter rows whose
    `schema_version` exceeds what this SDK understands.

    Filtering is conservative: rows missing `schema_version` are kept
    (treated as schema 1) so legacy v0.1 episodic streams still parse.
    Rows with `schema_version > KNOWN_MAX_SCHEMA` are dropped and a single
    warning is emitted per process so operators notice a forward-version
    mismatch without flooding logs.
    """
    global _warned_about_future_schema
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    skipped_future = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                v = row.get("schema_version", 1)
                if isinstance(v, int) and v > KNOWN_MAX_SCHEMA:
                    skipped_future += 1
                    continue
                out.append(row)
    except OSError:
        return []
    if skipped_future and not _warned_about_future_schema:
        _warned_about_future_schema = True
        sys.stderr.write(
            f"[mustafa-agentic-stack] dropped {skipped_future} row(s) from {path} "
            f"with schema_version > {KNOWN_MAX_SCHEMA}; SDK upgrade may be needed\n"
        )
    return out


def query_semantic(
    namespace: str,
    query: Optional[str] = None,
    k: int = 10,
    brain_root: Optional[str] = None,
) -> List[dict]:
    """Read graduated lessons for `namespace`.

    Without a query, returns the last `k` entries (in file order).
    With a query, returns up to `k` entries containing the query
    substring (case-insensitive) in claim, why, or how_to_apply.
    """
    _validate_namespace(namespace)
    if not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive int")
    sem = _semantic_dir(namespace, brain_root)
    path = os.path.join(sem, "lessons.jsonl")
    rows = _read_jsonl(path)
    if not rows:
        return []
    if query is None:
        return rows[-k:]
    q = query.lower()
    matches: List[dict] = []
    for r in rows:
        for field in ("claim", "why", "how_to_apply"):
            v = r.get(field)
            if isinstance(v, str) and q in v.lower():
                matches.append(r)
                break
        if len(matches) >= k:
            break
    return matches


# --- Policy r/w ------------------------------------------------------

def _try_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:
        return None


def read_policy(
    namespace: str,
    brain_root: Optional[str] = None,
) -> dict:
    """Load the namespace's policy file. Empty dict if missing/unreadable.

    Preference order:
      - <semantic>/policy.yaml (parsed as YAML if PyYAML installed,
        else JSON-as-fallback if file looks JSON-y)
      - <semantic>/policy.json (parsed as JSON)
    """
    _validate_namespace(namespace)
    sem = _semantic_dir(namespace, brain_root)
    yaml_path = os.path.join(sem, "policy.yaml")
    json_path = os.path.join(sem, "policy.json")

    yaml_mod = _try_yaml()
    if os.path.exists(yaml_path):
        try:
            with open(yaml_path) as f:
                text = f.read()
        except OSError:
            return {}
        if not text.strip():
            return {}
        if yaml_mod is not None:
            try:
                obj = yaml_mod.safe_load(text)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
        # Best-effort JSON fallback.
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    if os.path.exists(json_path):
        try:
            with open(json_path) as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def write_policy(
    namespace: str,
    policy: dict,
    brain_root: Optional[str] = None,
) -> None:
    """Atomically write the namespace's policy file.

    Writes YAML if PyYAML is available, JSON otherwise (file extension
    follows the format actually written so readers find it).
    """
    _validate_namespace(namespace)
    if not isinstance(policy, dict):
        raise ValueError("policy must be a dict")
    sem = _semantic_dir(namespace, brain_root)
    os.makedirs(sem, exist_ok=True)

    # Use the project's atomic-write helper.
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from _atomic import atomic_write_text  # type: ignore[import-not-found]

    yaml_mod = _try_yaml()
    if yaml_mod is not None:
        text = yaml_mod.safe_dump(policy, sort_keys=True)
        path = os.path.join(sem, "policy.yaml")
    else:
        text = json.dumps(policy, indent=2, sort_keys=True)
        path = os.path.join(sem, "policy.json")
    atomic_write_text(path, text)


# --- Clusterer registry re-export -----------------------------------

def register_clusterer(namespace: str, fn: Callable) -> None:
    """Re-export of agent.dream.registry.register_clusterer.

    Kept here so external code can do `from agent.memory.sdk import
    register_clusterer` without depending on the internal package layout.
    """
    _validate_namespace(namespace)
    # Path-based import — `agent/` is not a package in v0.2 so we add the
    # dream/ dir to sys.path and import `registry` as a flat module. This
    # keeps the SDK loadable both ways (script-style and package-style).
    dream_dir = os.path.normpath(os.path.join(_HERE, "..", "dream"))
    if dream_dir not in sys.path:
        sys.path.insert(0, dream_dir)
    import registry as _registry  # type: ignore[import-not-found]
    _registry.register_clusterer(namespace, fn)


__all__ = [
    "append_episodic",
    "query_semantic",
    "read_policy",
    "write_policy",
    "register_clusterer",
]
