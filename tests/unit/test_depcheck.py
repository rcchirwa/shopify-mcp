"""Offline tests for depcheck — environment ↔ pyproject dependency-drift check.

Story 10.37: declared deps (cachetools, types-cachetools) were absent from the
*actual* environments, so the MCP server boot-crashed and mypy failed. depcheck
is the durable safeguard: it compares every dependency declared in
pyproject.toml against what is actually installed and fails loudly (with an
actionable message) when an environment has drifted out of sync.
"""

from pathlib import Path

import pytest

import depcheck

# A distribution name that cannot plausibly be installed, used to simulate a
# declared-but-absent dependency (the Story 10.37 failure mode).
_ABSENT_DIST = "shopify-mcp-definitely-not-installed-xyz"


def _write_pyproject(tmp_path: Path, runtime: list[str], dev: list[str] | None = None) -> Path:
    """Write a minimal pyproject.toml under tmp_path and return its path."""
    lines = ["[project]", 'name = "x"', "dependencies = ["]
    lines += [f'    "{r}",' for r in runtime]
    lines.append("]")
    if dev is not None:
        lines += ["[project.optional-dependencies]", "dev = ["]
        lines += [f'    "{r}",' for r in dev]
        lines.append("]")
    path = tmp_path / "pyproject.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- distribution_name ------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("cachetools>=5,<6", "cachetools"),
        ("gql[requests]>=3.4.0,<4", "gql"),
        ("python-dotenv>=1.0.0,<2", "python-dotenv"),
        ("types-cachetools>=5,<7", "types-cachetools"),
        ("  nh3>=0.2,<0.3  ", "nh3"),
        ("mcp", "mcp"),
    ],
)
def test_distribution_name_strips_extras_and_specifiers(requirement: str, expected: str) -> None:
    assert depcheck.distribution_name(requirement) == expected


def test_distribution_name_rejects_unparseable_requirement() -> None:
    with pytest.raises(ValueError, match="Cannot parse distribution name"):
        depcheck.distribution_name("!!! not a requirement")


# --- load_declared_dependencies --------------------------------------------


def test_load_declared_dependencies_groups_runtime_and_extras(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, runtime=["requests>=2,<3"], dev=["pytest>=7,<9"])

    groups = depcheck.load_declared_dependencies(path)

    assert groups == {"project": ["requests>=2,<3"], "dev": ["pytest>=7,<9"]}


def test_load_declared_dependencies_handles_no_optional_dependencies(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, runtime=["requests>=2,<3"])

    groups = depcheck.load_declared_dependencies(path)

    assert groups == {"project": ["requests>=2,<3"]}


def test_load_declared_dependencies_reads_the_real_pyproject() -> None:
    # The shipped pyproject declares cachetools (runtime) and types-cachetools
    # (dev) — the two deps Story 10.37 found missing from drifted environments.
    groups = depcheck.load_declared_dependencies()

    assert any(r.startswith("cachetools") for r in groups["project"])
    assert any(r.startswith("types-cachetools") for r in groups["dev"])


# --- find_missing_dependencies ---------------------------------------------


def test_find_missing_flags_declared_but_absent_dependency() -> None:
    groups = {"project": ["pytest>=7", f"{_ABSENT_DIST}>=1"]}

    missing = depcheck.find_missing_dependencies(groups)

    assert missing == {"project": [f"{_ABSENT_DIST}>=1"]}


def test_find_missing_empty_when_every_dependency_present() -> None:
    groups = {"project": ["pytest>=7"], "dev": ["coverage>=7"]}

    assert depcheck.find_missing_dependencies(groups) == {}


def test_find_missing_reports_per_group() -> None:
    groups = {
        "project": [f"{_ABSENT_DIST}>=1"],
        "dev": ["pytest>=7", f"{_ABSENT_DIST}-two>=1"],
    }

    missing = depcheck.find_missing_dependencies(groups)

    assert missing == {
        "project": [f"{_ABSENT_DIST}>=1"],
        "dev": [f"{_ABSENT_DIST}-two>=1"],
    }


# --- check ------------------------------------------------------------------


def test_check_passes_for_a_fully_synced_environment(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, runtime=["pytest>=7"], dev=["coverage>=7"])

    assert depcheck.check(path) == {}


def test_check_flags_drift_against_a_pyproject(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, runtime=[f"{_ABSENT_DIST}>=1"])

    assert depcheck.check(path) == {"project": [f"{_ABSENT_DIST}>=1"]}


# Asserting the *live* environment is fully synced is the job of the
# `shopify-mcp-check-deps` CI step (which runs against a fresh install), not of
# the offline suite — coupling a unit test to the ambient venv makes "test
# failed" and "env drifted" indistinguishable. So main()'s in-sync path is
# driven through a stubbed check() rather than the real environment.


# --- main (CLI entry) -------------------------------------------------------


def test_main_returns_zero_and_reports_ok_when_synced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(depcheck, "check", dict)

    rc = depcheck.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "All declared dependencies are installed" in out


def test_main_returns_one_and_lists_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        depcheck,
        "check",
        lambda: {"project": ["cachetools>=5,<6"], "dev": ["types-cachetools>=5,<7"]},
    )

    rc = depcheck.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "out of sync" in out
    assert "cachetools>=5,<6" in out
    assert "types-cachetools>=5,<7" in out
    # Names the group each missing dep came from and the fix command.
    assert "runtime" in out
    assert "[dev]" in out
    assert "pip install --require-hashes -r requirements-dev.lock" in out
    assert "pip install --no-deps -e ." in out
