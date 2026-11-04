"""Tests for the shopify-mcp changes to ``scripts/verify/ci_venvs.py``.

``test_ci_venvs.py`` is the Holocron suite. This file covers what the port added: the
candidate's editable install is never run into a shared venv, ``-r`` files are read
from the repo root and keyed by content, ``pip-audit`` is not a pip command, and a
shell-only job gets no venv. No test builds a real venv: pip and venv calls are faked.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.verify import ci_venvs as cv

DEV_LOCK = "pip install --require-hashes -r requirements-dev.lock"
AUDIT_LOCK = "pip install --require-hashes -r requirements-audit.lock"


def _setup() -> dict[str, Any]:
    return {"uses": "actions/setup-python", "with": {"python-version": "3.11"}}


# The shape of .github/workflows/test.yml, cut to one step of each kind.
DOC: dict[str, Any] = {
    "jobs": {
        "offline-tests": {
            "steps": [
                _setup(),
                {"name": "Install pinned dependencies from lockfile", "run": DEV_LOCK},
                {"name": "Install package", "run": "pip install --no-deps -e ."},
                {"name": "Run offline tests under coverage", "run": "coverage run -m pytest -v"},
            ]
        },
        "commit-messages": {
            "steps": [
                {
                    "name": "Reject",
                    "run": "set -eu\nfor sha in $(git rev-list x); do\n  echo\ndone\n",
                }
            ]
        },
        "dependency-audit": {
            "steps": [
                _setup(),
                {"name": "Install pinned pip-audit", "run": AUDIT_LOCK},
                {"name": "pip-audit", "run": "pip-audit -r requirements.lock --strict"},
            ]
        },
    }
}


class FakeHost:
    """Records commands; makes ``venv`` create a bin/python; answers every probe 3.11."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, isolated: bool = True) -> None:
        self.calls.append(list(argv))
        if argv[1:4] == ["-I", "-m", "venv"]:
            python = Path(argv[4]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")

    def probe(self, python: str) -> str | None:
        return "3.11.15"

    def pip_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[2:4] == ["-m", "pip"]]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "test.yml").write_text("# handed to main by the test\n")
    (root / "requirements-dev.lock").write_text("ruff==0.15.15\n")
    (root / "requirements-audit.lock").write_text("pip-audit==2.9.0\n")
    return root


def _main(root: Path, host: FakeHost, doc: object = DOC, workflow: Path | None = None) -> int:
    workflow = workflow or root / ".github" / "workflows" / "test.yml"
    return cv.main(
        ["--workflow", str(workflow), "--root", str(root)],
        run=host.run,
        probe=host.probe,
        which=lambda name: "/fake/" + name,
        load=lambda _: doc,
    )


def _job(name: str, doc: object = DOC) -> cv.Job:
    return next(job for job in cv.jobs_from_document(doc) if job.name == name)


