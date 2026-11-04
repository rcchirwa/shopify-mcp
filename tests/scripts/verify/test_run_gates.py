"""Tests for ``scripts/verify/run_gates.py`` as adapted to shopify-mcp.

Holocron's own run_gates tests build Holocron's jobs, so they were not ported. These
cover the adapted parts: the step and job sets for ``.github/workflows/test.yml``, the
``[project.scripts]`` mapping, and that every gate runs the candidate tree (its
``src/`` on ``PYTHONPATH``, a fresh bytecode prefix). ``main`` runs against a real
temporary git repository; loading, venv building and gate execution are faked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.verify import run_gates as rg

REPO_ROOT = Path(__file__).resolve().parents[3]
GIT = shutil.which("git") or "git"


def _setup() -> dict[str, Any]:
    return {"uses": "actions/setup-python", "with": {"python-version": "3.11"}}


INSTALL_DEV = {
    "name": "Install pinned dependencies from lockfile",
    "run": "pip install --require-hashes -r requirements-dev.lock",
}
INSTALL_PACKAGE = {
    "name": "Install package (deps already pinned above)",
    "run": "pip install --no-deps -e .",
}

# The shape of .github/workflows/test.yml.
DOC: dict[str, Any] = {
    "jobs": {
        "offline-tests": {
            "steps": [
                _setup(),
                INSTALL_DEV,
                INSTALL_PACKAGE,
                {"name": "Run offline tests under coverage", "run": "coverage run -m pytest -v"},
                {"name": "Enforce 100% coverage", "run": "coverage report -m --fail-under=100"},
            ]
        },
        "lint": {
            "steps": [
                _setup(),
                INSTALL_DEV,
                INSTALL_PACKAGE,
                {"name": "Dependency drift check", "run": "shopify-mcp-check-deps"},
                {"name": "Ruff lint", "run": "ruff check ."},
                {"name": "Ruff format check", "run": "ruff format --check ."},
                {"name": "Mypy", "run": "mypy"},
            ]
        },
        "commit-messages": {
            "steps": [
                {
                    "name": "Reject AI attribution in commit messages",
                    "run": "set -eu\nfor sha in $(git rev-list x); do\n  echo\ndone\n",
                }
            ]
        },
        "dependency-audit": {
            "steps": [
                _setup(),
                {
                    "name": "Install pinned pip-audit",
                    "run": "pip install --require-hashes -r requirements-audit.lock",
                },
                {
                    "name": "pip-audit against requirements.lock",
                    "run": "pip-audit -r requirements.lock --strict",
                },
            ]
        },
    }
}


def test_plan_runs_the_workflow_gates_and_skips_the_rest() -> None:
    jobs, classified, reports = rg.plan(DOC)
    packaging = ["python", "-m", "pip", "install", "--dry-run", "--no-deps", "-e", "."]
    assert jobs == {
        "offline-tests": [
            packaging,
            ["coverage", "run", "-m", "pytest", "-v"],
            ["coverage", "report", "-m", "--fail-under=100"],
        ],
        "lint": [
            packaging,
            ["shopify-mcp-check-deps"],
            ["ruff", "check", "."],
            ["ruff", "format", "--check", "."],
            ["mypy"],
        ],
        "dependency-audit": [["pip-audit", "-r", "requirements.lock", "--strict"]],
    }
    skipped = {(c["job"], c["step"]): c["reason"] for c in classified if not c["run"]}
    assert skipped[("commit-messages", None)] == rg.SKIP_JOBS["commit-messages"]
    assert skipped[("lint", "Install pinned dependencies from lockfile")] == (
        "ci_venvs.py owns install steps"
    )
    ran = {(c["job"], c["step"]) for c in classified if c["run"]}
    assert ("lint", "Install package (deps already pinned above)") in ran
    assert reports == set()


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d["jobs"].update(integration=d["jobs"]["lint"]), "unknown job"),
        (
            lambda d: d["jobs"]["lint"]["steps"].append({"name": "Run ruff", "run": "ruff ."}),
            "unknown CI run step: lint/Run ruff",
        ),
        (
            lambda d: d["jobs"]["lint"]["steps"][4].update(run="ruff check . && echo ok"),
            "shell syntax in lint/Ruff lint",
        ),
        (
            lambda d: d["jobs"]["lint"]["steps"][4].update(run="pytest -q"),
            "unknown command in lint/Ruff lint",
        ),
        (
            lambda d: d["jobs"]["lint"]["steps"][2].update(run="pip install -e ."),
            "unexpected candidate install in lint/Install package",
        ),
    ],
    ids=["unknown-job", "unknown-step", "shell-syntax", "unknown-command", "candidate-install"],
)
def test_plan_refuses_what_it_cannot_reproduce(edit: Any, message: str) -> None:
    doc = json.loads(json.dumps(DOC))
    edit(doc)
    with pytest.raises(rg.Refusal, match=message):
        rg.plan(doc)


def test_project_scripts_reads_the_entry_points_from_pyproject() -> None:
    scripts = rg.project_scripts(REPO_ROOT, "100644")
    assert scripts["shopify-mcp-check-deps"] == ("shopify_mcp.depcheck", "main")


def test_project_scripts_refuses_an_entry_point_that_is_not_module_colon_function(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\nx = "shopify_mcp.depcheck:main; import os"\n'
    )
    with pytest.raises(rg.Refusal, match="unsupported entry point: x"):
        rg.project_scripts(tmp_path, "100644")


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [GIT, "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "test.yml").write_text("# handed to main by the test\n")
    shutil.copyfile(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    (root / ".gitignore").write_text("*.egg-info/\n")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "c")
    return root.resolve()


class Gates:
    """Records each gate's argv and environment; every gate exits 0."""

    def __init__(self) -> None:
        self.runs: list[tuple[list[str], dict[str, str], Path]] = []

    def execute(self, argv: list[str], *, cwd: Path, env: dict[str, str], stdout: Any) -> int:
        self.runs.append((argv, env, cwd))
        assert Path(env["PYTHONPYCACHEPREFIX"]).is_dir(), "the prefix must exist while gates run"
        return 0


