"""Offline tests for the E33-S08 CI venv builder, ``scripts/verify/ci_venvs.py``.

No test builds a real venv or reaches the network: the builder takes its command
runner and its interpreter probe as arguments, and these tests pass fakes. Only the
tests of ``probe_version`` and ``run_command`` start real processes.

Ported from Holocron. Its ``# kills:`` comments name the mutants each test was
measured to kill in Holocron; this repo has no check that keeps them true. What
gates this file here is coverage: ``[tool.coverage.run] source`` includes ``scripts``.
"""

from __future__ import annotations

import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from scripts.verify import ci_venvs as cv

UNIT_RUN = (
    "python -m pip install --upgrade pip\n"
    "# a comment naming pip install fake==1\n"
    'pip install pytest==7.4.4 "pydantic>=2"\n'
)
LINT_RUN = "pip install ruff==0.15.15  # pinned for RUF059"


def _setup(version: object = "3.11") -> dict[str, Any]:
    return {"uses": "actions/setup-python@def", "with": {"python-version": version}}


def _doc() -> dict[str, Any]:
    """The workflow as PyYAML's safe loader hands it back."""
    return {
        "name": "CI",
        True: {"push": {"branches": ["main"]}},
        "jobs": {
            "unit": {
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"uses": "actions/checkout@abc"},
                    {"name": "Set up Python", **_setup()},
                    {"name": "Install", "run": UNIT_RUN},
                    {"name": "Test", "run": "pytest -q"},
                ],
            },
            "lint": {"runs-on": "ubuntu-latest", "steps": [_setup(), {"run": LINT_RUN}]},
            "live": {
                "needs": "unit",
                "steps": [
                    _setup(),
                    {
                        "name": "Install",
                        "run": 'pip install neo4j==6.2.0 "testcontainers[neo4j]==4.14.2"\n',
                    },
                    {"name": "Docker", "run": "docker info"},
                ],
            },
        },
    }


