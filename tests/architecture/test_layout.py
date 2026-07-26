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
import tomllib
from collections import Counter
from pathlib import Path


def _find_repo_root() -> Path:
    """Walk up to the directory holding pyproject.toml.

    Deliberately not ``parents[2]``: a positional anchor silently points at the
    wrong directory if this module is ever moved a level, and most of the
    checks below are *absence* assertions over a glob — against a non-existent
    tree they would pass vacuously rather than fail. Walking to a landmark
    can't drift, and the sanity assertions below turn any remaining surprise
    into a loud failure.
    """
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError(f"No pyproject.toml above {__file__} — cannot locate the repo root.")


_REPO_ROOT = _find_repo_root()
_TESTS_ROOT = _REPO_ROOT / "tests"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Directory names that are never part of the suite: tool caches and virtualenvs.
_NON_SUITE_DIRS = frozenset({".git", ".venv", "venv", ".claude", "build", "dist", "node_modules"})

# Matches a pre-move test module reference — the old per-resource name with the
# suffix baked in, with or without the ``.py`` (prose cites both forms). Written
# as a pattern rather than spelled out literally so this module's own glob
# strings don't trip the sweep below.
_PRE_MOVE_PATH_RE = re.compile(r"test_[a-z0-9_]+_offline(?:\.py)?\b")

# ``--ignore`` entries pointing at an individual module rather than a directory
# — the hardcoded list this story exists to delete.
_PER_FILE_IGNORE_RE = re.compile(r"--ignore=\S+\.py")


def _read(path: Path) -> str:
    """Read as UTF-8 explicitly — the tree is full of en/em-dashes, and the
    locale default would raise UnicodeDecodeError on a non-UTF-8 host."""
    return path.read_text(encoding="utf-8")


def _pyproject() -> dict:
    """Parsed pyproject.toml — the single source of truth for both checks below."""
    return tomllib.loads(_read(_PYPROJECT))


def _is_hidden(path: Path, relative_to: Path) -> bool:
    """True if any path component below ``relative_to`` is hidden or a cache.

    Checked across every component, not just the leaf: ``rglob`` descends into
    ``.mypy_cache/3.11/tests/`` whose leaf name is perfectly ordinary.
    """
    return any(
        part.startswith((".", "__")) or part in _NON_SUITE_DIRS
        for part in path.relative_to(relative_to).parts
    )


def _package_dirs() -> list[Path]:
    """Every directory under ``tests/``, the root included, excluding caches."""
    return [_TESTS_ROOT] + [
        d for d in sorted(_TESTS_ROOT.rglob("*")) if d.is_dir() and not _is_hidden(d, _TESTS_ROOT)
    ]


def _python_sources() -> list[Path]:
    """Every first-party ``.py`` file: the packaged tree plus the test tree.

    Derived from ``[tool.setuptools] package-dir`` rather than hardcoded, so
    the sweep can never quietly walk a ``.venv`` or a nested worktree.

    Story 10.47 (FS-3) replaced the explicit ``py-modules``/``packages`` lists
    this used to read with a ``find`` directive, so there is no longer a list
    of names to expand — the packaged tree is simply everything under the
    configured package directory. Walking it is also the stronger check: a
    module accidentally left out of the distribution still gets swept, whereas
    reading the declaration would have skipped exactly the file most likely to
    be wrong.
    """
    package_dir = Path(_pyproject()["tool"]["setuptools"]["package-dir"][""])
    sources: list[Path] = []
    for root in (_REPO_ROOT / package_dir, _TESTS_ROOT):
        sources.extend(sorted(root.rglob("*.py")))
    return [path for path in sources if path.is_file() and not _is_hidden(path, _REPO_ROOT)]


def test_the_repo_root_anchor_resolves():
    """Fail loudly if the anchor is wrong, instead of passing vacuously.

    Every absence assertion in this module globs a directory. If ``_REPO_ROOT``
    ever pointed somewhere unexpected, those globs would come back empty and
    the whole file would go green while checking nothing.
    """
    assert _PYPROJECT.is_file(), f"No pyproject.toml at the resolved root {_REPO_ROOT}"
    assert _TESTS_ROOT.is_dir(), f"No tests/ directory at the resolved root {_REPO_ROOT}"
    assert (_TESTS_ROOT / "conftest.py").is_file(), "tests/conftest.py missing from the anchor"