def _run(candidate: Path, tmp_path: Path, gates: Gates) -> tuple[int, Path]:
    evidence = tmp_path / "evidence"
    code = rg.main(
        [
            "--root",
            str(candidate),
            "--candidate",
            _git(candidate, "rev-parse", "HEAD"),
            "--evidence",
            str(evidence),
            "--git-path",
            GIT,
        ],
        load=lambda _: DOC,
        build=lambda _: "".join(f"{job} /venvs/{job}\n" for job in rg.plan(DOC)[0]),
        execute=gates.execute,
        which=lambda name: GIT if name == "git" else None,
        platform="linux",
        emit=lambda _: None,
    )
    return code, evidence


def test_every_gate_runs_the_candidate_tree(candidate: Path, tmp_path: Path) -> None:
    gates = Gates()
    code, evidence = _run(candidate, tmp_path, gates)
    assert code == 0
    assert len(gates.runs) == 9, "every planned gate must have run"
    prefixes = set()
    for _, env, cwd in gates.runs:
        assert cwd == candidate
        assert env["PYTHONPATH"] == str(candidate / "src")
        prefix = Path(env["PYTHONPYCACHEPREFIX"])
        assert not prefix.is_relative_to(candidate)
        prefixes.add(prefix)
    assert len(prefixes) == 1
    assert not prefixes.pop().exists(), "the bytecode prefix is removed after the gates"
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["exit"] == 0
    assert set(summary["jobs"]) == {"offline-tests", "lint", "dependency-audit"}


def test_a_project_script_runs_its_entry_point_in_the_job_venv(
    candidate: Path, tmp_path: Path
) -> None:
    gates = Gates()
    assert _run(candidate, tmp_path, gates)[0] == 0
    argvs = [argv for argv, _, _ in gates.runs]
    assert [
        "/venvs/lint/bin/python",
        "-c",
        "import sys; from shopify_mcp.depcheck import main; sys.exit(main())",
    ] in argvs
    assert ["/venvs/lint/bin/ruff", "check", "."] in argvs


def test_the_packaging_check_runs_pip_from_the_job_venv(candidate: Path, tmp_path: Path) -> None:
    gates = Gates()
    assert _run(candidate, tmp_path, gates)[0] == 0
    argvs = [argv for argv, _, _ in gates.runs]
    check = ["-m", "pip", "install", "--dry-run", "--no-deps", "-e", "."]
    assert ["/venvs/offline-tests/bin/python", *check] in argvs
    assert ["/venvs/lint/bin/python", *check] in argvs