def _changed(edit: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    doc = _doc()
    edit(doc)
    return doc


def _lint_run(run: str) -> dict[str, Any]:
    return _changed(lambda d: d["jobs"]["lint"]["steps"][1].update(run=run))


def _unit_run(run: str) -> dict[str, Any]:
    return _changed(lambda d: d["jobs"]["unit"]["steps"][2].update(run=run))


def _with_docs_job(doc: dict[str, Any]) -> dict[str, Any]:
    doc["jobs"]["docs"] = {"steps": [_setup(), {"run": "echo nothing to install"}]}
    return doc


UNIT = cv.Job(
    "unit",
    "3.11",
    ("python -m pip install --upgrade pip", 'pip install pytest==7.4.4 "pydantic>=2"'),
    (),
)
LINT = cv.Job("lint", "3.11", ("pip install ruff==0.15.15  # pinned for RUF059",), ())
LIVE = cv.Job(
    "live",
    "3.11",
    ('pip install neo4j==6.2.0 "testcontainers[neo4j]==4.14.2"',),
    ("Docker", "Neo4j"),
)


class FakeHost:
    """Records every command and answers version probes without running anything."""

    def __init__(self, versions: dict[str, str | None] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.versions: dict[str, str | None] = {"/fake/python3.11": "3.11.15", **(versions or {})}
        self.fail_on: str | None = None
        self.isolated: list[bool] = []

    def run(self, argv: list[str], *, isolated: bool = True) -> None:
        self.calls.append(list(argv))
        self.isolated.append(isolated)
        if self.fail_on is not None and self.fail_on in argv:
            raise subprocess.CalledProcessError(1, argv)
        if argv[1:4] == ["-I", "-m", "venv"]:
            python = Path(argv[4]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")

    def probe(self, python: str) -> str | None:
        if python in self.versions:
            return self.versions[python]
        return "3.11.15" if Path(python).is_file() else None

    def builds(self) -> list[str]:
        return [Path(c[4]).parent.name for c in self.calls if c[1:4] == ["-I", "-m", "venv"]]


def _waiting_notes(monkeypatch: pytest.MonkeyPatch) -> threading.Semaphore:
    """Count the builder's "waiting for" notes, so a test can wait until it blocks."""
    notes = threading.Semaphore(0)

    def note(*args: object, **kwargs: object) -> None:
        if args and str(args[0]).startswith("waiting for"):
            notes.release()
        print(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(cv, "print", note, raising=False)
    return notes


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    return home


def _main(tmp_path: Path, host: FakeHost, doc: object, *extra: str) -> int:
    path = tmp_path / "ci.yml"
    path.write_text("# the document is handed to main by the test's loader\n")
    return cv.main(
        ["--workflow", str(path), *extra],
        run=host.run,
        probe=host.probe,
        which=lambda name: "/fake/" + name,
        load=lambda _: doc,
    )


def test_each_jobs_python_version_and_install_lines_are_read_from_the_document() -> None:
    # kills: INSTALL_LINE without `python -m `; Docker or Neo4j not recorded; comment
    # lines in a run block read as commands.
    assert cv.jobs_from_document(_doc()) == [UNIT, LINT, LIVE]


def test_the_real_ci_workflow_yields_jobs_that_each_install_something() -> None:
    # kills: Docker or Neo4j not recorded for the live integration job; a loader
    # refusal that the real ci.yml would trip.
    pytest.importorskip("yaml")
    jobs = cv.jobs_from_document(cv.read_document(sys.executable, cv.DEFAULT_WORKFLOW))
    assert [j.name for j in jobs] == ["offline-tests", "lint", "integration"]
    for job in jobs:
        assert job.python_version == "3.11"
        assert any(
            " install " in line and "--upgrade pip" not in line for line in job.install_lines
        )
    assert jobs[2].services == ("Docker", "Neo4j")


def test_a_second_run_rebuilds_only_the_job_whose_install_line_changed(
    tmp_path: Path, home: Path
) -> None:
    # kills: the marker check turned off; a key without the install lines.
    host = FakeHost()
    assert _main(tmp_path, host, _doc()) == 0
    cache = home / ".cache" / "shopify-mcp-ci"
    assert sorted(host.builds()) == sorted(cv.key_dir(cache, j).name for j in (UNIT, LINT, LIVE))
    host.calls.clear()
    assert _main(tmp_path, host, _doc()) == 0
    assert host.calls == []
    assert _main(tmp_path, host, _lint_run("pip install ruff==0.16.0")) == 0
    assert [b.split("-")[0] for b in host.builds()] == ["lint"]


def test_main_prints_one_job_path_line_per_job_under_the_home_cache(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: a cache relative to the working directory; built/reused lines on stdout.
    assert _main(tmp_path, FakeHost(), _doc()) == 0
    cache = home / ".cache" / "shopify-mcp-ci"
    expected = [f"{j.name} {cv.key_dir(cache, j) / 'g1'}" for j in (UNIT, LINT, LIVE)]
    assert capsys.readouterr().out.splitlines() == expected


def test_the_docker_job_says_docker_and_neo4j_are_not_provided(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: the services note dropped; the services note printed for every job.
    assert _main(tmp_path, FakeHost(), _doc()) == 0
    notes = [line for line in capsys.readouterr().err.splitlines() if "not provided" in line]
    assert notes == ["NOTE live: Python packages only; Docker and Neo4j are not provided"]


def test_an_interpreter_on_the_wrong_version_is_refused_and_the_needed_version_named(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: the version check skipped; --python ignored; an interpreter error at exit 0.
    host = FakeHost({"/opt/py312": "3.12.4"})
    assert _main(tmp_path, host, _doc(), "--python", "/opt/py312") == 1
    err = capsys.readouterr().err
    assert "needs Python 3.11" in err
    assert "/opt/py312 is Python 3.12.4" in err
    assert host.calls == []


def test_a_missing_interpreter_fails_the_run(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: a missing interpreter not raised; an interpreter error at exit 0.
    path = tmp_path / "ci.yml"
    path.write_text("")
    host = FakeHost()
    assert (
        cv.main(
            ["--workflow", str(path)],
            run=host.run,
            probe=host.probe,
            which=lambda _: None,
            load=lambda _: _doc(),
        )
        == 1
    )
    assert "python3.11 is not on PATH" in capsys.readouterr().err
    assert host.calls == []


def test_a_job_with_no_pip_install_line_is_reported_not_skipped(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: the no-install job not reported; exit 0 after the report.
    host = FakeHost()
    assert _main(tmp_path, host, _with_docs_job(_doc())) == 1
    out, err = capsys.readouterr()
    assert [line.split()[0] for line in out.splitlines()] == ["unit", "lint", "live"]
    assert "docs: no pip install line in the workflow; no venv built" in err


def test_a_failed_install_fails_the_run_and_names_the_job(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: CalledProcessError escaping main; exit 0 after a failed build.
    host = FakeHost()
    host.fail_on = "ruff==0.15.15"
    assert _main(tmp_path, host, _doc()) == 1
    out, err = capsys.readouterr()
    assert "lint: build failed" in err
    assert [line.split()[0] for line in out.splitlines()] == ["unit", "live"]


def test_rebuild_builds_every_venv_again(tmp_path: Path, home: Path) -> None:
    # kills: --rebuild ignored.
    host = FakeHost()
    assert _main(tmp_path, host, _doc()) == 0
    host.calls.clear()
    assert _main(tmp_path, host, _doc(), "--rebuild") == 0
    assert sorted(host.builds()) == sorted(
        cv.key_dir(home / ".cache" / "shopify-mcp-ci", j).name for j in (UNIT, LINT, LIVE)
    )


def test_main_prunes_only_when_asked_and_reports_what_it_removed(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: --prune ignored; pruning without --prune; the pruned path not reported.
    host = FakeHost()
    changed = _lint_run("pip install ruff==0.16.0")
    old = cv.key_dir(home / ".cache" / "shopify-mcp-ci", LINT) / "g1"
    assert _main(tmp_path, host, _doc()) == 0
    assert _main(tmp_path, host, changed) == 0
    assert old.is_dir()
    capsys.readouterr()
    assert _main(tmp_path, host, changed, "--prune") == 0
    assert not old.exists()
    assert f"lint: pruned {old}" in capsys.readouterr().err


def test_a_failed_prune_keeps_the_jobs_path_line_and_fails_the_run(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: a prune error reported as a failed build; the path line dropped; exit 0.
    def broken(job: cv.Job, cache_dir: Path, keep: Path) -> list[Path]:
        raise OSError("disk says no")

    monkeypatch.setattr(cv, "prune", broken)
    assert _main(tmp_path, FakeHost(), _doc(), "--prune") == 1
    out, err = capsys.readouterr()
    assert [line.split()[0] for line in out.splitlines()] == ["unit", "lint", "live"]
    assert "lint: prune failed: disk says no" in err
    assert "build failed" not in err


def test_an_interpreter_that_does_not_run_is_named_unrunnable(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: the unrunnable branch of the interpreter refusal.
    assert _main(tmp_path, FakeHost({"/opt/broken": None}), _doc(), "--python", "/opt/broken") == 1
    assert "/opt/broken is Python unrunnable" in capsys.readouterr().err


def test_dash_u_is_an_accepted_install_option() -> None:
    # kills: -U missing from INSTALL_OPTIONS.
    jobs = cv.jobs_from_document(_lint_run("pip install -U ruff==0.15.15"))
    assert jobs[1].install_lines == ("pip install -U ruff==0.15.15",)


def test_steps_and_keys_an_install_does_not_depend_on_are_accepted() -> None:
    # kills: PIP_SETTING without a left word boundary; a conditional step refused before
    # it is known to install anything.
    def edit(doc: dict[str, Any]) -> None:
        unit = doc["jobs"]["unit"]
        unit["if"] = "github.ref == 'refs/heads/main'"
        unit["env"] = {"SKIP_PIP_CHECK": "1"}
        unit["steps"][3] = {
            "name": "pip install check",
            "if": "always()",
            "run": 'export SKIP_PIP_CHECK=1\nif [ -n "$CI" ]; then pytest -q; fi\ncat <<EOF\nEOF\n',
        }

    assert cv.jobs_from_document(_changed(edit)) == [UNIT, LINT, LIVE]


def test_a_docker_job_that_installs_no_neo4j_needs_docker_only() -> None:
    # kills: Neo4j added to every Docker job; neo4j looked for in every command.
    def edit(doc: dict[str, Any]) -> None:
        steps = doc["jobs"]["live"]["steps"]
        steps[1]["run"] = "pip install requests==2.0.0"
        steps[2]["run"] = "docker run neo4j:5"

    assert cv.jobs_from_document(_changed(edit))[2].services == ("Docker",)


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        (["not", "a", "mapping"], "the workflow is not a mapping"),
        ({"name": "CI"}, "no jobs"),
        ({"jobs": ["unit"]}, "no jobs"),
        ({"jobs": {}}, "no jobs"),
        ({"jobs": {"bad/name": {"steps": []}}}, "a job name that cannot name a cache directory"),
        (_changed(lambda d: d["jobs"].update(unit="x")), "unit: not a mapping"),
        (
            _changed(lambda d: d["jobs"]["unit"].update(container="python:3.11")),
            "unit: runs in a container",
        ),
        (_changed(lambda d: d["jobs"]["unit"].update(steps="x")), "unit: steps is not a list"),
        (_changed(lambda d: d["jobs"]["unit"]["steps"].append("x")), "unit: step 5: not a mapping"),
        (_lint_run(["pip install ruff==0.15.15"]), "lint: step 2: run is not a string"),  # type: ignore[arg-type]
        (_changed(lambda d: d["jobs"]["unit"]["steps"].pop(1)), "unit: no python-version"),
        (
            _changed(lambda d: d["jobs"]["lint"]["steps"].append(_setup("3.12"))),
            "lint: two python-versions",
        ),
        (
            _changed(lambda d: d["jobs"]["unit"]["steps"][1].update(_setup(3.1))),
            "unit: step 2: python-version must be a quoted version",
        ),
        (
            _changed(
                lambda d: d["jobs"]["unit"]["steps"][1].update(_setup("${{ matrix.python }}"))
            ),
            "unit: step 2: python-version must be a quoted version",
        ),
        (
            _changed(lambda d: d.update(env={"PIP_INDEX_URL": "x"})),
            "workflow: pip setting PIP_INDEX_URL",
        ),
        (
            _changed(lambda d: d["jobs"]["lint"].update(env={"PIP_INDEX_URL": "x"})),
            "lint: pip setting PIP_INDEX_URL",
        ),
        (
            _changed(lambda d: d["jobs"]["lint"]["steps"][1].update(env={"pip_no_cache_dir": "1"})),
            "lint: step 2: pip setting PIP_NO_CACHE_DIR",
        ),
        (_changed(lambda d: d["jobs"]["lint"].update(env="x")), "lint: env is not a mapping"),
        (
            _changed(
                lambda d: d["jobs"]["unit"]["steps"][3].update(run="export PIP_CONSTRAINT=c.txt")
            ),
            "unit: step 4: pip setting PIP_CONSTRAINT",
        ),
        (
            _changed(lambda d: d["jobs"]["lint"]["steps"][1].update({"if": "always()"})),
            "lint: step 2: install line in a conditional step",
        ),
        (
            _unit_run(UNIT_RUN + "echo done\n"),
            "unit: step 3: install step holds a non-install line",
        ),
        (
            _unit_run('[ -n "$SKIP" ] || exit 0\n' + UNIT_RUN),
            "unit: step 3: install step holds a non-install line",
        ),
        (
            _unit_run(UNIT_RUN + "cat > notes.txt <<EOF\nhi\nEOF\n"),
            "unit: step 3: install step holds a non-install line",
        ),
        (
            _unit_run('pip install pytest==7.4.4 \\\n  "pydantic>=2"\n'),
            "unit: step 3: line continuation",
        ),
        (_unit_run('pip install pytest==7.4.4 "pydantic>=2\n'), "unit: step 3: cannot split"),
        # shopify-mcp: `-r FILE` inside the repo is accepted (see test_ci_venvs_shopify_mcp.py).
        (
            _lint_run("pip install -r ../requirements.txt"),
            "lint: step 2: unsupported requirement file",
        ),
        (
            _lint_run("pip install -r /etc/requirements.txt"),
            "lint: step 2: unsupported requirement file",
        ),
        (_lint_run("pip install -r"), "lint: step 2: unsupported requirement file"),
        (_lint_run("pip install -e ."), "lint: step 2: unsupported install argument"),
        (_lint_run("pip install '.[dev]'"), "lint: step 2: unsupported install argument"),
        (
            _lint_run("pip install ruff==0.15.15 && echo done"),
            "lint: step 2: unsupported install argument",
        ),
        (_lint_run("pip install $RUFF"), "lint: step 2: unsupported install argument"),
        (
            _lint_run("pip install ruff-0.15.15-py3-none-any.whl"),
            "lint: step 2: unsupported install argument",
        ),
        (_lint_run("pip install ruff.tar.xz"), "lint: step 2: unsupported install argument"),
        (_lint_run("pip install ruff>=0.15"), "lint: step 2: unsupported install argument"),
        (
            _lint_run('pip install "a\\" ruff[x]==0.15.15"'),
            "lint: step 2: unsupported install argument",
        ),
        (_lint_run("pip3 install ruff==0.15.15"), "lint: step 2: unrecognized pip command"),
        (
            _lint_run("python3 -m pip install ruff==0.15.15"),
            "lint: step 2: unrecognized pip command",
        ),
        (_lint_run("CI=1 pip install ruff==0.15.15"), "lint: step 2: unrecognized pip command"),
        (_lint_run("pip install ruff==0.15.15#;echo"), "lint: step 2: shell syntax"),
        (_lint_run("pip install ruff[x]==0.15.15"), "lint: step 2: shell syntax"),
        (_lint_run("pip install ruff==0.15.*"), "lint: step 2: shell syntax"),
        (_lint_run("pip install ruff==0.15.1?"), "lint: step 2: shell syntax"),
        (_lint_run('pip install "ruff==0.15.15"#;echo'), "lint: step 2: shell syntax"),
        (_lint_run("pip install 'a\\' ruff[x]==0.15.15"), "lint: step 2: shell syntax"),
    ],
    ids=[
        "not-a-mapping",
        "no-jobs",
        "jobs-not-a-mapping",
        "empty-jobs",
        "bad-job-name",
        "job-not-a-mapping",
        "container-job",
        "steps-not-a-list",
        "step-not-a-mapping",
        "run-not-a-string",
        "no-version",
        "two-versions",
        "float-version",
        "expression-version",
        "workflow-pip-env",
        "job-pip-env",
        "lowercase-step-pip-env",
        "env-not-a-mapping",
        "run-pip-env",
        "conditional-step",
        "trailing-non-install-line",
        "exit-before-install",
        "heredoc-after-install",
        "continuation",
        "unsplittable",
        "requirements-file-outside-repo",
        "requirements-file-absolute",
        "requirements-file-missing",
        "editable",
        "local-path",
        "shell-operator",
        "variable",
        "archive",
        "tar-xz",
        "unquoted-operator",
        "escaped-double-quote",
        "pip3",
        "python3-m-pip",
        "env-prefix",
        "mid-word-hash",
        "unquoted-glob",
        "unquoted-star",
        "unquoted-question-mark",
        "hash-after-quote",
        "single-quoted-backslash",
    ],
)
def test_a_workflow_the_builder_cannot_reproduce_is_refused(
    doc: object, message: str, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: each case, its own WorkflowError refusal turned off.
    with pytest.raises(cv.WorkflowError, match=re.escape(message)):
        cv.jobs_from_document(doc)
    host = FakeHost()
    assert _main(tmp_path, host, doc) == 2
    assert message in capsys.readouterr().err
    assert host.calls == []


def test_a_refusal_names_the_step_and_its_problem_exactly() -> None:
    # kills: a refusal with the wrong step number or without one.
    with pytest.raises(cv.WorkflowError) as refused:
        cv.jobs_from_document(_lint_run("pip install ruff==0.15.*"))
    assert str(refused.value) == "lint: step 2: shell syntax '*'"


@pytest.mark.parametrize(
    "doc",
    [
        _lint_run('curl -H "token hunter2" https://host | pip install ruff==0.15.15'),
        _unit_run("pip install pytest==7.4.4 hunter2 \\\n  more\n"),
        _lint_run("pip install hunter2*"),
        _lint_run('pip install "hunter2'),
        _lint_run("pip install hunter2.tar.gz"),
        _lint_run("pip install ruff==1.0 && echo hunter2"),
        _unit_run(UNIT_RUN + "echo hunter2\n"),
        _changed(lambda d: d["jobs"]["lint"].update(env={"PIP_INDEX_URL": "https://u:hunter2@h"})),
        _changed(lambda d: d["jobs"].update({"hunter2/x": {}})),
        _changed(lambda d: d["jobs"]["unit"]["steps"][3].update(run="export PIP_X=hunter2")),
    ],
    ids=[
        "unrecognized-pip",
        "continuation",
        "shell-syntax",
        "cannot-split",
        "unsupported-argument",
        "operator",
        "non-install-line",
        "pip-env-value",
        "job-name",
        "run-pip-setting",
    ],
)
def test_no_refusal_echoes_workflow_text(doc: object) -> None:
    # kills: any refusal message that quotes a run line, an argument, an env value or a
    # job name it refused.
    with pytest.raises(cv.WorkflowError) as refused:
        cv.jobs_from_document(doc)
    assert "hunter2" not in str(refused.value)


def test_read_document_loads_yaml_with_the_safe_loader(tmp_path: Path) -> None:
    # kills: the loaded document not handed back.
    pytest.importorskip("yaml")
    path = tmp_path / "ci.yml"
    path.write_text(
        "jobs:\n  lint:\n    steps:\n      - {uses: s, with: {python-version: '3.11'}}\n"
        "      - run: pip install ruff==0.15.15\n"
    )
    assert cv.read_document(sys.executable, path) == {
        "jobs": {
            "lint": {
                "steps": [
                    {"uses": "s", "with": {"python-version": "3.11"}},
                    {"run": "pip install ruff==0.15.15"},
                ]
            }
        }
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("jobs: [", "not valid YAML"),
        ("!!python/object/apply:builtins.len [[1, 2]]", "not valid YAML"),
        ("when: 2026-10-05\n", "a value JSON cannot carry"),
        ("a: &x [1]\nb: *x\n", "a YAML alias is refused"),
    ],
    ids=["invalid-yaml", "python-tag", "a-date", "an-alias"],
)
def test_read_document_refuses_what_safe_load_and_json_cannot_carry(
    text: str, message: str, tmp_path: Path
) -> None:
    # kills: a loader that accepts Python tags; an alias that json.dumps would expand;
    # a refusal that does not say which of these it was.
    pytest.importorskip("yaml")
    path = tmp_path / "ci.yml"
    path.write_text(text)
    with pytest.raises(cv.WorkflowError, match=re.escape(message)):
        cv.read_document(sys.executable, path)


def test_read_document_ignores_a_planted_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: the loader run without -I, so PYTHONPATH can replace yaml.
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "yaml.py").write_text(
        "AliasEvent = type('AliasEvent', (), {})\nSafeLoader = None\nYAMLError = Exception\n"
        "def parse(text, loader):\n    return []\n"
        "def safe_load(stream):\n    return {'jobs': 'planted'}\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(planted))
    path = tmp_path / "ci.yml"
    path.write_text("jobs: {}\n")
    try:
        document = cv.read_document(sys.executable, path)
    except (cv.WorkflowError, cv.ParserError):
        document = None
    assert document != {"jobs": "planted"}


def _stand_in(tmp_path: Path, script: str) -> str:
    python = tmp_path / "stand-in-python"
    python.write_text("#!/bin/sh\n" + script)
    python.chmod(0o755)
    return str(python)


@pytest.mark.parametrize(
    ("script", "error", "hangs"),
    [
        ("exit 4\n", cv.ParserError, False),
        ("echo '{}'\nexit 7\n", cv.ParserError, False),
        (None, cv.ParserError, False),
        ("echo not json\n", cv.ParserError, False),
        ("exit 2\n", cv.WorkflowError, False),
        ("sleep 3\necho '{}'\n", cv.WorkflowError, True),
    ],
    ids=["no-yaml-module", "a-crash", "no-interpreter", "not-json", "invalid-yaml", "hangs"],
)
def test_read_document_tells_a_broken_parser_from_an_unloadable_workflow(
    script: str | None,
    error: type[Exception],
    hangs: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # kills: a broken parser reported as a bad workflow; a crash, a missing interpreter,
    # bad JSON or a hang escaping read_document; an exit status read only through stdout.
    if hangs:

        def time_out(*args: object, **kwargs: object) -> NoReturn:
            assert kwargs["timeout"] == cv.LOAD_TIMEOUT
            raise subprocess.TimeoutExpired(cmd="stand-in-python", timeout=cv.LOAD_TIMEOUT)

        monkeypatch.setattr(subprocess, "run", time_out)
    path = tmp_path / "ci.yml"
    path.write_text("jobs: {}\n")
    python = str(tmp_path / "no-such-python") if script is None else _stand_in(tmp_path, script)
    with pytest.raises(error):
        cv.read_document(python, path)


@pytest.mark.parametrize("planted", [None, ""], ids=["no-yaml", "yaml-without-its-api"])
def test_a_missing_or_incomplete_yaml_is_a_broken_parser(
    planted: str | None, tmp_path: Path
) -> None:
    # kills: the loader's missing-yaml exit, or its missing-API exit, read as an
    # unloadable workflow.
    path = tmp_path / "ci.yml"
    path.write_text("jobs: {}\n")
    site = tmp_path / "planted"
    site.mkdir()
    if planted is not None:
        (site / "yaml.py").write_text(planted)
    runner = (
        "import sys; sys.path.insert(0, sys.argv[1]); code = sys.argv[2];"
        " sys.argv = ['-c', sys.argv[3]]; exec(code)"
    )
    python = _stand_in(tmp_path, f'exec {sys.executable} -S -c "{runner}" {site} "$3" "$4"\n')
    with pytest.raises(cv.ParserError, match="cannot import yaml"):
        cv.read_document(python, path)


def test_read_document_ignores_a_yaml_module_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: the loader run without -I, so the working directory can shadow yaml.
    (tmp_path / "yaml.py").write_text(
        "AliasEvent = type('AliasEvent', (), {})\nSafeLoader = None\nYAMLError = Exception\n"
        "def parse(text, loader):\n    return []\n"
        "def safe_load(stream):\n    return {'jobs': 'planted'}\n"
    )
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "ci.yml"
    path.write_text("jobs: {}\n")
    try:
        document = cv.read_document(sys.executable, path)
    except (cv.WorkflowError, cv.ParserError):
        document = None
    assert document != {"jobs": "planted"}


def test_main_loads_the_workflow_through_its_own_yaml_venv(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: a loader that skips the parser venv; the parser venv built without its
    # pinned PyYAML, or from another interpreter.
    seen: list[tuple[str, Path]] = []

    def reading(python: str, path: Path) -> object:
        seen.append((python, path))
        return _doc()

    monkeypatch.setattr(cv, "read_document", reading)
    path = tmp_path / "ci.yml"
    path.write_text("")
    host = FakeHost()
    parser = cv.parser_job()
    assert parser.install_lines == (
        "pip install --only-binary=:all: --require-hashes -r "
        + shlex.quote(str(cv.PARSER_REQUIREMENTS)),
    )
    assert "pyyaml==6.0.3" in cv.PARSER_REQUIREMENTS.read_text()
    assert parser.python_version == ".".join(map(str, sys.version_info[:2]))
    host.versions[
        str(cv.key_dir(home / ".cache" / "shopify-mcp-ci", parser) / "g1" / "bin" / "python")
    ] = ".".join(map(str, sys.version_info[:3]))
    assert (
        cv.main(
            ["--workflow", str(path)], run=host.run, probe=host.probe, which=lambda n: "/fake/" + n
        )
        == 0
    )
    venv = cv.key_dir(home / ".cache" / "shopify-mcp-ci", parser) / "g1"
    assert host.calls[0] == [sys.executable, "-I", "-m", "venv", str(venv)]
    assert seen == [(str(venv / "bin" / "python"), path)]


def _parser_ready(host: FakeHost, home: Path) -> Path:
    base = cv.key_dir(home / ".cache" / "shopify-mcp-ci", cv.parser_job())
    for generation in ("g1", "g2", "g3"):
        python = base / generation / "bin" / "python"
        host.versions[str(python)] = ".".join(map(str, sys.version_info[:3]))
    return base / "g1"


def test_a_parser_venv_that_cannot_be_built_fails_the_run(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: a parser build failure escaping main, or exit 0 after it.
    path = tmp_path / "ci.yml"
    path.write_text("")
    host = FakeHost()
    _parser_ready(host, home)
    host.fail_on = "--require-hashes"
    assert cv.main(["--workflow", str(path)], run=host.run, probe=host.probe) == 1
    assert "cannot build the YAML parser" in capsys.readouterr().err


def test_prune_also_removes_the_parser_venvs_old_generations(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: parser generations left behind by --prune.
    monkeypatch.setattr(cv, "read_document", lambda python, path: _doc())
    path = tmp_path / "ci.yml"
    path.write_text("")
    host = FakeHost()
    old = _parser_ready(host, home)
    args = ["--workflow", str(path)]

    def which(name: str) -> str:
        return "/fake/" + name

    assert cv.main(args, run=host.run, probe=host.probe, which=which) == 0
    assert cv.main([*args, "--rebuild"], run=host.run, probe=host.probe, which=which) == 0
    assert old.is_dir()
    assert cv.main([*args, "--prune"], run=host.run, probe=host.probe, which=which) == 0
    assert not old.exists()
    assert f"{cv.PARSER_NAME}: pruned {old}" in capsys.readouterr().err


def test_a_broken_parser_venv_fails_the_run_and_points_at_rebuild(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: a broken parser reported as a bad workflow at exit 2, or escaping main; the
    # advice not naming the parser venv to remove.
    def broken(python: str, path: Path) -> object:
        raise cv.ParserError("yaml is not importable")

    monkeypatch.setattr(cv, "read_document", broken)
    path = tmp_path / "ci.yml"
    path.write_text("")
    host = FakeHost()
    _parser_ready(host, home)
    assert cv.main(["--workflow", str(path)], run=host.run, probe=host.probe) == 1
    venv = cv.key_dir(home / ".cache" / "shopify-mcp-ci", cv.parser_job()) / "g1"
    assert f"the YAML parser venv is broken; remove {venv.parent} and run again" in (
        capsys.readouterr().err
    )


def test_rebuild_builds_the_parser_venv_again_too(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: --rebuild not passed to the parser venv's build.
    monkeypatch.setattr(cv, "read_document", lambda python, path: _doc())
    path = tmp_path / "ci.yml"
    path.write_text("")
    host = FakeHost()
    _parser_ready(host, home)
    args = ["--workflow", str(path)]
    assert cv.main(args, run=host.run, probe=host.probe, which=lambda n: "/fake/" + n) == 0
    assert (
        cv.main([*args, "--rebuild"], run=host.run, probe=host.probe, which=lambda n: "/fake/" + n)
        == 0
    )
    assert sum(b.startswith("yaml.parser-") for b in host.builds()) == 2


def test_install_lines_run_through_the_venvs_own_pip_without_a_shell(tmp_path: Path) -> None:
    # kills: `shlex.split(..., comments=False)`; the base interpreter's pip instead of the
    # venv's; dropping too few tokens from a `python -m pip` line; venv or pip run
    # without -I, so the working directory can shadow them.
    host = FakeHost()
    path, built = cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    assert built
    venv_python = str(path / "bin" / "python")
    assert host.calls == [
        ["/fake/python3.11", "-I", "-m", "venv", str(path)],
        [venv_python, "-I", "-m", "pip", "install", "ruff==0.15.15"],
    ]
    host.calls.clear()
    cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    unit_python = str(cv.key_dir(tmp_path, UNIT) / "g1" / "bin" / "python")
    assert host.calls[1:] == [
        [unit_python, "-I", "-m", "pip", "install", "--upgrade", "pip"],
        [unit_python, "-I", "-m", "pip", "install", "pytest==7.4.4", "pydantic>=2"],
    ]


def test_an_unchanged_job_reuses_its_venv(tmp_path: Path) -> None:
    # kills: the marker check turned off, so every run rebuilds.
    host = FakeHost()
    first = cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    host.calls.clear()
    second = cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    assert first == (second[0], True)
    assert second[1] is False
    assert host.calls == []


def test_a_changed_install_line_yields_a_new_path_and_other_jobs_keep_theirs() -> None:
    # kills: a key of the job name only; a key without the version; a key without the
    # install lines.
    changed = cv.Job("unit", "3.11", ("pip install pytest==8.0.0",), ())
    other_version = cv.Job("unit", "3.12", UNIT.install_lines, ())
    cache = Path("/cache")
    paths = {cv.key_dir(cache, j) for j in (UNIT, changed, other_version)}
    assert len(paths) == 3
    assert cv.key_dir(cache, UNIT) == cv.key_dir(
        cache, cv.Job(UNIT.name, UNIT.python_version, UNIT.install_lines, UNIT.services)
    )
    assert cv.key_dir(cache, UNIT).parent == cache
    assert cv.key_dir(cache, UNIT).name.startswith("unit-")


def test_the_version_match_is_on_whole_components() -> None:
    # kills: a `startswith(version)` that accepts 3.110 for 3.11; a major-only match.
    assert cv.version_matches("3.11", "3.11.15")
    assert cv.version_matches("3.11", "3.11")
    assert not cv.version_matches("3.11", "3.110.1")
    assert not cv.version_matches("3.11", "3.1")
    assert not cv.version_matches("3.11", None)


def test_the_interpreter_is_python_major_minor_on_path_unless_overridden() -> None:
    # kills: --python ignored; a lookup of the bare `python3`; a missing interpreter not raised.
    assert (
        cv.find_interpreter("3.11", None, which={"python3.11": "/b/python3.11"}.get)
        == "/b/python3.11"
    )
    assert cv.find_interpreter("3.11.4", None, which={"python3.11": "/b/py"}.get) == "/b/py"
    assert cv.find_interpreter("3.11", "/x/py", which=lambda _: "/b/py") == "/x/py"
    with pytest.raises(cv.InterpreterError, match=r"python3\.11 is not on PATH; pass --python"):
        cv.find_interpreter("3.11", None, which=lambda _: None)


def test_a_failed_install_leaves_no_marker_and_the_next_run_rebuilds_from_scratch(
    tmp_path: Path,
) -> None:
    # kills: the marker written before the installs; a half-built directory kept.
    host = FakeHost()
    host.fail_on = "ruff==0.15.15"
    with pytest.raises(subprocess.CalledProcessError):
        cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    path = cv.key_dir(tmp_path, LINT) / "g1"
    (path / "stale").write_text("left by the failed build")
    host.fail_on = None
    assert cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe) == (
        path.parent / "g2",
        True,
    )
    assert not path.exists()


def test_a_venv_whose_python_no_longer_runs_is_rebuilt_beside_it(tmp_path: Path) -> None:
    # kills: reuse on the marker alone, without probing the venv's own interpreter; the
    # old generation deleted by the rebuild.
    host = FakeHost()
    path, _ = cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    (path / "bin" / "python").unlink()
    host.calls.clear()
    new = cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    assert new == (path.parent / "g2", True)
    assert (path / cv.MARKER).is_file()


def test_a_venv_whose_python_reports_another_version_is_rebuilt(tmp_path: Path) -> None:
    # kills: reuse whenever the venv's interpreter answers at all.
    host = FakeHost()
    path, _ = cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    host.versions[str(path / "bin" / "python")] = "3.12.1"
    host.calls.clear()
    assert cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)[1]
    assert host.builds() == [path.parent.name]


def test_a_rebuild_builds_a_new_generation_and_leaves_the_old_one(tmp_path: Path) -> None:
    # kills: a rebuild that deletes the venv another session may be running from.
    host = FakeHost()
    old, _ = cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    new = cv.ensure_venv(
        LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe, rebuild=True
    )
    assert new == (old.parent / "g2", True)
    assert (old / cv.MARKER).is_file()


def test_prune_removes_the_jobs_superseded_venvs_and_nothing_else(tmp_path: Path) -> None:
    # kills: prune keeping old generations; prune removing the kept venv; prune matching
    # another job's or a longer job name's directories; an emptied key dir kept.
    host = FakeHost()
    old, _ = cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    keep, _ = cv.ensure_venv(
        LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe, rebuild=True
    )
    other, _ = cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    stale = tmp_path / ("lint-" + "0" * 16)
    (stale / "g1").mkdir(parents=True)
    lookalike = tmp_path / ("lint-tests-" + "0" * 16)
    (lookalike / "g1").mkdir(parents=True)
    assert sorted(cv.prune(LINT, tmp_path, keep)) == sorted([old, stale / "g1"])
    assert keep.is_dir()
    assert other.is_dir()
    assert (lookalike / "g1").is_dir()
    assert not stale.exists()
    assert not Path(f"{stale}.lock").exists()
    assert Path(f"{keep.parent}.lock").exists()


def test_prune_skips_a_venv_whose_lock_another_build_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: prune waiting on another build's lock; prune ignoring it.
    stale = tmp_path / ("lint-" + "0" * 16)
    (stale / "g1").mkdir(parents=True)
    fd = os.open(f"{stale}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    removed: list[list[Path]] = []
    worker = threading.Thread(
        target=lambda: removed.append(cv.prune(LINT, tmp_path, tmp_path / "keep")), daemon=True
    )
    try:
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert removed == [[]]
    assert (stale / "g1").is_dir()
    assert f"skipped {stale}: another build holds its lock" in capsys.readouterr().err


def test_a_build_refuses_a_symlinked_key_dir(tmp_path: Path) -> None:
    # kills: ensure_venv following a symlinked key dir out of the cache.
    outside = tmp_path / "outside"
    (outside / "g1").mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    cv.key_dir(cache, LINT).symlink_to(outside)
    host = FakeHost()
    with pytest.raises(OSError, match="symlink"):
        cv.ensure_venv(LINT, cache, "/fake/python3.11", run=host.run, probe=host.probe)
    assert (outside / "g1").is_dir()
    assert host.calls == []


def test_a_build_that_waited_on_an_unlinked_lock_file_takes_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: a lock held on an inode no other session can see any more.
    notes = _waiting_notes(monkeypatch)
    host = FakeHost()
    lock = Path(f"{cv.key_dir(tmp_path, LINT)}.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    worker = threading.Thread(
        target=cv.ensure_venv,
        args=(LINT, tmp_path, "/fake/python3.11"),
        kwargs={"run": host.run, "probe": host.probe},
        daemon=True,
    )
    try:
        worker.start()
        assert notes.acquire(timeout=5)
        lock.unlink()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    worker.join(5)
    assert not worker.is_alive()
    assert host.builds() == [cv.key_dir(tmp_path, LINT).name]
    assert lock.is_file()


def test_a_build_that_waited_on_a_replaced_lock_file_waits_on_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: a lock kept on an inode another session replaced.
    notes = _waiting_notes(monkeypatch)
    host = FakeHost()
    lock = Path(f"{cv.key_dir(tmp_path, LINT)}.lock")
    old: int | None = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(old, fcntl.LOCK_EX)
    new: int | None = None
    worker = threading.Thread(
        target=cv.ensure_venv,
        args=(LINT, tmp_path, "/fake/python3.11"),
        kwargs={"run": host.run, "probe": host.probe},
        daemon=True,
    )
    try:
        worker.start()
        assert notes.acquire(timeout=5)
        lock.unlink()
        new = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(new, fcntl.LOCK_EX)
        fcntl.flock(old, fcntl.LOCK_UN)
        os.close(old)
        old = None
        assert notes.acquire(timeout=5)
        assert host.calls == []
    finally:
        for fd in (old, new):
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
    worker.join(5)
    assert not worker.is_alive()
    assert host.builds() == [cv.key_dir(tmp_path, LINT).name]


def test_prune_retakes_a_lock_file_unlinked_after_its_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: prune keeping a lock on a file another session unlinked.
    stale = tmp_path / ("lint-" + "0" * 16)
    (stale / "g1").mkdir(parents=True)
    real_flock = fcntl.flock
    calls: list[int] = []

    def unlinking(fd: object, operation: int) -> None:
        real_flock(fd, operation)  # type: ignore[arg-type]
        calls.append(operation)
        if len(calls) == 1:
            os.unlink(f"{stale}.lock")

    monkeypatch.setattr(cv.fcntl, "flock", unlinking)
    assert cv.prune(LINT, tmp_path, tmp_path / "keep") == [stale / "g1"]
    assert len(calls) == 2
    assert not stale.exists()


def test_the_venv_is_created_in_the_callers_environment_and_pip_runs_isolated(
    tmp_path: Path,
) -> None:
    # kills: venv creation run in the allowlisted environment the probe never saw; pip
    # run with the caller's environment.
    host = FakeHost()
    cv.ensure_venv(UNIT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    assert host.isolated == [False, True, True]


@pytest.mark.parametrize("version", ["3.12.0", None], ids=["other-version", "unrunnable"])
def test_a_built_venv_on_another_version_is_refused_before_any_install(
    tmp_path: Path, version: str | None
) -> None:
    # kills: no probe of the venv just built; a probe only after the installs.
    built = cv.key_dir(tmp_path, LINT) / "g1"
    host = FakeHost({str(built / "bin" / "python"): version})
    with pytest.raises(OSError, match=re.escape(f"reports Python {version or 'unrunnable'}")):
        cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    assert host.builds() == [built.parent.name]
    assert len(host.calls) == 1
    assert not (built / cv.MARKER).exists()


def test_a_lock_handle_is_closed_when_flock_fails_unexpectedly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: a handle left open when flock raises something other than BlockingIOError.
    handles: list[object] = []

    def recording_open(*args: object, **kwargs: object) -> object:
        handle = open(*args, **kwargs)  # type: ignore[call-overload]  # noqa: SIM115
        handles.append(handle)
        return handle

    def failing_flock(fd: object, operation: int) -> None:
        raise OSError(37, "No locks available")

    monkeypatch.setattr(cv, "open", recording_open, raising=False)
    monkeypatch.setattr(cv.fcntl, "flock", failing_flock)
    host = FakeHost()
    with pytest.raises(OSError, match="No locks available"):
        cv.ensure_venv(LINT, tmp_path, "/fake/python3.11", run=host.run, probe=host.probe)
    assert len(handles) == 1
    assert handles[0].closed  # type: ignore[attr-defined]


def test_prune_rechecks_for_a_symlink_once_it_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: the symlink check made only before the lock is taken.
    outside = tmp_path / "outside"
    (outside / "g1").mkdir(parents=True)
    cache = tmp_path / "cache"
    stale = cache / ("lint-" + "0" * 16)
    (stale / "g1").mkdir(parents=True)
    real_flock = fcntl.flock

    def swapping(fd: object, operation: int) -> None:
        real_flock(fd, operation)  # type: ignore[arg-type]
        if not stale.is_symlink():
            shutil.rmtree(stale)
            stale.symlink_to(outside)

    monkeypatch.setattr(cv.fcntl, "flock", swapping)
    assert cv.prune(LINT, cache, cache / "keep") == []
    assert (outside / "g1").is_dir()


def test_prune_never_follows_a_symlinked_key_dir(tmp_path: Path) -> None:
    # kills: prune treating a symlink to a directory as a key dir.
    outside = tmp_path / "outside"
    (outside / "g1").mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ("lint-" + "0" * 16)).symlink_to(outside)
    assert cv.prune(LINT, cache, cache / "keep") == []
    assert (outside / "g1").is_dir()


def test_prune_never_follows_a_symlinked_generation(tmp_path: Path) -> None:
    # kills: prune running rmtree on a generation that is a symlink.
    outside = tmp_path / "outside"
    outside.mkdir()
    stale = tmp_path / "cache" / ("lint-" + "0" * 16)
    stale.mkdir(parents=True)
    (stale / "g1").symlink_to(outside)
    assert cv.prune(LINT, tmp_path / "cache", tmp_path / "keep") == []
    assert outside.is_dir()


def test_a_symlinked_generation_stops_the_build(tmp_path: Path) -> None:
    # kills: a build that deletes or builds into a symlinked generation.
    outside = tmp_path / "outside"
    outside.mkdir()
    base = cv.key_dir(tmp_path / "cache", LINT)
    base.mkdir(parents=True)
    (base / "g1").symlink_to(outside)
    host = FakeHost()
    with pytest.raises(OSError, match="symlink"):
        cv.ensure_venv(LINT, tmp_path / "cache", "/fake/python3.11", run=host.run, probe=host.probe)
    assert outside.is_dir()
    assert host.calls == []


def test_prune_skips_a_key_dir_another_prune_removed_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: prune reading a key dir that vanished.
    stale = tmp_path / ("lint-" + "0" * 16)
    (stale / "g1").mkdir(parents=True)
    real_flock = fcntl.flock

    def racing(fd: object, operation: int) -> None:
        shutil.rmtree(stale, ignore_errors=True)
        real_flock(fd, operation)  # type: ignore[arg-type]

    monkeypatch.setattr(cv.fcntl, "flock", racing)
    assert cv.prune(LINT, tmp_path, tmp_path / "keep") == []
    assert not Path(f"{stale}.lock").exists()


def test_help_shows_the_whole_docstring_and_the_prune_hazard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # kills: a --help that shows only the first docstring line; a --prune help without
    # its warning.
    with pytest.raises(SystemExit):
        cv.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "A per-key ``flock`` serializes sessions that build the same venv." in text
    assert "including any that another session is still running from" in text


def test_a_build_waits_for_the_jobs_lock_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: the flock removed; the waiting note dropped.
    notes = _waiting_notes(monkeypatch)
    host = FakeHost()
    lock = Path(f"{cv.key_dir(tmp_path, LINT)}.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    worker = threading.Thread(
        target=cv.ensure_venv,
        args=(LINT, tmp_path, "/fake/python3.11"),
        kwargs={"run": host.run, "probe": host.probe},
        daemon=True,
    )
    try:
        worker.start()
        assert notes.acquire(timeout=5)
        worker.join(0.3)
        assert worker.is_alive()
        assert host.calls == []
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    worker.join(5)
    assert not worker.is_alive()
    assert host.builds() == [cv.key_dir(tmp_path, LINT).name]
    assert f"waiting for {lock}" in capsys.readouterr().err


def test_an_unreadable_workflow_exits_2(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: an OSError from reading the workflow escaping main.
    assert cv.main(["--workflow", str(tmp_path / "missing.yml")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_a_workflow_that_is_not_utf8_exits_2(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kills: a UnicodeDecodeError from reading the workflow escaping main.
    path = tmp_path / "ci.yml"
    path.write_bytes(b"jobs:\n  \xff:\n")
    assert cv.main(["--workflow", str(path)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_the_default_probe_reports_a_real_interpreters_full_version(tmp_path: Path) -> None:
    # kills: a probe that prints major.minor only; one that ignores a non-zero exit; one
    # that returns '' when the interpreter cannot start; one that accepts any output.
    assert cv.probe_version(sys.executable) == ".".join(map(str, sys.version_info[:3]))
    failing = tmp_path / "python"
    failing.write_text("#!/bin/sh\necho 3.11.15\nexit 1\n")
    failing.chmod(0o755)
    assert cv.probe_version(str(failing)) is None
    noisy = tmp_path / "noisy"
    noisy.write_text("#!/bin/sh\nprintf '\\033[2J3.11.15\\n'\n")
    noisy.chmod(0o755)
    assert cv.probe_version(str(noisy)) is None
    assert cv.probe_version(str(Path(sys.executable).parent / "no-such-python")) is None
    assert cv.probe_version(os.devnull) is None


def test_the_default_probe_ignores_a_planted_sitecustomize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: the probe run without -I, so a sitecustomize on PYTHONPATH runs first.
    (tmp_path / "sitecustomize.py").write_text("print('9.9.9')\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert cv.probe_version(sys.executable) == ".".join(map(str, sys.version_info[:3]))


def test_the_default_probe_gives_up_on_an_interpreter_that_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: no timeout on the probe; a timeout that escapes probe_version.
    monkeypatch.setattr(cv, "PROBE_TIMEOUT", 0.5)
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nexec sleep 5\n")
    slow.chmod(0o755)
    start = time.monotonic()
    assert cv.probe_version(str(slow)) is None
    assert time.monotonic() - start < 4


def test_the_default_runner_gives_pip_only_an_allowlisted_environment(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: the caller's PIP_* or other variables passed to pip; the caller's pip.conf
    # read; PATH dropped.
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.invalid/simple")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    monkeypatch.setenv("HOME", "/home/ci")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/etc/curl.pem")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy:1080")
    monkeypatch.setenv("all_proxy", "socks5://proxy:1081")
    probe = (
        "import os; print(os.environ.get('PIP_INDEX_URL'), os.environ.get('AWS_SECRET_ACCESS_KEY'),"
        " os.environ.get('PIP_CONFIG_FILE'), os.environ.get('HOME'),"
        " os.environ.get('REQUESTS_CA_BUNDLE'), os.environ.get('CURL_CA_BUNDLE'),"
        " os.environ.get('ALL_PROXY'), os.environ.get('all_proxy'), os.environ.get('PATH') == "
        + repr(os.environ["PATH"])
        + ")"
    )
    cv.run_command([sys.executable, "-c", probe])
    assert capfd.readouterr().err.strip() == (
        f"None None {os.devnull} /home/ci /etc/ca.pem /etc/curl.pem"
        " socks5://proxy:1080 socks5://proxy:1081 True"
    )
    cv.run_command([sys.executable, "-c", probe], isolated=False)
    assert capfd.readouterr().err.strip().startswith("https://mirror.invalid/simple hunter2")


def test_the_default_runner_raises_on_a_failing_command() -> None:
    # kills: `check=False` in run_command.
    cv.run_command([sys.executable, "-c", "pass"])
    with pytest.raises(subprocess.CalledProcessError):
        cv.run_command([sys.executable, "-c", "raise SystemExit(3)"])


def test_the_default_runner_sends_the_commands_output_to_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # kills: inheriting stdout, which mixes pip's output into the `<job> <path>` lines.
    cv.run_command([sys.executable, "-c", "print('from pip')"])
    out, err = capfd.readouterr()
    assert out == ""
    assert "from pip" in err
