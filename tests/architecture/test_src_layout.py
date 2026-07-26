"""Structural guard for the ``src/`` layout (Story 10.47 / FS-3).

Six generic top-level names — ``depcheck``, ``logging_config``, ``settings``,
``shopify_client``, ``shopify_mcp`` plus the ``tools``/``validators``/``shopify``
packages — used to be installed onto the import path of any environment that
installed this project. ``shopify`` is the top-level module owned by the
**ShopifyAPI** distribution on PyPI, so installing that mainstream library into
the same venv put two different ``shopify`` packages on ``sys.path``; which one
won depended on path order, and the failure mode was a silent wrong import
rather than an error.

The flat layout hid a second class of bug. Because the packages sat at the repo
root, ``pytest`` imported them from the working tree and never from the
installed distribution, so a module accidentally omitted from the packaging
declaration passed the entire CI gate and only broke for someone doing a real
(non-editable) install.

This module is the executable spec for the fix: one top-level name
(``shopify_mcp``) living under ``src/``, everything else a sub-package of it.

Two ACs are deliberately **not** asserted here, because they can only be proven
by building and installing into throwaway environments — which needs network
access and several seconds per run, and would make this suite depend on PyPI:

- a wheel built from the tree installs and imports with no repo on ``sys.path``;
- installing ShopifyAPI alongside this package no longer yields two competing
  ``shopify`` modules.

Both are verified out of band (Story 10.47 implementation steps 10 and 11) and
recorded on the card. What *is* asserted below is the property that makes both
of them true — no legacy top-level name resolves into this repo any more —
which is the part that can regress silently in an ordinary PR.

Usage:
  pytest tests/architecture/test_src_layout.py -v
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_SRC_ROOT = _REPO_ROOT / "src"
_PACKAGE_ROOT = _SRC_ROOT / "shopify_mcp"

# The one top-level name this distribution is allowed to install.
_PACKAGE = "shopify_mcp"

# Every top-level name the flat layout used to install. ``shopify_mcp`` is
# absent on purpose: it is the survivor, and the sweeps below must not flag the
# namespace this story moves everything *into*.
_LEGACY_TOP_LEVEL = (
    "depcheck",
    "logging_config",
    "settings",
    "shopify",
    "shopify_client",
    "tools",
    "validators",
)

# The two modules that are entry points rather than library code: not unit
# tested, verified by actually launching them. Under the flat layout the same
# policy was expressed by leaving ``shopify_mcp`` out of the coverage ``source``
# list; under a single package it has to be an explicit ``omit``.
_ENTRY_POINT_MODULES = ("server.py", "__main__.py")

# Matches an absolute import of a legacy top-level name. Anchored at the start
# of a line so this module's own ``_LEGACY_TOP_LEVEL`` tuple can't trip it, and
# ``\b`` keeps ``shopify`` from matching inside ``shopify_mcp`` (``_`` is a word
# character, so there is no boundary there).
_LEGACY_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+(" + "|".join(_LEGACY_TOP_LEVEL) + r")\b",
    re.MULTILINE,
)

# Matches a stale string-literal mock target, e.g. a patch target beginning
# ``tools.`` or ``shopify_client.``. These are the 32+4 targets that no static
# tool can see: a stale one yields a green test that patches nothing.
#
# The whole quoted string must be a pure dotted path — a legacy root, one or
# more dotted attributes, then the *same* quote character (backreference). That
# precision is what lets this module sweep itself along with every other source
# file: prose such as "depcheck._PYPROJECT resolves to ..." contains a dotted
# path but does not consist of one, so it is not a match, while every real
# target (verified: all 36 are plain literals, none built by f-string or
# concatenation) is. Anchoring on quote-to-quote beats excluding this file,
# because a self-exclusion would also hide a genuinely stale target added here.
_MOCK_TARGET_RE = re.compile(
    r"""(["'])(?:""" + "|".join(_LEGACY_TOP_LEVEL) + r""")(?:\.[A-Za-z_][A-Za-z0-9_]*)+\1""",
)

# Directories that are never first-party source: VCS internals, caches, and
# virtualenvs. Mirrors test_layout.py's _NON_SUITE_DIRS.
_NON_SOURCE_DIRS = frozenset(
    {".git", ".venv", "venv", ".claude", "build", "dist", "node_modules", ".mypy_cache"}
)

# Resolves each name in argv to the filesystem path backing it, or null. Run in
# a subprocess so the answer reflects a clean interpreter rather than whatever
# this pytest session has already imported into ``sys.modules`` — ``find_spec``
# short-circuits on a cached module, which would make the result depend on test
# ordering.
_RESOLVE_PROBE = """
import importlib.util, json, sys

resolved = {}
for name in sys.argv[1:]:
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        spec = None
    origin = None
    if spec is not None:
        origin = spec.origin
        if origin in (None, "namespace") and spec.submodule_search_locations:
            locations = list(spec.submodule_search_locations)
            origin = locations[0] if locations else None
    resolved[name] = origin
json.dump({"resolved": resolved, "sys_path": sys.path}, sys.stdout)
"""


def _read(path: Path) -> str:
    """Read as UTF-8 explicitly — the tree is full of en/em-dashes, and the
    locale default would raise UnicodeDecodeError on a non-UTF-8 host."""
    return path.read_text(encoding="utf-8")


def _pyproject() -> dict:
    return tomllib.loads(_read(_PYPROJECT))


def _first_party_sources() -> list[Path]:
    """Every first-party ``.py`` file: the packaged tree plus the test tree.

    Walks ``src/`` and ``tests/`` directly rather than deriving the roots from
    the packaging declaration. Under a ``find`` directive there is no explicit
    package list to read, and walking the directory is the stronger check
    anyway: a module accidentally left out of the distribution still gets swept.
    """
    sources: list[Path] = []
    for root in (_SRC_ROOT, _REPO_ROOT / "tests"):
        sources.extend(
            path
            for path in sorted(root.rglob("*.py"))
            if not any(
                part.startswith(".") or part in _NON_SOURCE_DIRS
                for part in path.relative_to(_REPO_ROOT).parts
            )
        )
    return sources


def _resolve_top_level_names(names: tuple[str, ...]) -> dict[str, str | None]:
    """Map each top-level name to the path backing it in the installed env.

    ``-P`` (3.11+) keeps the current directory off ``sys.path``, so running
    this from the repo root cannot make the working tree masquerade as an
    installed distribution — which is the exact confusion this story exists to
    remove.
    """
    completed = subprocess.run(
        [sys.executable, "-P", "-c", _RESOLVE_PROBE, *names],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    assert _REPO_ROOT.as_posix() not in payload["sys_path"], (
        f"The probe interpreter has the repo root on sys.path ({payload['sys_path']}) — "
        "it would resolve the working tree instead of the installed distribution."
    )
    return payload["resolved"]


def _is_under_repo(origin: str | None) -> bool:
    if origin is None:
        return False
    return Path(origin) == _REPO_ROOT or _REPO_ROOT in Path(origin).parents


# --- the tree itself --------------------------------------------------------


def test_the_repo_root_anchor_resolves():
    """Fail loudly if the anchor is wrong, instead of passing vacuously.

    Several checks below are *absence* assertions over a glob; against a
    non-existent tree they would pass while checking nothing.
    """
    assert _PYPROJECT.is_file(), f"No pyproject.toml at the resolved root {_REPO_ROOT}"
    assert _SRC_ROOT.is_dir(), f"No src/ directory at the resolved root {_REPO_ROOT}"


def test_the_package_lives_under_src_with_both_entry_points():
    """AC: the package is ``src/shopify_mcp/``, importable and runnable."""
    assert _PACKAGE_ROOT.is_dir(), "src/shopify_mcp/ is missing"
    for name in ("__init__.py", "__main__.py", "server.py", "client.py"):
        assert (_PACKAGE_ROOT / name).is_file(), f"src/shopify_mcp/{name} is missing"
    for sub in ("shopify", "tools", "validators"):
        assert (_PACKAGE_ROOT / sub / "__init__.py").is_file(), (
            f"src/shopify_mcp/{sub}/ must be a sub-package of shopify_mcp"
        )


def test_src_holds_exactly_one_top_level_name():
    """AC: exactly one top-level name is packaged.

    A second directory or loose module under ``src/`` would be installed as its
    own top-level name and reintroduce the collision this story closes.

    ``*.egg-info`` is skipped: ``pip install -e .`` writes it beside the
    package (it is gitignored, and setuptools' own discovery ignores it), so
    counting it would make this assertion depend on whether the tree happens to
    have been installed.
    """
    entries = sorted(
        path.name
        for path in _SRC_ROOT.iterdir()
        if not path.name.startswith((".", "__"))
        and path.suffix != ".egg-info"
        and (path.is_dir() or path.suffix == ".py")
    )
    assert entries == [_PACKAGE], f"src/ must hold only {_PACKAGE}/, found: {entries}"


def test_no_legacy_module_remains_at_the_repo_root():
    """AC: the flat-layout modules and packages are gone from the root."""
    strays = sorted(
        name
        for name in (*(f"{n}.py" for n in _LEGACY_TOP_LEVEL), *_LEGACY_TOP_LEVEL, "shopify_mcp.py")
        if (_REPO_ROOT / name).exists()
    )
    assert not strays, (
        f"Flat-layout modules still at the repo root: {strays}. They belong under "
        "src/shopify_mcp/ (git mv, to preserve history)."
    )


# --- the packaging + tool declarations --------------------------------------


def test_setuptools_declares_the_src_package_dir():
    """AC: packaging points at ``src/`` and names no loose top-level modules."""
    setuptools = _pyproject()["tool"]["setuptools"]

    assert setuptools.get("package-dir") == {"": "src"}, (
        f'[tool.setuptools] must declare package-dir = {{"" = "src"}}, '
        f"got {setuptools.get('package-dir')!r}"
    )
    assert "py-modules" not in setuptools, (
        f"[tool.setuptools] still declares py-modules: {setuptools.get('py-modules')!r}. "
        "Loose top-level modules are exactly what this story removes."
    )
    where = _pyproject()["tool"]["setuptools"]["packages"]["find"]["where"]
    assert where == ["src"], f"[tool.setuptools.packages.find] where must be ['src'], got {where!r}"


def test_console_scripts_point_into_the_package():
    """AC: both console scripts resolve inside ``shopify_mcp``.

    README's Claude Desktop registration section launches the server through
    ``.venv/bin/shopify-mcp``, so a stale target here breaks the documented
    setup rather than any test.
    """
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["shopify-mcp"] == "shopify_mcp.server:main", scripts
    assert scripts["shopify-mcp-check-deps"] == "shopify_mcp.depcheck:main", scripts


def test_no_tool_config_names_a_legacy_top_level_path():
    """AC: coverage, mypy and ruff all point at the new tree.

    A config left pointing at ``tools`` or ``validators`` does not error — it
    silently measures, type-checks, or lints nothing, which is how a 100%
    coverage gate can stay green over uncovered code.
    """
    pyproject = _pyproject()
    configured = {
        "[tool.coverage.run] source": pyproject["tool"]["coverage"]["run"]["source"],
        "[tool.mypy] files": pyproject["tool"]["mypy"]["files"],
        "[tool.ruff.lint.per-file-ignores]": list(
            pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]
        ),
    }
    stale = {
        key: [entry for entry in values if entry.split("/")[0] in _LEGACY_TOP_LEVEL]
        for key, values in configured.items()
    }
    stale = {key: hits for key, hits in stale.items() if hits}
    assert not stale, f"Tool config still points at pre-move paths: {stale}"

    assert configured["[tool.coverage.run] source"] == [_PACKAGE], (
        f"coverage must measure the whole package: {configured['[tool.coverage.run] source']!r}"
    )
    assert f"src/{_PACKAGE}" in configured["[tool.mypy] files"], (
        f"mypy must type-check src/{_PACKAGE}: {configured['[tool.mypy] files']!r}"
    )


def test_coverage_omits_only_the_entry_points():
    """AC (guard): the coverage escape hatch stays the size it started at.

    Folding seven packages into one meant the "entry points aren't unit tested"
    carve-out had to become an explicit ``omit``. An ``omit`` list is a much
    easier place to hide an untested module than a ``source`` list, so pin its
    contents: coverage may skip the two entry points and nothing else.
    """
    omit = _pyproject()["tool"]["coverage"]["run"].get("omit", [])
    expected = sorted(f"*/{_PACKAGE}/{name}" for name in _ENTRY_POINT_MODULES)
    assert sorted(omit) == expected, (
        f"[tool.coverage.run] omit must be exactly {expected} — got {omit!r}. "
        "Anything else belongs under the 100% gate."
    )


# --- the sweeps that static tooling cannot do -------------------------------


def test_no_first_party_source_imports_a_legacy_top_level_name():
    """AC: every import went through the rename, production and tests alike."""
    stale: dict[str, list[str]] = {}
    for path in _first_party_sources():
        hits = sorted(set(_LEGACY_IMPORT_RE.findall(_read(path))))
        if hits:
            stale[str(path.relative_to(_REPO_ROOT))] = hits
    assert not stale, (
        f"Absolute imports of pre-move top-level names remain in: {stale}. "
        f"They must be imported as {_PACKAGE}.<name>."
    )


def test_no_stale_string_literal_mock_target_remains():
    """AC: zero stale patch targets — the landmine of this refactor.

    Mock targets are strings, so neither mypy nor ruff can see them. A stale
    target does not raise: ``mock`` happily patches the attribute on whatever
    module the old dotted path still resolves to, or — once the old name is
    gone — the test fails loudly only if it is lucky. Getting a green suite
    that patches nothing is the realistic bad outcome, so this sweep is the
    only thing standing between the rename and a silently unguarded suite.
    """
    stale: dict[str, list[str]] = {}
    for path in _first_party_sources():
        hits = sorted({match.group(0) for match in _MOCK_TARGET_RE.finditer(_read(path))})
        if hits:
            stale[str(path.relative_to(_REPO_ROOT))] = hits
    assert not stale, (
        f"Stale string-literal mock targets remain in: {stale}. Repoint each to "
        f"{_PACKAGE}.<module>… — a stale target patches nothing and still passes."
    )


# --- what the installed environment actually exposes ------------------------


def test_the_package_resolves_from_the_installed_distribution():
    """Positive control for the sweep below — proves the probe really resolves.

    Without this, a probe that silently returned nothing for every name would
    make the absence assertion below pass while checking nothing.
    """
    origin = _resolve_top_level_names((_PACKAGE,))[_PACKAGE]
    assert origin is not None, (
        f"{_PACKAGE} does not resolve in this environment — run `pip install -e .`"
    )
    assert _is_under_repo(origin) and "src" in Path(origin).parts, (
        f"{_PACKAGE} resolves to {origin!r}, which is not the src/ tree of this repo."
    )


def test_no_legacy_top_level_name_resolves_into_this_repo():
    """AC: this distribution installs exactly one top-level name.

    Asserts on *where* each legacy name resolves rather than whether it
    resolves at all. ``shopify`` resolving to the third-party ShopifyAPI
    library is the desired end state, not a failure — what must never happen
    again is a top-level name resolving back into this repo, because that is
    the half of the collision this project controls.
    """
    resolved = _resolve_top_level_names(_LEGACY_TOP_LEVEL)
    offenders = {name: origin for name, origin in resolved.items() if _is_under_repo(origin)}
    assert not offenders, (
        f"Legacy top-level names still resolve into this repo: {offenders}. "
        f"Each must be reachable only as {_PACKAGE}.<name>."
    )


# --- the path constants whose depth changed ---------------------------------


def test_the_write_audit_log_still_resolves_to_the_repo_root():
    """AC: the audit log lands at the repo root, not inside src/.

    ``tools/_log.py`` derived it from ``dirname(dirname(__file__))``, which was
    the repo root at the old depth and would be ``src/shopify_mcp/`` at the new
    one — silently relocating the write-audit trail.
    """
    from shopify_mcp.tools import _log

    assert Path(_log.LOG_FILE).resolve() == _REPO_ROOT / "aon_mcp_log.txt", (
        f"LOG_FILE resolves to {_log.LOG_FILE!r}, not the repo root."
    )


def test_depcheck_still_reads_the_repo_root_pyproject():
    """AC: the dependency-drift check still finds pyproject.toml."""
    from shopify_mcp import depcheck

    assert depcheck._PYPROJECT.resolve() == _PYPROJECT, (
        f"depcheck._PYPROJECT resolves to {depcheck._PYPROJECT!r}, not the repo root."
    )


def test_both_env_paths_still_resolve_to_the_repo_root():
    """AC: the server boots with credentials.

    Both the server and the client pin ``.env`` to the repo root so loading is
    independent of the working directory the MCP process is launched with
    (Claude Desktop launches subprocesses with CWD=/). Getting this wrong means
    the server starts with no credentials.
    """
    from shopify_mcp import client, server

    expected = _REPO_ROOT / ".env"
    assert server._ENV_PATH.resolve() == expected, server._ENV_PATH
    assert client._ENV_PATH.resolve() == expected, client._ENV_PATH
