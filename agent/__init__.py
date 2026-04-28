"""mustafa-agentic-stack agent package.

The codebase historically used flat sys.path-style imports inside agent/
subdirectories. v0.2 adds package-style entry points (e.g. agent.memory.sdk,
agent.dream) so external consumers can `import agent.memory.sdk` from a
repo-root-on-PYTHONPATH consumer. The flat-style imports inside agent/
modules continue to work by falling through sys.path.
"""
