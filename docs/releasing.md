# Releasing

## Publishing `recall-brain` to PyPI

The package name is **`recall-brain`** (the `brainstack` name is already taken
on PyPI by an unrelated project; `recall-brain` is unclaimed and matches
`pyproject.toml`). Once published, users can run it with zero clone:

```bash
uvx recall-brain query "what did I learn about flaky tests"
# or
pipx install recall-brain
```

### Steps

```bash
# 1. Build (verified: both artifacts pass `twine check`)
python -m pip install build twine
python -m build                      # writes dist/recall_brain-<ver>.{whl,tar.gz}

# 2. Sanity-check the wheel in a throwaway venv
python3 -m venv /tmp/rb-check
/tmp/rb-check/bin/pip install dist/recall_brain-*.whl
/tmp/rb-check/bin/recall --help      # entrypoints must resolve

# 3. Validate metadata
twine check dist/*

# 4. Upload (needs a PyPI API token in ~/.pypirc or TWINE_PASSWORD)
#    Test it on TestPyPI first if you like:
#      twine upload --repository testpypi dist/*
twine upload dist/*
```

Bump `version` in `pyproject.toml` for every release (PyPI refuses to
overwrite an existing version). After publishing, the MCP-registry
[`server.json`](../server.json) `uvx` runtime hint resolves against the
published package.

## Other distribution channels (manual, one-time)

- **MCP Registry** (`registry.modelcontextprotocol.io`): publish
  [`server.json`](../server.json) per the registry's publisher flow.
- **Community MCP directories**: submit to mcp.so, Smithery, Glama, and the
  `punkpeye/awesome-mcp-servers` list.
- **Claude Code plugin**: users can already add this repo as a marketplace via
  [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json);
  submit to the community plugin directory for listing.
