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
_SRC_ROOT = _REPO_ROOT / "src"
_SHOPIFY_ROOT = _SRC_ROOT / "shopify_mcp" / "shopify"


def _imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by a source file (``import x.y`` and
    ``from x.y import z`` both yield ``x``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        # Relative imports are skipped here, and unlike in the dotted-path
        # helper below that is still safe: this function feeds only the
        # "operations must not import the MCP server" rule, whose forbidden
        # names (mcp, fastmcp) are third-party top-level packages that no
        # relative import inside shopify_mcp can ever reach.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _package_of(path: Path) -> str:
    """The dotted package a module belongs to, e.g. ``shopify_mcp.shopify.operations``.

    Derived from the path relative to ``src/``. For ``__init__.py`` the package
    is the containing directory itself, which is also what Python uses to
    resolve that module's relative imports.
    """
    parts = path.relative_to(_SRC_ROOT).parts[:-1]
    return ".".join(parts)


def _imported_dotted_paths(path: Path) -> set[str]:
    """Full dotted module paths imported by a source file, relative ones resolved.

    The companion to ``_imported_modules`` above, added by Story 10.47 (FS-3).
    Once everything moved under one top-level package, the top-level name of
    *every* first-party import became ``shopify_mcp`` — so a rule phrased over
    root names alone can no longer distinguish ``shopify_mcp.tools`` from
    ``shopify_mcp.shopify.queries`` and would pass unconditionally. Comparing
    the full path keeps the layering rule enforceable.

    Relative imports are **resolved, not skipped**, and that is load-bearing.
    The predecessor of this helper ignored ``node.level > 0`` on the reasoning
    that "relative imports stay within shopify/" — true only while ``shopify``
    and ``tools`` were sibling *top-level* packages, where ``from ..tools
    import x`` raised "attempted relative import beyond top-level package".
    Now that both are sub-packages of ``shopify_mcp``, ``from ...tools import
    _gid`` inside ``shopify/operations/`` is legal, is a genuine layering
    violation, and would be invisible to a guard that skipped it — the move
    itself opened the hole.

    ``import x.y`` yields ``x.y``; ``from x.y import z`` yields both ``x.y`` and
    ``x.y.z``, since ``z`` may itself be a submodule.
    """
    return _imported_dotted_paths_for(path, package=_package_of(path))


def _imported_dotted_paths_for(path: Path, package: str) -> set[str]:
    """``_imported_dotted_paths`` with the anchoring package supplied explicitly.

    Split out so the resolver can be exercised against a synthetic module (see
    the regression test below) without that module having to exist inside the
    real package tree.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module
            else:
                # `from . import x` resolves against the module's own package;
                # each extra dot strips one more trailing component.
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            if not base:
                continue
            paths.add(base)
            paths.update(f"{base}.{alias.name}" for alias in node.names)
    return paths


def _shopify_sources() -> list[Path]:
    return sorted(_SHOPIFY_ROOT.rglob("*.py"))


def test_there_are_shopify_modules_to_check():
    """Guard against the rglob silently matching nothing."""
    assert _shopify_sources(), "no shopify/*.py modules found"


def test_a_relative_import_that_escapes_shopify_is_detected(tmp_path):
    """The rule must survive the form the src/ move made legal (Story 10.47).

    Before the move ``shopify`` and ``tools`` were sibling top-level packages,
    so ``from ..tools import x`` was a hard ImportError and the guard could
    safely ignore relative imports. As sub-packages of ``shopify_mcp`` that
    same import is legal and is exactly the violation this module exists to
    catch, so it has to be resolved rather than skipped.

    Written against a synthetic module: the real tree is (correctly) free of
    such imports, so asserting on it would pass whether or not the resolver
    works — the bug being pinned here is a guard that silently checks nothing.
    """
    module = _SRC_ROOT / "shopify_mcp" / "shopify" / "operations" / "_probe.py"
    fake = tmp_path / "probe.py"
    fake.write_text("from ...tools._log import log_write\n", encoding="utf-8")

    # Resolve against the real module's package, reading the synthetic source.
    resolved = _imported_dotted_paths_for(fake, package=_package_of(module))
    assert "shopify_mcp.tools._log" in resolved, (
        f"A relative import escaping shopify/ resolved to {resolved} — the "
        "layering rule cannot see it, so shopify/ could import tools/ freely."
    )


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
