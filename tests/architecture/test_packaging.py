"""Structural guard for the shipped-distribution contents (Story 10.46 / FS-2).

``_testing/`` held the offline suite's test doubles (``FakeClient``,
``CapturingServer``) but was also listed in ``[tool.setuptools] packages`` —
so `pip install -e .`, and any real wheel install, put those fixtures into
site-packages next to production code. This module is the executable spec for
the fix: the doubles live under ``tests/support/`` (not part of any installed
package), ``_testing`` is gone from the packaging declaration, and the mypy
gate that used to type-check the doubles at their old path moved with them
rather than silently disappearing.

Usage:
  pytest tests/architecture/test_packaging.py -v
"""

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_SUPPORT_ROOT = _REPO_ROOT / "tests" / "support"


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def test_testing_directory_no_longer_exists_at_repo_root():
    """AC: `_testing/` no longer exists at the repo root."""
    assert not (_REPO_ROOT / "_testing").exists(), (
        "_testing/ still exists at the repo root — the test doubles must live "
        "under tests/support/ instead (git mv, to preserve history)."
    )


def test_the_doubles_live_under_tests_support():
    """AC: FakeClient and CapturingServer live under tests/support/.

    Imports the package rather than grepping ``__init__.py`` for the names,
    so a stale comment mentioning them without an actual re-export can't
    pass this check.
    """
    assert _SUPPORT_ROOT.is_dir(), "tests/support/ is missing"
    assert (_SUPPORT_ROOT / "__init__.py").is_file(), "tests/support/__init__.py is missing"
    assert (_SUPPORT_ROOT / "fake_client.py").is_file(), "tests/support/fake_client.py is missing"

    from tests import support

    assert hasattr(support, "CapturingServer") and hasattr(support, "FakeClient"), (
        "tests/support/__init__.py must still re-export CapturingServer and FakeClient"
    )


def test_testing_is_absent_from_setuptools_packages():
    """AC: `_testing` is absent from [tool.setuptools] packages — the
    distribution no longer ships test fixtures alongside production code."""
    packages = _pyproject()["tool"]["setuptools"]["packages"]
    assert "_testing" not in packages, (
        f"_testing is still listed in [tool.setuptools] packages: {packages}"
    )


# Directories never worth sweeping for stale imports: VCS internals, caches,
# and virtualenvs. Mirrors tests/architecture/test_layout.py's _NON_SUITE_DIRS
# so this scan can't turn into a full-tree walk as the repo grows.
_NON_SOURCE_DIRS = frozenset(
    {".git", ".venv", "venv", ".claude", "build", "dist", "node_modules", ".mypy_cache"}
)


def test_no_python_source_imports_the_old_testing_package():
    """AC: no test file imports from the removed `_testing` package."""
    this_file = Path(__file__).resolve()
    offenders = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        if path.resolve() == this_file:
            continue
        if any(part.startswith(".") or part in _NON_SOURCE_DIRS for part in path.parts):
            continue
        content = path.read_text(encoding="utf-8")
        if "from _testing" in content or "import _testing" in content:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, f"Still importing the removed _testing package: {offenders}"


def test_mypy_gate_moved_to_tests_support_not_dropped():
    """AC: mypy still type-checks the doubles at their new path under
    disallow_untyped_defs — the gate moved with the code and was not dropped."""
    mypy_files = _pyproject()["tool"]["mypy"]["files"]
    assert "_testing" not in mypy_files, f"_testing still in [tool.mypy] files: {mypy_files}"
    assert "tests/support" in mypy_files, (
        f"tests/support missing from [tool.mypy] files: {mypy_files} — the "
        "disallow_untyped_defs gate on the doubles must move with the code."
    )


def test_coverage_source_still_excludes_the_doubles():
    """AC (confirm, not change): [tool.coverage.run] source excludes the
    doubles at their new path — a stray inclusion would break the 100% gate."""
    source = _pyproject()["tool"]["coverage"]["run"]["source"]
    assert "tests" not in source and "tests.support" not in source, (
        f"tests/support must stay excluded from coverage measurement: {source}"
    )