def test_the_venv_builder_is_told_the_candidate_root(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def capture(argv: list[str], **_: Any) -> bytes:
        seen.append(argv)
        return b""

    rg.build_venvs(tmp_path, capture_fn=capture)
    argv = seen[0]
    assert argv[argv.index("--root") + 1] == str(tmp_path)
    assert argv[argv.index("--workflow") + 1] == str(tmp_path / rg.WORKFLOW)


def test_the_bytecode_prefix_is_removed_when_a_gate_raises(candidate: Path, tmp_path: Path) -> None:
    seen: list[Path] = []

    class Broken(Gates):
        def execute(self, argv: list[str], *, cwd: Path, env: dict[str, str], stdout: Any) -> int:
            seen.append(Path(env["PYTHONPYCACHEPREFIX"]))
            raise RuntimeError("a gate runner bug")

    with pytest.raises(RuntimeError, match="a gate runner bug"):
        _run(candidate, tmp_path, Broken())
    assert len(seen) == 1
    assert not seen[0].exists()


def _stale_egg_info(candidate: Path) -> Path:
    """An ignored egg-info left by an older install, dated well before any gate."""
    pkg_info = candidate / "src" / "shopify_mcp.egg-info" / "PKG-INFO"
    pkg_info.parent.mkdir(parents=True)
    pkg_info.write_text("Name: shopify-mcp\nVersion: 0.0.0-stale\n")
    os.utime(pkg_info, (1_000_000_000, 1_000_000_000))
    return pkg_info


class Refreshing(Gates):
    """The packaging check rewrites PKG-INFO, as setuptools does in a real run."""

    def __init__(self, pkg_info: Path) -> None:
        super().__init__()
        self.pkg_info = pkg_info

    def execute(self, argv: list[str], *, cwd: Path, env: dict[str, str], stdout: Any) -> int:
        if "--dry-run" in argv:
            self.pkg_info.write_text("Name: shopify-mcp\nVersion: 0.1.0\n")
        return super().execute(argv, cwd=cwd, env=env, stdout=stdout)


def test_metadata_the_packaging_check_did_not_refresh_fails_the_gate(
    candidate: Path, tmp_path: Path
) -> None:
    _stale_egg_info(candidate)
    gates = Gates()
    code, evidence = _run(candidate, tmp_path, gates)
    assert code == 1
    assert len(gates.runs) == 1, "the job stops at the packaging check"
    assert (evidence / "offline-tests-01.exit").read_text() == "1\n"
    assert (
        "metadata not refreshed by the packaging check: src/shopify_mcp.egg-info"
        in (evidence / "offline-tests-01.log").read_text()
    )


def test_metadata_the_packaging_check_refreshed_passes(candidate: Path, tmp_path: Path) -> None:
    gates = Refreshing(_stale_egg_info(candidate))
    code, _ = _run(candidate, tmp_path, gates)
    assert code == 0
    assert len(gates.runs) == 9


def test_an_egg_info_without_pkg_info_counts_as_stale(candidate: Path) -> None:
    (candidate / "src" / "shopify_mcp.egg-info").mkdir(parents=True)
    assert rg.stale_metadata(candidate, 0.0) == ["src/shopify_mcp.egg-info"]


def test_the_freshness_check_allows_whole_second_timestamps(candidate: Path) -> None:
    # A file system that keeps whole seconds can stamp a file written 0.9 s into a gate
    # that started at 5_000.9 as 5_000.0. Literal times, so a change to the slack shows.
    pkg_info = _stale_egg_info(candidate)
    os.utime(pkg_info, (5_000.0, 5_000.0))
    assert rg.stale_metadata(candidate, 5_001.5) == []
    assert rg.stale_metadata(candidate, 5_002.5) == ["src/shopify_mcp.egg-info"]


def test_a_failed_packaging_check_keeps_its_own_exit_code(candidate: Path, tmp_path: Path) -> None:
    pkg_info = _stale_egg_info(candidate)

    class FailingPackaging(Gates):
        def execute(self, argv: list[str], *, cwd: Path, env: dict[str, str], stdout: Any) -> int:
            super().execute(argv, cwd=cwd, env=env, stdout=stdout)
            return 7 if "--dry-run" in argv else 0

    code, evidence = _run(candidate, tmp_path, FailingPackaging())
    assert code == 7, "a failed packaging check keeps its own exit code"
    assert "metadata not refreshed" not in (evidence / "offline-tests-01.log").read_text()
    assert pkg_info.exists()


def test_an_untracked_pyproject_is_named_in_the_refusal(tmp_path: Path) -> None:
    with pytest.raises(rg.Refusal, match=r"^pyproject.toml is not tracked at HEAD$"):
        rg.project_scripts(tmp_path, None)


def test_the_callers_pythonpath_never_reaches_a_gate(
    candidate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/some/other/tree/src")
    gates = Gates()
    assert _run(candidate, tmp_path, gates)[0] == 0
    assert {env["PYTHONPATH"] for _, env, _ in gates.runs} == {str(candidate / "src")}
    assert os.environ["PYTHONPATH"] == "/some/other/tree/src"


# Refusal paths of the Holocron code that neither suite reached. Holocron did not
# measure coverage of scripts/; shopify-mcp does.


def _lint(*steps: Any) -> dict[str, Any]:
    return {"jobs": {"lint": {"steps": list(steps)}}}


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("ruff check .", "workflow step must be a mapping"),
        ({"name": 5, "run": "ruff check ."}, "workflow run step needs a name and command text"),
        ({"name": "Ruff lint", "run": 3}, "workflow run step needs a name and command text"),
    ],
    ids=["not-a-mapping", "name-not-text", "run-not-text"],
)
def test_plan_refuses_a_malformed_step(step: Any, message: str) -> None:
    with pytest.raises(rg.Refusal, match=f"^{message}$"):
        rg.plan(_lint(step))


