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


def test_the_distribution_cannot_ship_test_fixtures():
    """AC: the distribution no longer ships test fixtures beside production code.

    Story 10.46 asserted this by checking ``_testing`` was absent from the
    explicit ``[tool.setuptools] packages`` list. Story 10.47 (FS-3) replaced
    that list with a ``find`` directive, which has no names to inspect — so
    re-checking the declaration would pass vacuously no matter what shipped.

    The guarantee is now structural instead: discovery is scoped to ``src/``,
    and the doubles live under ``tests/``, which is outside it. That is the
    stronger form — it holds for any fixture directory, not just the one named
    ``_testing``.
    """
    setuptools = _pyproject()["tool"]["setuptools"]
    assert setuptools["packages"]["find"]["where"] == ["src"], (
        "Package discovery must stay scoped to src/ — widening it would sweep "
        f"the test tree back into the distribution: {setuptools['packages']!r}"
    )
    # The doubles must not be reachable by that discovery. Checked by walking
    # src/ for the fixture package rather than by comparing _SUPPORT_ROOT to
    # src/ — _SUPPORT_ROOT is a hardcoded constant, so such a comparison is
    # decided by the constant's definition and can never fail (Story 10.47
    # review).
    packaged_fixtures = sorted(
        str(p.relative_to(_REPO_ROOT))
        for p in (_REPO_ROOT / "src").rglob("*")
        if p.is_dir() and p.name in {"_testing", "support", "fixtures"}
    )
    assert not packaged_fixtures, (
        f"Test-fixture directories found inside the packaged tree: {packaged_fixtures}. "
        "The doubles belong under tests/support/, which discovery cannot see."
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
    scanned = 0
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        if path.resolve() == this_file:
            continue
        # Filter on the path *relative to the repo root*. Filtering the
        # absolute path meant any checkout living beneath a dot-directory
        # disabled this guard entirely — including this project's own
        # `.claude/worktrees/<branch>` workflow, where it scanned 0 files and
        # passed vacuously (found by the Story 10.47 review).
        if any(
            part.startswith(".") or part in _NON_SOURCE_DIRS
            for part in path.relative_to(_REPO_ROOT).parts
        ):
            continue
        scanned += 1
        content = path.read_text(encoding="utf-8")
        if "from _testing" in content or "import _testing" in content:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert scanned, (
        "This sweep examined 0 files — it is passing without checking anything. "
        "Check the directory filter above before trusting a green result."
    )
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
