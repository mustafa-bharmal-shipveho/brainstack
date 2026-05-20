"""Pre-distribution: `pip install -r requirements-dev.txt && make test` must
work on a fresh clone, including on Python 3.9 (which CONTRIBUTING.md says is
supported for tests).

Two failures the pre-distribution audit caught:

  B1.1 — `requirements-dev.txt` declared only `pytest` + `pytest-timeout`.
         Core deps (`qdrant-client`, `typer`, `pyyaml`, `platformdirs`,
         `fastembed`) are in `pyproject.toml` but never reach the dev path.
         A colleague running `pip install -r requirements-dev.txt` then
         `pytest tests/` hits `ModuleNotFoundError: qdrant_client`.

  B1.2 — `tests/test_install_brain_remote.py:21` used `str | None`
         (Python 3.10+ union syntax). The file lacks `from __future__
         import annotations`, so on Python 3.9 the annotation is evaluated
         at function-def time and the whole file fails to collect.

These tests pin both as a regression gate.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestRequirementsDevInstallsCoreDeps:
    """`pip install -r requirements-dev.txt` must pull in everything the
    test suite imports — not just pytest. Otherwise `make test` errors on
    a fresh clone with ModuleNotFoundError."""

    def _read(self) -> str:
        return (REPO_ROOT / "requirements-dev.txt").read_text()

    def test_includes_brainstack_itself_or_core_deps_explicitly(self):
        """The cleanest form is `-e .[embeddings,mcp]` which pulls
        everything via pyproject.toml's optional-dependencies. Alternative:
        list each core dep (qdrant-client, typer, pyyaml, platformdirs)
        explicitly. Either is acceptable; passing nothing is not."""
        text = self._read()
        has_editable_install = "-e ." in text or "-e '.'" in text
        has_explicit_core = all(
            dep in text for dep in ("qdrant-client", "typer", "pyyaml")
        )
        assert has_editable_install or has_explicit_core, (
            "requirements-dev.txt must install the brainstack package itself "
            "(via `-e .[embeddings,mcp]`) OR list core deps "
            "(qdrant-client, typer, pyyaml) explicitly. Currently neither.\n"
            f"Content:\n{text}"
        )

    def test_pytest_still_declared(self):
        """Sanity: don't lose the dev-only pytest dependency in the refactor."""
        text = self._read()
        assert "pytest" in text


class TestPython39CompatInTestFiles:
    """CONTRIBUTING.md claims the test suite runs on Python 3.9. That holds
    iff every test file either uses `from __future__ import annotations` OR
    avoids Python 3.10+ annotation syntax (`X | None`, `list[int]` at
    runtime-evaluated positions, etc.)."""

    def test_install_brain_remote_is_python_39_compatible(self):
        """The known offender from the audit: line 21 had
        `def _find_py310() -> str | None:` and no `from __future__ import
        annotations`. Either fix unblocks 3.9."""
        path = REPO_ROOT / "tests" / "test_install_brain_remote.py"
        source = path.read_text()
        tree = ast.parse(source)

        # If `from __future__ import annotations` is the first import,
        # all annotations are strings and 3.9 doesn't evaluate them.
        has_future_annotations = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        if has_future_annotations:
            return  # PEP 563 — annotations are strings, 3.9-safe

        # Without the future-import: scan for `X | None` / `X | Y` PEP-604
        # unions in annotation positions. These crash on 3.9 at def-time.
        class UnionFinder(ast.NodeVisitor):
            def __init__(self):
                self.hits: list[tuple[int, str]] = []

            def visit_BinOp(self, node: ast.BinOp):
                if isinstance(node.op, ast.BitOr):
                    self.hits.append((node.lineno, ast.unparse(node)))
                self.generic_visit(node)

        finder = UnionFinder()
        # Walk only annotation slots — return type + arg annotations + var
        # annotations. (Bare BitOr in expressions is fine; only annotation-
        # position unions break on 3.9.)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns:
                    finder.visit(node.returns)
                for arg in (
                    node.args.args
                    + node.args.kwonlyargs
                    + (node.args.posonlyargs if hasattr(node.args, "posonlyargs") else [])
                ):
                    if arg.annotation:
                        finder.visit(arg.annotation)
            if isinstance(node, ast.AnnAssign) and node.annotation:
                finder.visit(node.annotation)

        assert not finder.hits, (
            f"{path.name} uses PEP-604 union syntax (X | Y) in annotation "
            f"positions without `from __future__ import annotations`. "
            f"Python 3.9 will fail to import this file.\n"
            f"Hits: {finder.hits}\n"
            "Fix: either add `from __future__ import annotations` at the top, "
            "or replace `X | None` with `Optional[X]`."
        )