def test_a_step_added_to_skip_steps_is_classified_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(rg.SKIP_STEPS, "Upload logs", "nothing to upload locally")
    jobs, classified, _ = rg.plan(
        _lint(
            {"name": "Upload logs", "run": "upload"}, {"name": "Ruff lint", "run": "ruff check ."}
        )
    )
    assert jobs == {"lint": [["ruff", "check", "."]]}
    assert classified[0] == {
        "job": "lint",
        "step": "Upload logs",
        "reason": "nothing to upload locally",
        "run": False,
    }


@pytest.mark.parametrize("path", ["/tmp/report.xml", "../report.xml", "."])
def test_a_junitxml_path_outside_the_worktree_is_refused(path: str) -> None:
    step = {
        "name": "Run offline tests under coverage",
        "run": f"coverage run -m pytest --junitxml={path}",
    }
    with pytest.raises(rg.Refusal, match="junitxml path must stay inside the worktree"):
        rg.plan({"jobs": {"offline-tests": {"steps": [step]}}})


def test_quoted_text_is_not_read_as_shell_syntax() -> None:
    assert list(rg.unquoted("a'b*'c\"d?\"e")) == ["a", "c", "e"]
    jobs, _, _ = rg.plan(_lint({"name": "Ruff lint", "run": 'ruff check "src/*.py"'}))
    assert jobs == {"lint": [["ruff", "check", "src/*.py"]]}


def test_a_source_whose_head_type_is_not_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with (
        pytest.raises(rg.Refusal, match="wrong HEAD type"),
        rg.regular_source(tmp_path / "f", "120000"),
    ):
        pass


def test_a_source_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(rg.Refusal, match="is not regular"), rg.regular_source(tmp_path, "100644"):
        pass


def _repo(tmp_path: Path) -> tuple[Path, list[str]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "f.txt")
    return root, [GIT, "-C", str(root)]


def _commit(root: Path) -> str:
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "c")
    return _git(root, "rev-parse", "HEAD")


def test_a_gitlink_in_the_tree_is_refused(tmp_path: Path) -> None:
    root, git = _repo(tmp_path)
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    head = _commit(root)
    with pytest.raises(rg.Refusal, match=r"^tracked path is not a file: sub$"):
        rg.verify_head_content(root, git, head)


def test_a_tracked_file_that_cannot_be_read_counts_as_changed(tmp_path: Path) -> None:
    root, git = _repo(tmp_path)
    head = _commit(root)
    (root / "f.txt").unlink()
    with pytest.raises(rg.Refusal, match=r"^dirty tracked content differs from HEAD: f\.txt$"):
        rg.verify_head_content(root, git, head)


def test_a_wrong_caffeinate_path_is_refused_on_macos(candidate: Path, tmp_path: Path) -> None:
    messages: list[str] = []
    gates = Gates()
    code = rg.main(
        [
            "--root",
            str(candidate),
            "--candidate",
            _git(candidate, "rev-parse", "HEAD"),
            "--evidence",
            str(tmp_path / "evidence"),
            "--git-path",
            GIT,
        ],
        load=lambda _: DOC,
        build=lambda _: "",
        execute=gates.execute,
        which=lambda name: GIT if name == "git" else "/elsewhere/caffeinate",
        platform="darwin",
        emit=messages.append,
    )
    assert code == 1
    assert messages == ["'refused: unexpected caffeinate path'"]
    assert gates.runs == []
