"""
Offline guard for the shopify/ layering rule (Story 10.23 / A5, AC5).

The ``shopify`` domain layer must stay reusable from non-MCP entry points, so:
  - no module under ``shopify/`` may import from ``tools`` (one-way dependency,
    no import cycles), and
  - ``shopify.operations`` must not import the MCP server (FastMCP / ``mcp``),
    so operations are callable without it.

Both are checked statically by parsing the source with ``ast`` — no need to
import the heavy MCP stack at test time.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/architecture/test_layering.py -v
"""

import ast
from pathlib import Path

# Anchored to the repo root rather than the CWD so the guard reads the same
# sources whatever directory pytest is invoked from (Story 10.45). The domain
# layer moved under src/shopify_mcp/ in Story 10.47 (FS-3); if this constant is
# ever left pointing at a directory that no longer exists, the rglob below
# matches nothing and every rule in this module passes while checking nothing —
# which is what test_there_are_shopify_modules_to_check exists to catch.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHOPIFY_ROOT = _REPO_ROOT / "src" / "shopify_mcp" / "shopify"


def _imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by a source file (``import x.y`` and
    ``from x.y import z`` both yield ``x``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        # Ignore relative imports (node.level > 0) — they stay within shopify/.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_dotted_paths(path: Path) -> set[str]:
    """Full dotted module paths imported by a source file.

    The companion to ``_imported_modules`` above, added by Story 10.47 (FS-3).
    Once everything moved under one top-level package, the top-level name of
    *every* first-party import became ``shopify_mcp`` — so a rule phrased over
    root names alone can no longer distinguish ``shopify_mcp.tools`` from
    ``shopify_mcp.shopify.queries`` and would pass unconditionally. Comparing
    the full path keeps the layering rule enforceable.

    ``import x.y`` yields ``x.y``; ``from x.y import z`` yields both ``x.y`` and
    ``x.y.z``, since ``z`` may itself be a submodule.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                paths.add(alias.name)
        # Ignore relative imports (node.level > 0) — they stay within shopify/.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            paths.add(node.module)
            paths.update(f"{node.module}.{alias.name}" for alias in node.names)
    return paths


def _shopify_sources() -> list[Path]:
    return sorted(_SHOPIFY_ROOT.rglob("*.py"))


def test_there_are_shopify_modules_to_check():
    """Guard against the rglob silently matching nothing."""
    assert _shopify_sources(), "no shopify/*.py modules found"


def test_shopify_never_imports_tools():
    # The tool surface is reached as ``shopify_mcp.tools`` since Story 10.47, so
    # the top-level name an offending import contributes is ``shopify_mcp``.
    # Matching on that alone would flag every legitimate intra-package import,
    # so the check looks at the full dotted path instead.
    forbidden_prefix = "shopify_mcp.tools"

    def _tool_imports(path: Path) -> list[str]:
        return sorted(
            name
            for name in _imported_dotted_paths(path)
            if name == forbidden_prefix or name.startswith(f"{forbidden_prefix}.")
        )

    offenders = {
        str(p.relative_to(_SHOPIFY_ROOT.parent)): hits
        for p in _shopify_sources()
        if (hits := _tool_imports(p))
    }
    assert not offenders, f"shopify/ must not import tools/: {offenders}"


def test_operations_never_import_mcp():
    ops_root = _SHOPIFY_ROOT / "operations"
    forbidden = {"mcp", "fastmcp"}
    offenders = {
        str(p.relative_to(_SHOPIFY_ROOT.parent)): sorted(_imported_modules(p) & forbidden)
        for p in ops_root.rglob("*.py")
        if _imported_modules(p) & forbidden
    }
    assert not offenders, f"shopify/operations must not import the MCP server: {offenders}"