def test_no_test_modules_live_outside_the_tests_tree():
    """AC: all 39 test modules live under ``tests/``; nothing strays outside.

    Swept repo-wide, not just at the root. ``testpaths = ["tests"]`` means a
    ``test_*.py`` dropped anywhere else is no longer collected by a bare
    ``pytest`` at all — it doesn't fail, it silently stops running. That is a
    worse failure than the root clutter this story removed, so it gets an
    assertion.
    """
    strays = sorted(
        str(path.relative_to(_REPO_ROOT))
        for path in _REPO_ROOT.rglob("test_*.py")
        if not _is_hidden(path, _REPO_ROOT) and _TESTS_ROOT not in path.parents
    )
    assert not strays, (
        f"{len(strays)} test module(s) outside tests/: {strays}. Anything not "
        "under tests/ is silently skipped by testpaths — move it (git mv, to "
        "preserve history)."
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
        f"found {collisions} across {duplicated}. Nine is the count this layout "
        "shipped with (Story 10.45 AC), not a floor with independent meaning — if "
        "you deliberately merged or renamed a duplicated module, lower it here."
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

    # pytest accepts addopts as a string or a TOML array; normalize both.
    addopts = ini_options.get("addopts", "")
    if isinstance(addopts, list):
        addopts = " ".join(addopts)
    per_file = _PER_FILE_IGNORE_RE.findall(addopts)
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

    Asserts on the imported *value* rather than the conftest's source text, so
    reformatting the declaration can't fail a correct config — and checks the
    named directory actually exists, since ``collect_ignore`` pointing at a
    renamed directory is a silent no-op.
    """
    from tests import conftest

    ignored = getattr(conftest, "collect_ignore", [])
    assert "live" in ignored, (
        f'tests/conftest.py must declare collect_ignore = ["live"] (got {ignored!r}) '
        "— those runners need SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN against a real store."
    )
    for name in ignored:
        assert (_TESTS_ROOT / name).exists(), (
            f"collect_ignore names {name!r}, which does not exist under tests/ — "
            "a stale entry excludes nothing and fails silently."
        )


def test_python_version_config_agrees_across_the_toolchain():
    """AC: requires-python, ruff target-version, and mypy python_version agree.

    Story 10.37 raised ``requires-python`` to ``>=3.11`` without moving the two
    tool configs with it (Story 10.50) — nothing failed when they disagreed, so
    there was no signal until a symptom (stdlib ``tomllib`` misclassified as
    third-party) surfaced downstream. This assertion is the signal for the next
    floor bump: if only one of the three is edited, this test fails immediately
    instead of drifting silently again.
    """
    pyproject = _pyproject()
    requires_python = pyproject["project"]["requires-python"]
    ruff_target = pyproject["tool"]["ruff"]["target-version"]
    mypy_version = pyproject["tool"]["mypy"]["python_version"]

    floor_match = re.match(r">=(\d+)\.(\d+)", requires_python)
    assert floor_match, f"Unexpected requires-python format: {requires_python!r}"
    expected_short = f"{floor_match.group(1)}.{floor_match.group(2)}"
    expected_py_tag = f"py{floor_match.group(1)}{floor_match.group(2)}"

    assert ruff_target == expected_py_tag, (
        f"[tool.ruff] target-version is {ruff_target!r} but requires-python "
        f"{requires_python!r} implies {expected_py_tag!r}. Bump target-version "
        "to match the declared floor."
    )
    assert mypy_version == expected_short, (
        f"[tool.mypy] python_version is {mypy_version!r} but requires-python "
        f"{requires_python!r} implies {expected_short!r}. Bump python_version "
        "to match the declared floor."
    )


def test_no_python_source_points_at_a_pre_move_test_module():
    """AC: no docstring or comment in Python source cites a pre-move test module.

    Scoped to Python source on purpose. The Markdown ledgers (TECH_DEBT.md,
    architectural_tech_debt.md) are append-only historical records whose closed
    entries legitimately name the files as they were at the time; rewriting
    those would falsify the audit trail. Their *forward-looking* lines — reopen
    triggers, "enforced by" pointers — were repointed by hand instead.
    """
    stale: dict[str, list[str]] = {}
    for path in _python_sources():
        hits = sorted(set(_PRE_MOVE_PATH_RE.findall(_read(path))))
        if hits:
            stale[str(path.relative_to(_REPO_ROOT))] = hits
    assert not stale, f"Stale pre-move test references in: {stale}"
