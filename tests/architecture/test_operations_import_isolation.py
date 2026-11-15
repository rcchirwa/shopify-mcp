"""Story 10.101: the typed operations layer must not load the MCP tools layer.

``tests/architecture/test_layering.py`` reads import statements with an AST, so
it sees only a module's direct imports. ``shopify.operations.publications``
imported its error classes from ``shopify_mcp.client``, and ``client`` imports
``shopify_mcp.tools._http`` and friends: the path operations -> client -> tools
is two hops, and no single file's AST shows it. This test closes the gap at
run time. Each operations module is imported in a fresh interpreter, and the
test fails if any ``shopify_mcp.tools`` module ended up in ``sys.modules``.
"""

import importlib
import json
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import shopify_mcp.shopify.operations as operations_pkg
from shopify_mcp import client

# The child interpreter must import the same copy of the package as this test,
# whether it is installed or reached through PYTHONPATH (the CI gate runs the
# candidate's src/ on PYTHONPATH without installing it).
_ENV = {**os.environ, "PYTHONPATH": str(Path(client.__file__).resolve().parents[1])}


def _run_probe(code: str, *args: str) -> list[str]:
    """Run ``code`` in a fresh interpreter; return the JSON list it prints."""
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )
    assert result.returncode == 0, result.stderr
    loaded: list[str] = json.loads(result.stdout)
    return loaded


_OPERATION_MODULES = sorted(
    m.name
    for m in pkgutil.iter_modules(operations_pkg.__path__, prefix=f"{operations_pkg.__name__}.")
)

_PROBE = (
    "import importlib, json, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "print(json.dumps(sorted(m for m in sys.modules "
    "if m == 'shopify_mcp.tools' or m.startswith('shopify_mcp.tools.'))))\n"
)

# The moved names. ``client`` re-exports each one (Story 10.101), so existing
# ``from shopify_mcp.client import ...`` callers keep working.
_MOVED_NAMES = [
    "_CarriesUnfenced",
    "ShopifyError",
    "ShopifyProtocolError",
    "ShopifyTransportError",
    "TransientShopifyError",
    "unfenced_message",
]


def test_operations_modules_were_discovered() -> None:
    # An empty parametrize list would pass vacuously.
    assert "shopify_mcp.shopify.operations.publications" in _OPERATION_MODULES


@pytest.mark.parametrize("module_name", _OPERATION_MODULES)
def test_operations_module_loads_no_tools_module(module_name: str) -> None:
    loaded = _run_probe(_PROBE, module_name)
    assert loaded == [], f"{module_name} loads tools modules: {loaded}"


def test_errors_module_loads_no_client_or_tools_module() -> None:
    probe = (
        "import json, sys\n"
        "import shopify_mcp.errors\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m == 'shopify_mcp.client' or m == 'shopify_mcp.tools' "
        "or m.startswith('shopify_mcp.tools.'))))\n"
    )
    assert _run_probe(probe) == []


@pytest.mark.parametrize("name", _MOVED_NAMES)
def test_client_reexports_the_identical_object(name: str) -> None:
    errors = importlib.import_module("shopify_mcp.errors")
    assert getattr(client, name) is getattr(errors, name)


def test_error_hierarchy_is_preserved() -> None:
    errors = importlib.import_module("shopify_mcp.errors")
    assert issubclass(errors.ShopifyError, errors._CarriesUnfenced)
    assert issubclass(errors.ShopifyProtocolError, errors.ShopifyError)
    assert issubclass(errors.ShopifyTransportError, errors.ShopifyProtocolError)
    assert issubclass(errors.TransientShopifyError, errors._CarriesUnfenced)
    assert not issubclass(errors.TransientShopifyError, errors.ShopifyError)


def test_isolation_probe_is_not_vacuous() -> None:
    # Control: importing the client does load tools modules, so a "[]" result
    # above means the guard looked and found nothing, not that it cannot see.
    assert _run_probe(_PROBE, "shopify_mcp.client") != []
