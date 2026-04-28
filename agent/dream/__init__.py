"""Pluggable dream-cycle namespace registry.

In v0.1 the dream cycle had a single hardcoded namespace ("default"). v0.2
opens the brain to external consumers (e.g. a separate TypeScript runtime)
that want their own clusterer logic per namespace while sharing the same
brain root.

Usage from external code:

    from agent.dream import registry
    registry.register_clusterer("inbox", my_inbox_clusterer)
    results = registry.run_all(brain_root="/path/to/brain")

The default namespace is registered automatically on first call to
`register_default_clusterer()` and runs the existing v0.1 logic so single-
namespace brains are unchanged.
"""
from . import registry  # noqa: F401  re-export
from .registry import (  # noqa: F401
    register_clusterer,
    unregister_clusterer,
    registered_namespaces,
    get_clusterer,
    run_all,
    register_default_clusterer,
)