def test_the_candidate_install_is_accepted_and_never_run(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _job("offline-tests").install_lines == (DEV_LOCK,)
    host = FakeHost()
    assert _main(root, host) == 0
    assert "offline-tests " in capsys.readouterr().out
    assert host.pip_calls(), "the lockfile install must have run"
    assert not [call for call in host.pip_calls() if "-e" in call]


def test_the_candidate_line_must_be_exact() -> None:
    doc = {"jobs": {"offline-tests": {"steps": [_setup(), {"run": "pip install -e ."}]}}}
    with pytest.raises(cv.WorkflowError, match="unsupported install argument"):
        cv.jobs_from_document(doc)


def test_a_shell_only_job_gets_no_venv(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _job("commit-messages") == cv.Job("commit-messages", "", (), ())
    assert _main(root, FakeHost()) == 0
    out, err = capsys.readouterr()
    assert "commit-messages: no python-version and no install line; no venv built" in err
    assert sorted(line.split()[0] for line in out.splitlines()) == [
        "dependency-audit",
        "offline-tests",
    ]


def test_an_install_line_without_a_python_version_is_still_refused() -> None:
    doc = {"jobs": {"lint": {"steps": [{"run": DEV_LOCK}]}}}
    with pytest.raises(cv.WorkflowError, match="lint: no python-version"):
        cv.jobs_from_document(doc)


@pytest.mark.parametrize("line", ["pip-audit -r requirements.lock", "pip-compile x.in"])
def test_a_pip_named_tool_is_not_a_pip_command(line: str) -> None:
    doc = {"jobs": {"lint": {"steps": [_setup(), {"run": DEV_LOCK}, {"run": line}]}}}
    assert _job("lint", doc).install_lines == (DEV_LOCK,)


def test_pip_reads_a_copy_of_each_requirement_file_inside_its_venv(root: Path) -> None:
    host = FakeHost()
    assert _main(root, host) == 0
    for call in host.pip_calls():
        given = Path(call[call.index("-r") + 1])
        venv = Path(call[0]).parents[1]
        assert given.is_relative_to(venv)
        assert given.read_bytes() == (root / given.name).read_bytes()
    assert len(host.pip_calls()) == 2


def test_a_change_after_the_check_never_reaches_pip(tmp_path: Path, root: Path) -> None:
    original = (root / "requirements-dev.lock").read_bytes()

    class Racing(FakeHost):
        def run(self, argv: list[str], *, isolated: bool = True) -> None:
            super().run(argv, isolated=isolated)
            if argv[2:4] == ["-m", "pip"]:
                (root / "requirements-dev.lock").write_text("ruff==0.0.1\n")
                self.read = Path(argv[argv.index("-r") + 1]).read_bytes()

    host = Racing()
    job = cv.pin_requirement_files(_job("offline-tests"), root)
    cv.ensure_venv(
        job, tmp_path / "cache", "/fake/python3.11", run=host.run, probe=host.probe, root=root
    )
    assert host.read == original


def test_the_root_comes_from_root_not_from_the_workflow_path(
    tmp_path: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flat = tmp_path / "flat.yml"
    flat.write_text("# handed to main by the test\n")
    host = FakeHost()
    assert _main(root, host, workflow=flat) == 0
    assert len(host.pip_calls()) == 2
    assert "offline-tests " in capsys.readouterr().out


def test_the_default_root_is_the_repo_holding_the_script() -> None:
    repo = Path(__file__).resolve().parents[3]
    assert repo == cv.DEFAULT_ROOT
    assert cv.DEFAULT_WORKFLOW == cv.DEFAULT_ROOT / ".github" / "workflows" / "test.yml"


def test_an_unpinned_job_is_refused_before_pip(tmp_path: Path, root: Path) -> None:
    host = FakeHost()
    with pytest.raises(OSError, match="requirement files are not pinned"):
        cv.ensure_venv(
            _job("offline-tests"),
            tmp_path / "cache",
            "/fake/python3.11",
            run=host.run,
            probe=host.probe,
            root=root,
        )
    assert host.pip_calls() == []


def _two_jobs(second_lock: str) -> dict[str, Any]:
    def job(lock: str) -> dict[str, Any]:
        return {"steps": [_setup(), {"run": f"pip install --require-hashes -r {lock}"}]}

    return {"jobs": {"offline-tests": job("requirements-dev.lock"), "lint": job(second_lock)}}


def _venv_builds(host: FakeHost) -> list[list[str]]:
    return [call for call in host.calls if call[1:4] == ["-I", "-m", "venv"]]


def test_jobs_that_need_the_same_packages_share_one_venv(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost()
    assert _main(root, host, _two_jobs("requirements-dev.lock")) == 0
    assert len(_venv_builds(host)) == 1
    paths = dict(line.split() for line in capsys.readouterr().out.splitlines())
    assert paths["offline-tests"] == paths["lint"]
    assert "offline-tests-" in paths["lint"], "the first job's venv is the one shared"


def test_jobs_that_need_different_packages_get_their_own_venvs(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost()
    assert _main(root, host, _two_jobs("requirements-audit.lock")) == 0
    assert len(_venv_builds(host)) == 2
    paths = dict(line.split() for line in capsys.readouterr().out.splitlines())
    assert paths["offline-tests"] != paths["lint"]


def test_the_content_key_ignores_the_job_name() -> None:
    job = _job("offline-tests")
    assert cv.content_key(job) == cv.content_key(dataclasses.replace(job, name="lint"))


def test_the_key_follows_the_requirement_file_content(tmp_path: Path, root: Path) -> None:
    job = _job("offline-tests")
    cache = tmp_path / "cache"
    before = cv.key_dir(cache, cv.pin_requirement_files(job, root))
    (root / "requirements-dev.lock").write_text("ruff==0.15.16\n")
    after = cv.key_dir(cache, cv.pin_requirement_files(job, root))
    assert before != after


def test_two_trees_with_the_same_lockfile_share_one_key(tmp_path: Path, root: Path) -> None:
    other = tmp_path / "other-worktree"
    other.mkdir()
    (other / "requirements-dev.lock").write_bytes((root / "requirements-dev.lock").read_bytes())
    job = _job("offline-tests")
    cache = tmp_path / "cache"
    assert cv.key_dir(cache, cv.pin_requirement_files(job, root)) == cv.key_dir(
        cache, cv.pin_requirement_files(job, other)
    )


def test_a_requirement_file_that_changes_during_the_build_is_refused(
    tmp_path: Path, root: Path
) -> None:
    job = cv.pin_requirement_files(_job("offline-tests"), root)
    (root / "requirements-dev.lock").write_text("ruff==0.0.1\n")
    host = FakeHost()
    with pytest.raises(OSError, match="a requirement file changed during the build"):
        cv.ensure_venv(
            job, tmp_path / "cache", "/fake/python3.11", run=host.run, probe=host.probe, root=root
        )
    assert host.pip_calls() == []


def test_a_requirement_file_removed_during_the_build_is_refused(tmp_path: Path, root: Path) -> None:
    job = cv.pin_requirement_files(_job("offline-tests"), root)
    (root / "requirements-dev.lock").unlink()
    host = FakeHost()
    with pytest.raises(OSError, match="requirement file is not a regular file"):
        cv.ensure_venv(
            job, tmp_path / "cache", "/fake/python3.11", run=host.run, probe=host.probe, root=root
        )
    assert host.pip_calls() == []


def test_a_missing_requirement_file_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "requirements-dev.lock").unlink()
    host = FakeHost()
    assert _main(root, host) == 2
    assert (
        "requirement file is not a regular file inside the repo: requirements-dev.lock"
        in capsys.readouterr().err
    )
    assert host.calls == []


def test_a_symlinked_requirement_file_is_refused(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.lock"
    target.write_text("ruff==0.15.15\n")
    (root / "requirements-dev.lock").unlink()
    (root / "requirements-dev.lock").symlink_to(target)
    with pytest.raises(cv.WorkflowError, match="not a regular file"):
        cv.pin_requirement_files(_job("offline-tests"), root)


def test_a_requirement_file_under_a_symlinked_directory_is_refused(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "dev.lock").write_text("ruff==0.15.15\n")
    (root / "locks").symlink_to(outside)
    doc = {"jobs": {"lint": {"steps": [_setup(), {"run": "pip install -r locks/dev.lock"}]}}}
    with pytest.raises(
        cv.WorkflowError, match=re.escape("not a regular file inside the repo: locks/dev.lock")
    ):
        cv.pin_requirement_files(_job("lint", doc), root)


def test_a_requirement_file_in_a_real_subdirectory_is_accepted(root: Path) -> None:
    (root / "locks").mkdir()
    (root / "locks" / "dev.lock").write_text("ruff==0.15.15\n")
    doc = {"jobs": {"lint": {"steps": [_setup(), {"run": "pip install -r locks/dev.lock"}]}}}
    pinned = cv.pin_requirement_files(_job("lint", doc), root)
    assert [name for name, _ in pinned.requirement_files] == ["locks/dev.lock"]
