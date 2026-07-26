"""Structural guard for the test-suite layout (Story 10.45 / FS-1).

The suite used to be 39 ``test_*.py`` modules sitting at the repo root — over
80% of the codebase by line count, drowning ``git status`` and tab-completion.
The live/offline split was encoded twice: once as an ``_offline`` filename
suffix on 36 files, and again as a hand-maintained ``--ignore=<file>`` list in
``pyproject.toml`` that had to be edited by hand every time a live runner was
added.

This module is the executable spec for the layout that replaced it: tests live
under ``tests/`` mirroring the source tree, the split is expressed once — by
directory — and every directory is an importable package.

That last rule is load-bearing, not stylistic. Mirroring the source layout
produces nine duplicate test basenames, and under pytest's default *prepend*
import mode two same-named modules in non-package directories collide on
``sys.modules``: pytest aborts the whole run with an "import file mismatch"
collection error rather than a warning. A missing ``__init__.py`` therefore
fails loudly but non-obviously, which is exactly the failure mode worth
pinning down in an assertion.

Usage:
  pytest tests/architecture/test_layout.py -v
"""

import re
from collections import Counter
from pathlib import Path

import tomllib

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Matches a concrete pre-move test path — the old per-resource module name with
# the suffix baked in. Written as a pattern rather than spelled out literally so
# this module's own glob strings and prose don't trip the sweep below; that also
# means the sweep can't be defeated by a file that merely discusses the old
# convention without naming a real path.
_PRE_MOVE_PATH_RE = re.compile(r"test_[a-z0-9_]+_offline\.py")

# ``--ignore`` entries pointing at an individual module rather than a directory
# — the hardcoded list this story exists to delete.
_PER_FILE_IGNORE_RE = re.compile(r"--ignore=\S+\.py")


def _pyproject() -> dict:
    """Parsed pyproject.toml — the single source of truth for both checks below."""
    return tomllib.loads(_PYPROJECT.read_text())


def _package_dirs() -> list[Path]:
    """Every directory under ``tests/``, the root included, excluding caches."""
    return [_TESTS_ROOT] + [
        d
        for d in sorted(_TESTS_ROOT.rglob("*"))
        if d.is_dir() and not d.name.startswith((".", "__"))
    ]


def _python_sources() -> list[Path]:
    """Every first-party ``.py`` file: the packaged modules plus the test tree.

    Derived from ``[tool.setuptools]`` rather than hardcoded so a newly
    packaged module is swept without editing this test — and so the sweep can
    never quietly walk a ``.venv`` or a nested worktree.
    """
    setuptools = _pyproject()["tool"]["setuptools"]
    sources = [_REPO_ROOT / f"{module}.py" for module in setuptools["py-modules"]]
    roots = [_TESTS_ROOT] + [
        _REPO_ROOT / package for package in setuptools["packages"] if "." not in package
    ]
    for root in roots:
        sources.extend(sorted(root.rglob("*.py")))
    return [path for path in sources if path.is_file()]


def test_no_test_modules_remain_at_the_repo_root():
    """AC: all 39 test modules live under ``tests/``; the root holds none."""
    strays = sorted(path.name for path in _REPO_ROOT.glob("test_*.py"))
    assert not strays, (
        f"{len(strays)} test module(s) still at the repo root: {strays}. "
        "Move them under tests/ (git mv, to preserve history)."
    )


def test_the_test_tree_mirrors_the_source_layout():
    """Guard the premise of the ``__init__.py`` rule: duplicates really exist.

    If a future reshuffle flattened the tree, the packaging requirement below
    would still pass but would no longer be protecting anything. Asserting the
    duplicates are present keeps that test honest.
    """
    basenames = Counter(path.name for path in _TESTS_ROOT.rglob("test_*.py"))
    # Count collisions, not distinct names: test_webhooks.py appears three
    # times (tools, shopify.operations, live) and each copy past the first is
    # an independent chance to hit the import-mismatch error.
    collisions = sum(count - 1 for count in basenames.values() if count > 1)
    duplicated = sorted(name for name, count in basenames.items() if count > 1)
    assert collisions >= 9, (
        f"Expected at least 9 basename collisions from mirroring the source layout, "
        f"found {collisions} across {duplicated}"
    )


def test_every_tests_directory_is_an_importable_package():
    """AC: an ``__init__.py`` in every directory, or collection hard-fails."""
    missing = [
        str(directory.relative_to(_REPO_ROOT))
        for directory in _package_dirs()
        if not (directory / "__init__.py").is_file()
    ]
    assert not missing, (
        f"Missing __init__.py in: {missing}. Duplicate test basenames in a "
        "non-package directory abort collection with an 'import file mismatch'."
    )


def test_no_test_module_carries_the_offline_suffix():
    """AC: the filename-encoded ``_offline`` marker is gone."""
    suffixed = sorted(
        str(path.relative_to(_REPO_ROOT)) for path in _TESTS_ROOT.rglob("test_*_offline.py")
    )
    assert not suffixed, (
        f"Filename-encoded offline marker still present on: {suffixed}. "
        "The live/offline split is expressed by directory (tests/live/) now."
    )


def test_the_live_split_is_expressed_by_directory_not_by_a_file_list():
    """AC: adding a live runner requires no config edit."""
    ini_options = _pyproject()["tool"]["pytest"]["ini_options"]

    assert ini_options.get("testpaths") == ["tests"], (
        "pytest testpaths must scope default discovery to tests/, so a bare "
        "`pytest` picks up the suite from anywhere in the repo."
    )

    per_file = _PER_FILE_IGNORE_RE.findall(ini_options.get("addopts", ""))
    assert not per_file, (
        f"addopts still names individual modules: {per_file}. Exclude the "
        "tests/live directory instead, so adding a runner needs no config edit."
    )


def test_the_live_exclusion_is_anchored_to_the_conftest_not_the_cwd():
    """AC: the exclusion survives pytest being invoked from another directory.

    A relative ``--ignore=tests/live`` in pyproject.toml looks equivalent but
    is resolved against the *invocation* directory, so ``cd /tmp && pytest
    ~/shopify-mcp`` silently collects the live runners and fails five tests on
    missing credentials. ``collect_ignore`` is resolved relative to the conftest
    that declares it, which is what makes it hold from anywhere.
    """
    conftest = (_TESTS_ROOT / "conftest.py").read_text()
    assert re.search(r"^collect_ignore\s*=\s*\[[^\]]*\"live\"", conftest, re.MULTILINE), (
        'tests/conftest.py must declare collect_ignore = ["live"] — those '
        "runners need SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN against a real store."
    )


def test_no_source_file_points_at_a_pre_move_test_path():
    """AC: no docstring or comment still cites a ``test_*_offline.py`` path."""
    stale: dict[str, list[str]] = {}
    for path in _python_sources():
        hits = sorted(set(_PRE_MOVE_PATH_RE.findall(path.read_text())))
        if hits:
            stale[str(path.relative_to(_REPO_ROOT))] = hits
    assert not stale, f"Stale pre-move test paths referenced in: {stale}"
