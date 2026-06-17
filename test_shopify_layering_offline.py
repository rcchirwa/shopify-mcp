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
  pytest test_shopify_layering_offline.py -v
"""

import ast
from pathlib import Path

_SHOPIFY_ROOT = Path(__file__).parent / "shopify"


def _imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by a source file (``import x.y`` and
    ``from x.y import z`` both yield ``x``)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        # Ignore relative imports (node.level > 0) — they stay within shopify/.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _shopify_sources() -> list[Path]:
    return sorted(_SHOPIFY_ROOT.rglob("*.py"))


def test_there_are_shopify_modules_to_check():
    """Guard against the rglob silently matching nothing."""
    assert _shopify_sources(), "no shopify/*.py modules found"


def test_shopify_never_imports_tools():
    offenders = {
        str(p.relative_to(_SHOPIFY_ROOT.parent)): sorted(_imported_modules(p) & {"tools"})
        for p in _shopify_sources()
        if "tools" in _imported_modules(p)
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
