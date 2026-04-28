"""Per-namespace clusterer registry for the v0.2 dream cycle.

Each registered clusterer takes (brain_root, dry_run) kwargs and returns a
result dict. `run_all` iterates the registry, calls each, and aggregates.

The "default" namespace, when registered via `register_default_clusterer`,
delegates to the existing single-stream `auto_dream.run` so v0.1 behavior
is unchanged when no other namespaces are registered.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, Optional


_REGISTRY: Dict[str, Callable] = {}


def register_clusterer(namespace: str, fn: Callable) -> None:
    """Register a clusterer for a namespace. Re-registering the same key is OK."""
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be a non-empty string")
    if not callable(fn):
        raise ValueError("fn must be callable")
    _REGISTRY[namespace] = fn


def unregister_clusterer(namespace: str) -> None:
    """Drop a registration if present. Idempotent."""
    _REGISTRY.pop(namespace, None)


def registered_namespaces() -> list:
    """Return the list of currently-registered namespace keys."""
    return sorted(_REGISTRY.keys())


def get_clusterer(namespace: str) -> Optional[Callable]:
    """Return the registered clusterer for a namespace, or None."""
    return _REGISTRY.get(namespace)


def run_all(brain_root: Optional[str] = None, dry_run: bool = False) -> Dict[str, dict]:
    """Run every registered clusterer and aggregate their result dicts.

    Each clusterer is called with kwargs (brain_root=..., dry_run=...) and
    must return a dict containing at least {"namespace": str,
    "candidates_written": int}. Failures are caught per-namespace so one
    misbehaving clusterer doesn't take the whole pass down.
    """
    results: Dict[str, dict] = {}
    for ns, fn in list(_REGISTRY.items()):
        try:
            r = fn(brain_root=brain_root, dry_run=dry_run)
        except Exception as exc:  # pragma: no cover — defensive
            r = {
                "namespace": ns,
                "candidates_written": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(r, dict):
            r = {"namespace": ns, "candidates_written": 0,
                 "error": "clusterer returned non-dict"}
        r.setdefault("namespace", ns)
        r.setdefault("candidates_written", 0)
        results[ns] = r
    return results


def _default_clusterer(brain_root: Optional[str] = None, dry_run: bool = False) -> dict:
    """Wrap the existing auto_dream.run for namespace=default."""
    # Resolve the memory module dir on sys.path so auto_dream's flat
    # imports (`from promote import ...`) keep working.
    here = os.path.dirname(os.path.abspath(__file__))
    memory_dir = os.path.normpath(os.path.join(here, "..", "memory"))
    if memory_dir not in sys.path:
        sys.path.insert(0, memory_dir)
    import auto_dream  # type: ignore[import-not-found]
    return auto_dream.run(
        brain_root=brain_root, namespace="default", dry_run=dry_run,
    )


def register_default_clusterer() -> None:
    """Register the v0.1 single-stream clusterer under namespace='default'.

    Called by external bootstrap code (and by some tests). Idempotent.
    """
    register_clusterer("default", _default_clusterer)
