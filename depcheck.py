"""Dependency-drift check — does this environment match pyproject.toml?

Story 10.37 (env/ops): the declared deps were correct, but the *actual*
environments were stale. cachetools was absent from the interpreter the MCP
server launched under (boot crash: ``ModuleNotFoundError: No module named
'cachetools'``), and ``types-cachetools`` was absent from ``.venv`` (mypy
``import-untyped``). Both are the same failure: an environment drifted out of
sync with the dependencies pyproject.toml declares.

This module is the durable safeguard. It reads every dependency declared in
pyproject.toml — the runtime ``[project].dependencies`` plus each
``[project.optional-dependencies]`` extra — and checks each one is actually
installed in the *current* interpreter, reporting any that are missing with the
exact command to re-sync. Run it after pulling new dependencies, wire it into
CI, or point it at the interpreter the MCP server launches under to catch the
boot-crash drift before it happens.

pyproject.toml is parsed directly (not the installed package metadata) on
purpose: ``importlib.metadata`` reflects what was declared at the *last*
install, so a stale env that never reinstalled would report itself in sync.
pyproject.toml on disk is the source of truth; the installed env is what we
validate against it.

Scope — this checks *presence*, the failure mode Story 10.37 hit (a declared
distribution simply absent). It deliberately does not:

- validate version specifiers (``cachetools>=5,<6`` passes as long as *some*
  cachetools is installed) — pip enforces the declared bounds at install time;
- recurse into a dependency's own extras (``gql[requests]`` is checked as
  ``gql``; the ``requests`` extra's contents are independently declared here as
  the top-level ``requests`` dependency anyway);
- evaluate environment markers — the project declares none today; a marked
  dependency (e.g. ``foo; python_version < '3.11'``) would need marker-aware
  filtering added here before it could be reported correctly;
- scan for known CVEs in the resolved versions — that gap is Story 10.40's
  (SEC-13, SEC-14): ``requirements.lock`` / ``requirements-dev.lock``
  (``pip-compile --generate-hashes``) pin exact, hash-verified versions, and
  the CI ``dependency-audit`` job runs ``pip-audit`` against the lockfile on
  every PR. depcheck stays this scan's presence-only complement rather than
  growing version/CVE logic of its own — see README.md's "Regenerating the
  lockfile" section for the re-sync workflow.

It reads the pyproject.toml beside this module, i.e. the source tree of an
editable install — the only layout this dev/ops tool is meant to run in.

Run with ``python -m depcheck`` or the ``shopify-mcp-check-deps`` console
script. Exit code 0 = in sync, 1 = drift detected.
"""

import importlib.metadata
import re
from pathlib import Path

import tomllib

# pyproject.toml lives beside this module at the repo root.
_PYPROJECT = Path(__file__).resolve().parent / "pyproject.toml"

# Leading token of a PEP 508 requirement is the distribution name; it ends at
# the first extras bracket, version specifier, marker, or whitespace.
_DIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def distribution_name(requirement: str) -> str:
    """Return the PyPI distribution name from a requirement string.

    ``"gql[requests]>=3.4.0,<4"`` -> ``"gql"``; ``"cachetools>=5,<6"`` ->
    ``"cachetools"``. Extras and version specifiers are stripped.
    """
    match = _DIST_NAME_RE.match(requirement.strip())
    if match is None:
        raise ValueError(f"Cannot parse distribution name from requirement: {requirement!r}")
    return match.group(0)


def load_declared_dependencies(pyproject_path: Path = _PYPROJECT) -> dict[str, list[str]]:
    """Return declared requirement strings grouped by source.

    The ``"project"`` key holds ``[project].dependencies``; each remaining key
    is an extra from ``[project.optional-dependencies]`` (e.g. ``"dev"``).
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups: dict[str, list[str]] = {"project": list(project.get("dependencies", []))}
    for extra, requirements in project.get("optional-dependencies", {}).items():
        groups[extra] = list(requirements)
    return groups


def _is_installed(dist_name: str) -> bool:
    """True if a distribution by this name is importable metadata in this env."""
    try:
        importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def find_missing_dependencies(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return, per group, the declared requirements absent from this env.

    Groups with no missing dependency are omitted, so an empty dict means the
    environment satisfies every declared dependency.
    """
    missing: dict[str, list[str]] = {}
    for group, requirements in groups.items():
        absent = [req for req in requirements if not _is_installed(distribution_name(req))]
        if absent:
            missing[group] = absent
    return missing


def check(pyproject_path: Path = _PYPROJECT) -> dict[str, list[str]]:
    """Return declared dependencies missing from the current environment."""
    return find_missing_dependencies(load_declared_dependencies(pyproject_path))


def main() -> int:
    """CLI entry: report drift, exit 0 if in sync and 1 if anything is missing."""
    missing = check()
    if not missing:
        print(f"All declared dependencies are installed ({_PYPROJECT}).")
        return 0
    print("Environment is out of sync with pyproject.toml — missing declared dependencies:")
    for group, requirements in missing.items():
        label = "runtime" if group == "project" else f"[{group}]"
        for requirement in requirements:
            print(f"  - {requirement}  (declared in {label})")
    print(
        "\nRe-sync this environment with:"
        "\n  pip install --require-hashes -r requirements-dev.lock"
        "\n  pip install --no-deps -e ."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover
