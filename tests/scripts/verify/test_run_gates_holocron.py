"""Gate runner tests use real temp git repos and fake gate processes.

Ported from Holocron's tests/e33/test_e33_s29_run_gates.py and adapted to shopify-mcp:
test.yml, no Docker, and a tracked pyproject.toml (run_gates reads [project.scripts]).
Its ``# kills:`` comments are Holocron's measurements; nothing here keeps them true.
"""

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GIT = "/usr/bin/git"


def module():
    assert (ROOT / "scripts/verify/run_gates.py").is_file(), "gate runner is missing"
    return importlib.import_module("scripts.verify.run_gates")


def git(root, *args):
    return subprocess.check_output([GIT, "-C", str(root), *args]).decode().strip()


@pytest.fixture
def lane(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    workflow = root / ".github/workflows/test.yml"
    workflow.parent.mkdir(parents=True)
    document = {
        "jobs": {
            "offline-tests": {
                "steps": [
                    {
                        "name": "Run offline tests under coverage",
                        "run": "coverage run -m pytest --junitxml=report.xml\ncoverage report --fail-under=100",
                    },
                    {"name": "Mypy", "run": "mypy"},
                ]
            }
        }
    }
    workflow.write_text(json.dumps(document))
    (root / "source.txt").write_text("original\n")
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root, git(root, "rev-parse", "HEAD"), tmp_path / "evidence", document


def invoke(
    lane,
    *,
    root=None,
    candidate=None,
    which=None,
    execute=None,
    platform="linux",
    builder=None,
    loader=None,
):
    repo, head, evidence, document = lane
    calls = []
    messages = []
    envs = "offline-tests /fake/offline\n"

    def fake_execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0 if execute is None else execute(argv, **kwargs)

    result = module().main(
        [
            "--root",
            str(root or repo),
            "--candidate",
            head if candidate is None else candidate,
            "--evidence",
            str(evidence),
            "--git-path",
            GIT,
        ],
        load=loader or (lambda _: document),
        build=builder or (lambda _: envs),
        execute=fake_execute,
        which=which
        or (
            lambda name: {
                "git": GIT,
                "caffeinate": "/usr/bin/caffeinate",
            }.get(name)
        ),
        platform=platform,
        emit=messages.append,
    )
    return result, calls, "\n".join(messages)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("root", "worktree root"),
        ("dirty", "dirty"),
        ("candidate", "candidate"),
        ("git", "git"),
        ("evidence", "evidence folder already exists"),
        ("short", "40 hex"),
        ("unknown", "unknown"),
        ("shell", "shell syntax"),
    ],
)
def test_refusals_start_no_job_or_evidence(lane, fault, reason):
    # kills: scripts/verify/run_gates.py:396 `if Path(top).resolve() != root:` -> `if False and Path(top).resolve() != root:`
    # kills: scripts/verify/run_gates.py:388 `if which(tool) != wanted:` -> `if False and which(tool) != wanted:`
    # kills: scripts/verify/run_gates.py:399 `if head != args.candidate.lower():` -> `if False and head != args.candidate.lower():`
    # kills: scripts/verify/run_gates.py:401 `if os.path.lexists(args.evidence):` -> `if False and os.path.lexists(args.evidence):`
    # kills: scripts/verify/run_gates.py:551 `return 1` -> `return 0`
    root, head, evidence, document = lane
    kwargs = {}
    if fault == "root":
        child = root / "child"
        child.mkdir()
        kwargs["root"] = child
    elif fault == "dirty":
        (root / "source.txt").write_text("changed")
    elif fault == "candidate":
        kwargs["candidate"] = "f" * 40
    elif fault == "git":
        kwargs["which"] = lambda name: "/bad/tool" if name == fault else {"git": GIT}.get(name)
    elif fault == "evidence":
        evidence.mkdir()
        (evidence / "keep").write_text("keep")
    elif fault == "short":
        kwargs["candidate"] = head[:7]
    else:
        step = document["jobs"]["offline-tests"]["steps"][0]
        if fault == "unknown":
            step["name"] = "New gate"
        else:
            step["run"] += " && echo unsafe"
    result, calls, message = invoke(lane, **kwargs)
    assert result != 0
    assert reason in message
    assert calls == []
    if fault == "evidence":
        assert sorted(path.name for path in evidence.iterdir()) == ["keep"]
    else:
        assert not evidence.exists()


def test_command_exit_three_stops_job_and_writes_evidence(lane):
    # kills: scripts/verify/run_gates.py:503 `if code:` -> `if False and code:`
    result, calls, message = invoke(lane, execute=lambda *args, **kwargs: 3)
    evidence = lane[2]
    assert result == 3, message
    assert len(calls) == 1
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["jobs"]["offline-tests"]["commands"][0]["exit"] == 3
    assert (evidence / "offline-tests-01.exit").read_text() == "3\n"
    assert summary["exit"] == 3


def test_times_and_darwin_sleep_guard_are_recorded(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:463 `time.get_clock_info("monotonic").implementation` -> `"disabled"`
    # kills: scripts/verify/run_gates.py:484 `if platform == "darwin":` -> `if platform == "never":`
    # kills: scripts/verify/run_gates.py:499 `time.time() - wall,` -> `0,`
    from types import SimpleNamespace

    wall = iter(range(100, 1000, 10))
    monotonic = iter(range(200, 1000, 2))
    monkeypatch.setattr(
        module(),
        "time",
        SimpleNamespace(
            time=lambda: next(wall),
            monotonic=lambda: next(monotonic),
            get_clock_info=lambda name: SimpleNamespace(implementation="fake monotonic clock"),
        ),
        raising=False,
    )
    result, calls, message = invoke(lane, platform="darwin")
    assert result == 0, message
    summary = json.loads((lane[2] / "summary.json").read_text())
    assert summary.get("monotonic_clock") == "fake monotonic clock"
    job = summary["jobs"]["offline-tests"]
    assert job["wall_seconds"] == 70
    assert job["monotonic_seconds"] == 14
    assert len(job["commands"]) == 3
    for command in job["commands"]:
        assert command["wall_seconds"] == 10
        assert command["monotonic_seconds"] == 2
    assert all(argv[:2] == ["/usr/bin/caffeinate", "-i"] for argv, _ in calls)


@pytest.mark.parametrize(
    "change,reason", [("head", "HEAD moved"), ("source", "source.txt"), ("deleted", "source.txt")]
)
def test_postrun_detects_candidate_or_content_changes_even_on_failure(lane, change, reason):
    # kills: scripts/verify/run_gates.py:514 `            != head` -> `            != head and False`
    # kills: scripts/verify/run_gates.py:526 `if changed:` -> `if False and changed:`
    root = lane[0]

    def mutate(*args, **kwargs):
        if change == "head":
            git(root, "commit", "--allow-empty", "-qm", "move")
        elif change == "source":
            (root / "source.txt").write_text("mutant\n")
        else:
            (root / "source.txt").unlink()
        return 3

    result, calls, message = invoke(lane, execute=mutate)
    assert result == 1, message
    assert reason in message
    assert len(calls) == 1
    assert (lane[2] / "summary.json").is_file()
    assert json.loads((lane[2] / "summary.json").read_text())["exit"] == 1


def test_builder_paths_reach_commands(lane):
    result, calls, message = invoke(lane, builder=lambda _: "offline-tests /chosen/venv\n")
    assert result == 0, message
    assert calls[0][0][0] == "/chosen/venv/bin/coverage"
    assert calls[-1][0][0] == "/chosen/venv/bin/mypy"
    assert calls[0][1]["env"]["PATH"].startswith("/chosen/venv/bin" + __import__("os").pathsep)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "offline-tests relative\n",
        "other /fake/other\n",
        "offline-tests /one\noffline-tests /two\n",
    ],
)
def test_builder_malformed_paths_start_no_gate_or_evidence(lane, output):
    # kills: scripts/verify/run_gates.py:445 `not Path(fields[1]).is_absolute()` -> `False`
    try:
        result, calls, message = invoke(lane, builder=lambda _: output)
    except KeyError:
        assert False, "runner must refuse missing venvs before gates"
    assert result != 0
    assert "venv" in message
    assert calls == []
    assert not lane[2].exists()


def test_workflow_read_is_capped_before_jobs(lane):
    # kills: scripts/verify/run_gates.py:73 `if len(data) > cap:` -> `if len(data) > cap + 1024:`
    (lane[0] / ".github/workflows/test.yml").write_bytes(b" " * (1024 * 1024 + 1))
    git(lane[0], "add", ".github/workflows/test.yml")
    git(lane[0], "commit", "-qm", "large workflow fixture")
    candidate = git(lane[0], "rev-parse", "HEAD")
    result, calls, message = invoke(lane, candidate=candidate)
    assert result != 0
    assert "WORKFLOW_CAP" in message
    assert calls == []
    assert not lane[2].exists()


def test_captured_process_output_is_capped(tmp_path):
    # kills: scripts/verify/run_gates.py:73 `if len(data) > cap:` -> `if len(data) > cap + 1024:`
    capture = getattr(module(), "capture", None)
    assert callable(capture), "bounded subprocess capture is missing"
    with pytest.raises(module().Refusal, match="TEST_CAP"):
        capture([__import__("sys").executable, "-c", "print('x' * 33)"], cap=32, label="TEST_CAP")
    assert (
        capture([__import__("sys").executable, "-c", "print('ok')"], cap=32, label="TEST_CAP")
        == b"ok\n"
    )


def test_evidence_inside_worktree_is_refused(lane):
    # kills: scripts/verify/run_gates.py:404 `if evidence.is_relative_to(root):` -> `if False and evidence.is_relative_to(root):`
    root, head, _, document = lane
    inside = root / "evidence"
    result, calls, message = invoke((root, head, inside, document))
    assert result != 0
    assert "outside" in message
    assert calls == []
    assert not inside.exists()


def test_untracked_ci_report_is_copied_and_second_run_is_allowed(lane):
    # kills: scripts/verify/run_gates.py:537 `output.write(block)` -> `None`
    root, head, evidence, document = lane
    (root / "report.xml").write_text("<testsuite/>\n")
    result, _, message = invoke(lane)
    assert result == 0, message
    assert (evidence / "reports/report.xml").is_file(), "CI report was not copied"
    assert (evidence / "reports/report.xml").read_bytes() == b"<testsuite/>\n", (
        "CI report bytes were not copied"
    )
    again = (root, head, evidence.with_name("second"), document)
    assert invoke(again)[0] == 0
    (root / "extra.xml").write_text("not CI output")
    last = (root, head, evidence.with_name("third"), document)
    result, calls, message = invoke(last)
    assert result != 0 and "dirty" in message
    assert calls == []
    assert not last[2].exists()


def test_tracked_ci_report_change_is_never_excused(lane):
    # kills: scripts/verify/run_gates.py:551 `return 1` -> `return 0`
    root, _, evidence, document = lane
    (root / "report.xml").write_text("first")
    git(root, "add", "report.xml")
    git(root, "commit", "-qm", "tracked report")
    head = git(root, "rev-parse", "HEAD")
    (root / "report.xml").write_text("changed")
    result, calls, message = invoke((root, head, evidence, document))
    assert result != 0 and "dirty" in message
    assert calls == []
    assert not evidence.exists()


@pytest.mark.parametrize(
    "line",
    [
        "ruff check .; echo unsafe",
        "ruff check *",
        "ruff check $(pwd)",
        "ruff check . > output",
        "ruff check `pwd`",
        "ruff check . &",
    ],
)
def test_shell_operators_and_expansions_are_refused(lane, line):
    # kills: scripts/verify/run_gates.py:212 `char in "*?[~{}"` -> `char in ""`
    lane[3]["jobs"]["offline-tests"]["steps"][0]["run"] = line
    result, calls, message = invoke(lane)
    assert result != 0 and "shell syntax" in message
    assert calls == []
    assert not lane[2].exists()


def _fake_parser(path, output, code=0):
    """An executable that stands in for the parser venv's python (yaml is not installed here)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncat > /dev/null\nprintf '%s' '{output}'\nexit {code}\n")
    path.chmod(0o755)
    return str(path)


def test_loader_hands_the_workflow_to_the_parser_and_reads_its_json(tmp_path):
    workflow = tmp_path / "test.yml"
    workflow.write_text("jobs: {}\n")
    parser = _fake_parser(tmp_path / "parser", '{"jobs": {"lint": {}}}')
    assert module().load_workflow(workflow, parser_python=parser) == {"jobs": {"lint": {}}}


def test_loader_refuses_when_the_parser_refuses(tmp_path):
    workflow = tmp_path / "test.yml"
    workflow.write_text("jobs: {}\n")
    parser = _fake_parser(tmp_path / "parser", "", code=3)
    with pytest.raises(module().Refusal, match="PARSER_CAP: command exited 3"):
        module().load_workflow(workflow, parser_python=parser)


def test_default_loader_builds_the_parser_venv_from_the_candidate_builder(tmp_path):
    root = tmp_path / "repo"
    workflow = root / ".github/workflows/test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs: {}\n")
    venv = tmp_path / "parser-venv"
    _fake_parser(venv / "bin/python", '{"jobs": {"from": "default"}}')
    builder = root / "scripts/verify/ci_venvs.py"
    builder.parent.mkdir(parents=True)
    builder.write_text(
        "from pathlib import Path\n"
        "def parser_job():\n    return 'parser'\n"
        "def ensure_venv(job, cache_dir, python):\n"
        f"    assert job == 'parser'\n    return Path({str(venv)!r}), False\n"
    )
    assert module().load_workflow(workflow) == {"jobs": {"from": "default"}}


def test_safe_loader_reads_yaml_and_refuses_aliases(tmp_path):
    # kills: scripts/verify/run_gates.py:125 `sys.exit(3)` -> `pass`
    pytest.importorskip("yaml")
    loader = getattr(module(), "load_workflow", None)
    assert callable(loader), "candidate YAML loader is missing"
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  lint:\n    steps:\n      - name: Ruff lint\n        run: ruff check .\n"
    )
    assert (
        loader(workflow, parser_python=__import__("sys").executable)["jobs"]["lint"]["steps"][0][
            "run"
        ]
        == "ruff check ."
    )
    workflow.write_text("jobs: &jobs {}\ncopy: *jobs\n")
    with pytest.raises(module().Refusal, match="PARSER"):
        loader(workflow, parser_python=__import__("sys").executable)


def test_builder_uses_candidate_script_and_workflow(lane):
    # kills: scripts/verify/run_gates.py:142 `str(root / "scripts/verify/ci_venvs.py")` -> `str(root / "scripts/verify/wrong.py")`
    builder = getattr(module(), "build_venvs", None)
    assert callable(builder), "candidate venv builder is missing"
    seen = []

    def capture(argv, **kwargs):
        seen.append((argv, kwargs))
        return b"offline-tests /from/builder\n"

    assert builder(lane[0], capture_fn=capture) == "offline-tests /from/builder\n"
    command, limits = seen[0]
    assert command[1:] == [
        str(lane[0] / "scripts/verify/ci_venvs.py"),
        "--workflow",
        str(lane[0] / ".github/workflows/test.yml"),
        "--root",
        str(lane[0]),
        "--python",
        command[0],
    ]
    assert limits["label"] == "BUILDER_CAP"


def test_default_executor_streams_stdout_and_stderr(tmp_path):
    # kills: scripts/verify/run_gates.py:162 `).returncode` -> `).returncode * 0`
    execute = getattr(module(), "execute_command", None)
    assert callable(execute), "streaming command executor is missing"
    log = tmp_path / "command.log"
    with log.open("wb") as output:
        code = execute(
            [
                __import__("sys").executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
            ],
            cwd=tmp_path,
            env={},
            stdout=output,
        )
    assert code == 3
    assert set(log.read_text().splitlines()) == {"out", "err"}


def test_every_test_yml_step_has_explicit_classification():
    # Needs yaml to read the real workflow; skipped where yaml is not installed.
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    jobs, classified, reports = module().plan(document)
    install = "Install pinned dependencies from lockfile"
    package = "Install package (deps already pinned above)"
    expected = [
        ("offline-tests", install, False),
        ("offline-tests", package, True),
        ("offline-tests", "Run offline tests under coverage", True),
        ("offline-tests", "Enforce 100% coverage", True),
        ("lint", install, False),
        ("lint", package, True),
        ("lint", "Dependency drift check", True),
        ("lint", "Ruff lint", True),
        ("lint", "Ruff format check", True),
        ("lint", "Mypy", True),
        ("commit-messages", None, False),
        ("dependency-audit", "Install pinned pip-audit", False),
        ("dependency-audit", "pip-audit against requirements.lock", True),
        ("dependency-audit", "pip-audit against requirements-dev.lock", True),
        ("dependency-audit", "pip-audit against requirements-audit.lock", True),
    ]
    assert [(step["job"], step["step"], step["run"]) for step in classified] == expected
    assert all(step["reason"] for step in classified if not step["run"])
    assert {job: len(commands) for job, commands in jobs.items()} == {
        "offline-tests": 3,
        "lint": 5,
        "dependency-audit": 3,
    }
    assert reports == set()


def umask_step_document(name, run):
    return {"jobs": {"offline-tests": {"steps": [{"name": name, "run": run}]}}}


def test_an_unknown_step_name_is_still_refused():
    document = umask_step_document(
        "Run mode-reading tests under umask 027", "python scripts/verify/umask_sweep.py --ci-leg"
    )
    with pytest.raises(module().Refusal) as refused:
        module().plan(document)
    assert str(refused.value) == (
        "unknown CI run step: offline-tests/Run mode-reading tests under umask 027"
    )


def test_unknown_install_named_step_is_refused(lane):
    # kills: scripts/verify/run_gates.py:191 `if name in INSTALL_STEPS:` -> `if name.startswith("Install "):`
    lane[3]["jobs"]["offline-tests"]["steps"][0] = {
        "name": "Install new dependencies",
        "run": "echo ignored",
    }
    result, calls, message = invoke(lane)
    assert result != 0 and "unknown" in message
    assert calls == []
    assert not lane[2].exists()


@pytest.mark.parametrize("change", ["head", "source"])
def test_builder_cannot_move_candidate_before_gate_start(lane, change):
    # kills: scripts/verify/run_gates.py:452 `!= head or any(` -> `!= head and False or any(`
    def build(root):
        if change == "head":
            git(root, "commit", "--allow-empty", "-qm", "move")
        else:
            (root / "source.txt").write_text("changed by builder")
        return "offline-tests /fake/offline\n"

    result, calls, message = invoke(lane, builder=build)
    assert result != 0, "moving candidate must be refused before gates"
    assert "preflight" in message
    assert calls == []
    assert not lane[2].exists()


def test_process_launch_failure_still_writes_exit_and_integrity_evidence(lane):
    # kills: scripts/verify/run_gates.py:493 `code = 127` -> `code = 0`
    def fail(*args, **kwargs):
        (lane[0] / "source.txt").write_text("changed before launch failure")
        raise OSError("cannot launch")

    result, calls, message = invoke(lane, execute=fail)
    assert result != 0
    assert "source.txt" in message
    assert len(calls) == 1
    assert (lane[2] / "summary.json").is_file(), "launch failure lost summary"
    summary = json.loads((lane[2] / "summary.json").read_text())
    assert summary["jobs"]["offline-tests"]["commands"][0]["exit"] == 127
    assert (lane[2] / "offline-tests-01.exit").read_text() == "127\n"


def test_dirty_tracked_builder_is_refused_before_loading(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:551 `return 1` -> `return 0`
    (lane[0] / "source.txt").write_text("dirty")

    def unsafe(_):
        assert False, "dirty tracked tree must be refused before loading candidate code"

    result = module().main(
        [
            "--root",
            str(lane[0]),
            "--candidate",
            lane[1],
            "--evidence",
            str(lane[2]),
            "--git-path",
            GIT,
        ],
        load=unsafe,
        which=lambda name: {"git": GIT}.get(name),
        emit=lambda _: None,
        platform="linux",
    )
    assert result != 0
    assert not lane[2].exists()


@pytest.mark.parametrize(
    "document",
    [
        None,
        {"jobs": []},
        {"jobs": {"../bad": {"steps": []}}},
        {"jobs": {"offline-tests": {"steps": None}}},
    ],
)
def test_malformed_workflow_is_refused(lane, document):
    # kills: scripts/verify/run_gates.py:170 `not isinstance(document, dict)` -> `False`
    try:
        result = module().main(
            [
                "--root",
                str(lane[0]),
                "--candidate",
                lane[1],
                "--evidence",
                str(lane[2]),
                "--git-path",
                GIT,
            ],
            load=lambda _: document,
            which=lambda name: {"git": GIT}.get(name),
            emit=lambda _: None,
            platform="linux",
        )
    except (TypeError, AttributeError, KeyError):
        assert False, "malformed workflow must get a refusal, not a traceback"
    assert result != 0
    assert not lane[2].exists()


@pytest.mark.parametrize("flag", ["skip-worktree", "assume-unchanged", "stat-cache"])
@pytest.mark.parametrize(
    "path", ["source.txt", ".github/workflows/test.yml", "scripts/verify/ci_venvs.py"]
)
def test_hidden_tracked_bytes_are_refused_before_candidate_load(lane, flag, path):
    # kills: scripts/verify/run_gates.py:347 `git_blob_hash(path, mode) != blob_oid` -> `False and git_blob_hash(path, mode) != blob_oid`
    root, _, evidence, document = lane
    builder = root / "scripts/verify/ci_venvs.py"
    builder.parent.mkdir(parents=True)
    builder.write_text("pass\n")
    git(root, "add", "scripts/verify/ci_venvs.py")
    git(root, "commit", "-qm", "builder fixture")
    head = git(root, "rev-parse", "HEAD")
    target = root / path
    saved = target.stat()
    os.utime(target, ns=(saved.st_atime_ns, saved.st_mtime_ns - 10_000_000_000))
    git(root, "config", "core.trustctime", "false")
    git(root, "config", "core.checkStat", "minimal")
    git(root, "update-index", "--refresh")
    if flag != "stat-cache":
        git(root, "update-index", "--" + flag, path)
    saved = target.stat()
    original = target.read_bytes()
    target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    os.utime(target, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    assert git(root, "status", "--porcelain=v1") == "", "fixture must hide changed bytes"
    loaded = []
    result, calls, message = invoke(
        (root, head, evidence, document), loader=lambda value: loaded.append(value) or document
    )
    assert result != 0, "HEAD blob mismatch must be refused"
    assert "dirty" in message and path in message
    assert loaded == [], "candidate loader must not see changed tracked files"
    assert calls == []
    assert not evidence.exists()


@pytest.mark.parametrize("flag", ["skip-worktree", "assume-unchanged"])
def test_clean_tracked_index_flags_do_not_block_gates(lane, flag):
    # kills: scripts/verify/run_gates.py:353 `if changed:` -> `if not changed:`
    git(lane[0], "update-index", "--" + flag, "source.txt")
    result, calls, message = invoke(lane)
    assert result == 0, message
    assert len(calls) == 3


@pytest.mark.parametrize("changed", [False, True])
def test_tracked_symlink_bytes_are_checked_against_head(lane, changed):
    # kills: scripts/verify/run_gates.py:347 `git_blob_hash(path, mode) != blob_oid` -> `False and git_blob_hash(path, mode) != blob_oid`
    root, _, evidence, document = lane
    link = root / "link"
    link.symlink_to("source.txt")
    git(root, "add", "link")
    git(root, "commit", "-qm", "link fixture")
    head = git(root, "rev-parse", "HEAD")
    if changed:
        git(root, "update-index", "--skip-worktree", "link")
        link.unlink()
        link.symlink_to("missing.txt")
    result, calls, message = invoke((root, head, evidence, document))
    if changed:
        assert result != 0 and "dirty" in message and "link" in message
        assert calls == []
        assert not evidence.exists()
    else:
        assert result == 0, message
        assert len(calls) == 3


def test_fifo_tracked_replacement_is_refused_before_all_calls(tmp_path):
    # kills: scripts/verify/run_gates.py:257 `if not stat.S_ISREG(info.st_mode):` -> `if False and not stat.S_ISREG(info.st_mode):`
    import tempfile

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        root, _, evidence, document = lane.__wrapped__(Path(temporary))
        (root / "source.txt").write_bytes(b"")
        git(root, "add", "source.txt")
        git(root, "commit", "-qm", "empty source fixture")
        head = git(root, "rev-parse", "HEAD")
        git(root, "update-index", "--skip-worktree", "source.txt")
        (root / "source.txt").unlink()
        os.mkfifo(root / "source.txt")
        assert git(root, "status", "--porcelain=v1") == ""
        program = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from scripts.verify import run_gates\n"
            "root, head, evidence, document = sys.argv[1:]\n"
            "document = json.loads(document)\n"
            "calls = {'loader': 0, 'builder': 0, 'jobs': 0}\n"
            "messages = []\n"
            "def load(_):\n"
            "    calls['loader'] += 1\n"
            "    return document\n"
            "def build(_):\n"
            "    calls['builder'] += 1\n"
            "    return 'offline-tests /fake/offline\\n'\n"
            "def execute(*args, **kwargs):\n"
            "    calls['jobs'] += 1\n"
            "    return 0\n"
            "result = run_gates.main(['--root', root, '--candidate', head, '--evidence', evidence, '--git-path', '/usr/bin/git'], load=load, build=build, execute=execute, which=lambda name: {'git': '/usr/bin/git', 'docker': '/fake/docker'}.get(name), platform='linux', emit=messages.append)\n"
            "print(json.dumps({'exit': result, 'calls': calls, 'message': '\\n'.join(messages), 'evidence': Path(evidence).exists()}))\n"
        )
        timed_out = False
        try:
            done = subprocess.run(
                [
                    __import__("sys").executable,
                    "-B",
                    "-c",
                    program,
                    str(root),
                    head,
                    str(evidence),
                    json.dumps(document),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        assert not timed_out, "FIFO preflight exceeded 3 seconds"
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        assert result["exit"] != 0, "tracked FIFO must be refused"
        assert "dirty" in result["message"] and "source.txt" in result["message"]
        assert result["calls"] == {"loader": 0, "builder": 0, "jobs": 0}
        assert not result["evidence"]


def test_default_candidate_loader_ignores_matching_timestamp_bytecode(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:114 `exec(compile(source_bytes, spec.origin, "exec"), cv.__dict__)` -> `spec.loader.exec_module(cv)`
    import importlib._bootstrap_external
    import importlib.util

    root, _, evidence, document = lane
    source = root / "scripts/verify/ci_venvs.py"
    source.parent.mkdir(parents=True)
    text = (
        "from pathlib import Path\n"
        "def parser_job(): return None\n"
        "def ensure_venv(*args): return Path('/fake/parser'), None\n"
    )
    source.write_text(text)
    (root / ".gitignore").write_text("__pycache__/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "candidate parser fixture")
    head = git(root, "rev-parse", "HEAD")
    sentinel = evidence.with_name("bytecode-sentinel")
    malicious = (
        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('cache executed')\n" + text
    )
    cached = Path(importlib.util.cache_from_source(str(source)))
    # parents=True: under run_gates, PYTHONPYCACHEPREFIX puts this path in a fresh folder.
    cached.parent.mkdir(parents=True)
    cached.write_bytes(
        importlib._bootstrap_external._code_to_timestamp_pyc(
            compile(malicious, str(source), "exec"),
            int(source.stat().st_mtime),
            source.stat().st_size,
        )
    )
    captured = []
    original = module().capture

    def capture(argv, **kwargs):
        if kwargs["label"] == "PARSER_CAP":
            captured.append(argv)
            return json.dumps(document).encode()
        return original(argv, **kwargs)

    monkeypatch.setattr(module(), "capture", capture)
    result, calls, message = invoke((root, head, evidence, document), loader=module().load_workflow)
    assert not sentinel.exists(), "matching timestamp bytecode executed before gates"
    assert result == 0, message
    assert len(calls) == 3
    assert len(captured) == 1 and captured[0][0] == "/fake/parser/bin/python"


@pytest.mark.parametrize("parent", [False, True])
def test_external_report_link_is_refused_before_jobs_and_evidence(lane, parent):
    # kills: scripts/verify/run_gates.py:433 `for report in reports:` -> `for report in ():`
    root, _, evidence, document = lane
    external = evidence.with_name("external")
    external.mkdir()
    sentinel = external / "report.xml"
    sentinel.write_bytes(b"harmless external sentinel")
    report = "output/report.xml" if parent else "report.xml"
    document["jobs"]["offline-tests"]["steps"][0]["run"] = (
        f"coverage run -m pytest --junitxml={report}"
    )
    if parent:
        (root / "output").symlink_to(external, target_is_directory=True)
    else:
        (root / report).symlink_to(sentinel)
    built = []
    result, calls, message = invoke(
        lane, builder=lambda value: built.append(value) or "offline-tests /fake/offline\n"
    )
    assert result != 0, "external report link was accepted before gates"
    assert "report" in message
    assert calls == [] and built == [], "external report link started jobs or builder"
    assert not evidence.exists(), "external report link created evidence"
    assert sentinel.read_bytes() == b"harmless external sentinel"


@pytest.mark.parametrize("parent", [False, True])
def test_report_link_created_by_failed_gate_is_not_copied(lane, parent):
    # kills: scripts/verify/run_gates.py:531 `with report_source(root, report) as source:` -> `with (root / report).open("rb") as source:`
    root, _, evidence, document = lane
    external = evidence.with_name("external")
    external.mkdir()
    sentinel = external / "report.xml"
    sentinel.write_bytes(b"harmless external sentinel")
    report = "output/report.xml" if parent else "report.xml"
    document["jobs"]["offline-tests"]["steps"][0]["run"] = (
        f"coverage run -m pytest --junitxml={report}"
    )

    def execute(*args, **kwargs):
        if parent:
            (root / "output").symlink_to(external, target_is_directory=True)
        else:
            (root / report).symlink_to(sentinel)
        return 3

    result, calls, message = invoke(lane, execute=execute)
    assert not (evidence / "reports" / report).exists(), (
        "gate-created report link copied external bytes"
    )
    assert result != 0 and len(calls) == 1
    summary = json.loads((evidence / "summary.json").read_text())
    assert any("report" in error for error in summary["integrity_errors"]), (
        "unsafe report lost integrity evidence"
    )
    assert sentinel.read_bytes() == b"harmless external sentinel"


def test_git_replace_tree_is_refused_before_candidate_calls(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:390 `git = [args.git_path, "--no-replace-objects", "-C", str(root)]` -> `git = [args.git_path, "-C", str(root)]`
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    root, original, evidence, document = lane
    (root / "source.txt").write_text("replacement tree\n")
    git(root, "add", "source.txt")
    git(root, "commit", "-qm", "replacement fixture")
    replacement = git(root, "rev-parse", "HEAD")
    git(root, "replace", original, replacement)
    git(root, "update-ref", "HEAD", original)
    assert git(root, "rev-parse", "HEAD") == original
    assert git(root, "status", "--porcelain=v1") == "", "replacement fixture must appear clean"
    loaded, built = [], []
    result, calls, message = invoke(
        (root, original, evidence, document),
        loader=lambda value: loaded.append(value) or document,
        builder=lambda value: built.append(value) or "offline-tests /fake/offline\n",
    )
    assert result != 0, "replacement HEAD tree was accepted for original candidate"
    assert "dirty" in message
    assert loaded == [] and built == [] and calls == []
    assert not evidence.exists()


def test_existing_external_report_hardlink_is_refused_before_gates(lane):
    # kills: scripts/verify/run_gates.py:282 `info.st_nlink != 1` -> `False`
    root, _, evidence, _ = lane
    sentinel = evidence.with_name("external-sentinel")
    sentinel.write_bytes(b"harmless hardlink sentinel")
    os.link(sentinel, root / "report.xml")
    built = []
    result, calls, message = invoke(
        lane, builder=lambda value: built.append(value) or "offline-tests /fake/offline\n"
    )
    assert result != 0, "external report hardlink was accepted before gates"
    assert "report.xml" in message, "hardlink refusal did not name the report"
    assert built == [] and calls == [], "hardlink started builder or gates"
    assert not evidence.exists(), "hardlink refusal created evidence"
    assert sentinel.read_bytes() == b"harmless hardlink sentinel"


def test_failed_gate_report_hardlink_is_not_copied(lane):
    root, _, evidence, _ = lane
    sentinel = evidence.with_name("external-sentinel")
    sentinel.write_bytes(b"harmless hardlink sentinel")

    def execute(*args, **kwargs):
        os.link(sentinel, root / "report.xml")
        return 3

    result, calls, message = invoke(lane, execute=execute)
    assert not (evidence / "reports/report.xml").exists(), (
        "failed gate copied external hardlink bytes"
    )
    assert result != 0 and len(calls) == 1
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["exit"] != 0
    assert summary["jobs"]["offline-tests"]["commands"][0]["exit"] == 3
    assert any(
        "unsafe report" in error and "report.xml" in error for error in summary["integrity_errors"]
    ), "hardlink lost named integrity evidence"
    assert "report.xml" in message
    assert sentinel.read_bytes() == b"harmless hardlink sentinel"


def test_successful_gate_user_execute_mode_change_is_refused(lane):
    # kills: scripts/verify/run_gates.py:259 `stat.S_IXUSR` -> `0o111`
    root, _, evidence, document = lane
    source = root / "source.txt"
    source.chmod(0o755)
    git(root, "add", "source.txt")
    git(root, "commit", "-qm", "executable source fixture")
    head = git(root, "rev-parse", "HEAD")
    original = source.read_bytes()
    assert git(root, "ls-tree", "HEAD", "source.txt").startswith("100755 ")
    assert source.stat().st_mode & 0o777 == 0o755

    def execute(*args, **kwargs):
        source.chmod(0o654)
        return 0

    result, calls, message = invoke((root, head, evidence, document), execute=execute)
    assert source.read_bytes() == original, "mode fixture changed source bytes"
    assert git(root, "rev-parse", "HEAD") == head, "mode fixture moved HEAD"
    assert source.stat().st_mode & 0o777 == 0o654
    assert result == 1, "successful gate user-execute change returned a false PASS"
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["exit"] == 1, "user-execute change summary returned a false PASS"
    assert any("source.txt" in error for error in summary["integrity_errors"]), (
        "user-execute change lost named integrity evidence"
    )
    assert len(calls) == 3
    assert all(command["exit"] == 0 for command in summary["jobs"]["offline-tests"]["commands"])
    assert "source.txt" in message


@pytest.mark.parametrize("function", ["content_hash", "git_blob_hash"])
def test_tracked_hash_size_cap_refuses_before_read(tmp_path, monkeypatch, function):
    # kills: scripts/verify/run_gates.py:326 `require_size(info.st_size, TRACKED_CAP, label)` -> `None`
    # kills: scripts/verify/run_gates.py:309 `require_size(info.st_size, TRACKED_CAP, label)` -> `None`
    from contextlib import contextmanager

    target = tmp_path / "large.txt"
    target.write_bytes(b"123456789")
    monkeypatch.setattr(module(), "TRACKED_CAP", 8, raising=False)
    opened = module().regular_source
    reads = []

    class Reader:
        def read(self, count):
            reads.append(count)
            return source.read(count)

    @contextmanager
    def checked(path, mode):
        with opened(path, mode) as (source_value, info):
            nonlocal source
            source = source_value
            yield Reader(), info

    source = None
    monkeypatch.setattr(module(), "regular_source", checked)
    with pytest.raises(module().Refusal, match=r"TRACKED_CAP.*large\.txt"):
        getattr(module(), function)(target, "100644")
    assert reads == [], "oversized tracked descriptor was read before refusal"


@pytest.mark.parametrize("function", ["content_hash", "git_blob_hash"])
def test_growing_tracked_hash_stream_is_capped(tmp_path, monkeypatch, function):
    # kills: scripts/verify/run_gates.py:87 `require_size(total, cap, label)` -> `None`
    from contextlib import contextmanager

    target = tmp_path / "growing.txt"
    target.write_bytes(b"1234")
    monkeypatch.setattr(module(), "TRACKED_CAP", 8, raising=False)
    opened = module().regular_source
    consumed = []

    class Reader:
        def __init__(self, source):
            self.source = source
            self.grew = False

        def read(self, count):
            data = self.source.read(count)
            consumed.append(len(data))
            if not self.grew:
                self.grew = True
                with target.open("ab") as output:
                    output.write(b"abcdefgh")
            return data

    @contextmanager
    def checked(path, mode):
        with opened(path, mode) as (source, info):
            yield Reader(source), info

    monkeypatch.setattr(module(), "regular_source", checked)
    with pytest.raises(module().Refusal, match=r"TRACKED_CAP.*growing\.txt"):
        getattr(module(), function)(target, "100644")
    assert sum(consumed) <= 9, "tracked stream read beyond the cap sentinel byte"


@pytest.mark.parametrize("function", ["content_hash", "git_blob_hash"])
def test_tracked_symlink_hash_is_capped(tmp_path, monkeypatch, function):
    # kills: scripts/verify/run_gates.py:321 `require_size(len(data), TRACKED_CAP, label)` -> `None`
    # kills: scripts/verify/run_gates.py:306 `require_size(len(data), TRACKED_CAP, label)` -> `None`
    # kills: scripts/verify/run_gates.py:79 `if size > cap:` -> `if False and size > cap:`
    target = tmp_path / "long-link"
    target.symlink_to("123456789")
    monkeypatch.setattr(module(), "TRACKED_CAP", 8, raising=False)
    with pytest.raises(module().Refusal, match=r"TRACKED_CAP.*long-link"):
        getattr(module(), function)(target, "120000")


def test_oversized_tracked_candidate_starts_no_calls(lane, monkeypatch):
    root, _, evidence, document = lane
    (root / "source.txt").write_bytes(b"x" * 1025)
    git(root, "add", "source.txt")
    git(root, "commit", "-qm", "large tracked fixture")
    head = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(module(), "TRACKED_CAP", 1024, raising=False)
    loaded, built = [], []
    result, calls, message = invoke(
        (root, head, evidence, document),
        loader=lambda value: loaded.append(value) or document,
        builder=lambda value: built.append(value) or "offline-tests /fake/offline\n",
    )
    assert result == 1, "oversized tracked candidate was accepted"
    assert "TRACKED_CAP" in message and "source.txt" in message
    assert loaded == [] and built == [] and calls == []
    assert not evidence.exists()


def test_oversized_report_preflight_starts_no_builder_or_evidence(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:284 `require_size(info.st_size, REPORT_CAP, f"REPORT_CAP {report}")` -> `None`
    root, _, evidence, _ = lane
    (root / "report.xml").write_bytes(b"123456789")
    monkeypatch.setattr(module(), "REPORT_CAP", 8, raising=False)
    built = []
    result, calls, message = invoke(
        lane, builder=lambda value: built.append(value) or "offline-tests /fake/offline\n"
    )
    assert result == 1, "oversized report was accepted before gates"
    assert "REPORT_CAP" in message and "report.xml" in message
    assert built == [] and calls == []
    assert not evidence.exists()


def test_failed_gate_oversized_report_has_summary_and_no_copy(lane, monkeypatch):
    root, _, evidence, _ = lane
    monkeypatch.setattr(module(), "REPORT_CAP", 8, raising=False)

    def execute(*args, **kwargs):
        (root / "report.xml").write_bytes(b"123456789")
        return 3

    result, calls, message = invoke(lane, execute=execute)
    assert not (evidence / "reports/report.xml").exists(), "oversized gate report was copied"
    assert result == 1 and len(calls) == 1
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["exit"] == 1
    assert summary["jobs"]["offline-tests"]["commands"][0]["exit"] == 3
    assert any(
        "REPORT_CAP" in error and "report.xml" in error for error in summary["integrity_errors"]
    )
    assert "REPORT_CAP" in message


def test_growing_report_copy_is_capped_and_partial_copy_removed(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:540 `target.unlink(missing_ok=True)` -> `None`
    # kills: scripts/verify/run_gates.py:87 `require_size(total, cap, label)` -> `None`
    from contextlib import contextmanager

    root, _, evidence, _ = lane
    report = root / "report.xml"
    monkeypatch.setattr(module(), "REPORT_CAP", 8, raising=False)
    opened = module().report_source
    consumed = []

    class Reader:
        def __init__(self, source):
            self.source = source
            self.grew = False

        def read(self, count):
            data = self.source.read(count)
            consumed.append(len(data))
            if not self.grew:
                self.grew = True
                with report.open("ab") as output:
                    output.write(b"abcdefgh")
            return data

    @contextmanager
    def checked(root_value, name):
        with opened(root_value, name) as source:
            yield None if source is None else Reader(source)

    monkeypatch.setattr(module(), "report_source", checked)

    def execute(*args, **kwargs):
        report.write_bytes(b"1234")
        return 3

    result, calls, message = invoke(lane, execute=execute)
    assert not (evidence / "reports/report.xml").exists(), "growing report left copied bytes"
    assert result == 1 and len(calls) == 1
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["exit"] == 1
    assert any(
        "REPORT_CAP" in error and "report.xml" in error for error in summary["integrity_errors"]
    )
    assert sum(consumed) <= 9, "report read beyond the cap sentinel byte"
    assert "REPORT_CAP" in message


@pytest.mark.parametrize("fault", ["path", "step", "exception", "integrity"])
def test_external_text_emissions_escape_record_controls(lane, fault):
    # kills: scripts/verify/run_gates.py:63 `return ascii(str(value))` -> `return str(value)`
    root, _, evidence, document = lane
    value = "bad\nFORGED\t\x1b\x7f.txt"
    target = root / value
    kwargs = {}
    if fault in {"path", "integrity"}:
        target.write_text("original")
        git(root, "add", "--", value)
        git(root, "commit", "-qm", "control-name fixture")
        head = git(root, "rev-parse", "HEAD")
        lane = root, head, evidence, document
        if fault == "path":
            git(root, "update-index", "--skip-worktree", "--", value)
            target.write_text("changed")
        else:

            def execute(*args, **kwargs):
                target.write_text("changed")
                return 0

            kwargs["execute"] = execute
    elif fault == "step":
        document["jobs"]["offline-tests"]["steps"][0]["name"] = value
    else:

        def build(_):
            raise ValueError(value)

        kwargs["builder"] = build
    result, _, message = invoke(lane, **kwargs)
    assert len(message.splitlines()) == 1, "external text forged multiple terminal records"
    assert all(ord(char) >= 32 and ord(char) != 127 for char in message), (
        "external text left a terminal control byte"
    )
    assert "bad\\nFORGED\\t\\x1b\\x7f.txt" in message, "escaped diagnostic lost the useful name"
    assert result == 1
    if fault == "integrity":
        summary = json.loads((evidence / "summary.json").read_text())
        assert any(value in error for error in summary["integrity_errors"])
        assert summary["exit"] == 1
    else:
        assert not evidence.exists()


def test_process_exception_log_escapes_record_controls(lane):
    # kills: scripts/verify/run_gates.py:492 `terminal_text(exc).encode()` -> `str(exc).encode()`
    def execute(*args, **kwargs):
        raise OSError("cannot\nFORGED\t\x1b\x7f")

    result, _, _ = invoke(lane, execute=execute)
    lines = (lane[2] / "offline-tests-01.log").read_text().splitlines()
    assert len(lines) == 1, "process exception forged multiple log records"
    assert all(ord(char) >= 32 and ord(char) != 127 for char in lines[0])
    assert "cannot\\nFORGED\\t\\x1b\\x7f" in lines[0]
    assert result == 127


def test_cli_argument_error_escapes_record_controls(lane, capsys):
    # kills: scripts/verify/run_gates.py:68 `super().error(terminal_text(message))` -> `super().error(message)`
    root, head, evidence, _ = lane
    with pytest.raises(SystemExit) as stopped:
        module().main(
            [
                "--root",
                str(root),
                "--candidate",
                head,
                "--evidence",
                str(evidence),
                "--bad\nFORGED\t\x1b",
            ]
        )
    assert stopped.value.code == 2
    text = capsys.readouterr().err
    lines = [line for line in text.splitlines() if "FORGED" in line]
    assert len(lines) == 1 and lines[0].startswith("run_gates.py: error:"), (
        "argument error forged a terminal record"
    )
    assert "--bad\\nFORGED\\t\\x1b" in lines[0]
    assert all(ord(char) >= 32 and ord(char) != 127 for char in lines[0])
    assert not evidence.exists()


@pytest.mark.parametrize("job", ["offline-tests", "lint", "dependency-audit"])
@pytest.mark.parametrize("kind", ["empty", "skip-only", "blank-run"])
def test_every_job_without_runnable_commands_is_refused(lane, job, kind):
    # kills: scripts/verify/run_gates.py:232 `if not commands:` -> `if False and not commands:`
    root, head, evidence, _ = lane
    steps = []
    if kind == "skip-only":
        steps = [{"name": "Install pinned pip-audit", "run": "pip install -r x.lock"}]
    elif kind == "blank-run":
        steps = [{"name": "Ruff lint", "run": "  \n# no command\n"}]
    document = {"jobs": {job: {"steps": steps}}}
    built = []
    result, calls, message = invoke(
        (root, head, evidence, document),
        builder=lambda value: built.append(value) or f"{job} /fake/{job}\n",
    )
    assert result == 1, "job without runnable commands returned a false PASS"
    assert job in message and "runnable" in message
    assert built == [] and calls == [], "empty job reached builder or gates"
    assert not evidence.exists()


def test_postgate_tracked_cap_keeps_named_integrity_evidence(lane, monkeypatch):
    # kills: scripts/verify/run_gates.py:523 `changed.append(f"{path}: {exc}")` -> `changed.append(path)`
    root, _, evidence, _ = lane
    monkeypatch.setattr(module(), "TRACKED_CAP", 1024, raising=False)

    def execute(*args, **kwargs):
        (root / "source.txt").write_bytes(b"x" * 1025)
        return 0

    result, calls, _ = invoke(lane, execute=execute)
    summary = json.loads((evidence / "summary.json").read_text())
    assert any(
        "TRACKED_CAP" in error and "source.txt" in error for error in summary["integrity_errors"]
    ), "tracked post-gate cap lost its named integrity evidence"
    assert result == 1 and summary["exit"] == 1
    assert len(calls) == 3


@pytest.mark.parametrize("returncode", [0, 7])
def test_captured_child_stderr_cannot_forge_terminal_records(capfd, returncode):
    # kills: scripts/verify/run_gates.py:99 `detail = terminal_text(stderr.decode(errors="replace"))` -> `detail = stderr.decode(errors="replace")`
    # kills: scripts/verify/run_gates.py:93 `done = subprocess.run(argv, input=input, stdout=output, stderr=error, check=False)` -> `done = subprocess.run(argv, input=input, stdout=output, check=False)`
    payload = "child\nforged\t\x1b[31m\x7f"
    program = (
        f"import sys; sys.stdout.write('ok'); sys.stderr.write({payload!r}); sys.exit({returncode})"
    )
    refusal = None
    try:
        data = module().capture(
            [__import__("sys").executable, "-c", program], cap=32, label="TEST_CAP"
        )
    except module().Refusal as exc:
        refusal = str(exc)
    assert capfd.readouterr().err == "", "captured child stderr forged terminal records"
    if returncode:
        assert refusal is not None
        assert "command exited 7" in refusal
        assert "child\\nforged\\t\\x1b[31m\\x7f" in refusal
        assert len(refusal.splitlines()) == 1, "child refusal forged terminal records"
    else:
        assert refusal is None
        assert data == b"ok", "child stderr changed captured stdout"


def test_captured_child_stderr_read_is_capped(monkeypatch, capfd):
    # kills: scripts/verify/run_gates.py:97 `stderr = read_capped(error, STDERR_CAP, "STDERR_CAP")` -> `stderr = error.read()`
    monkeypatch.setattr(module(), "STDERR_CAP", 8, raising=False)
    with pytest.raises(module().Refusal, match="STDERR_CAP"):
        module().capture(
            [__import__("sys").executable, "-c", "import sys; sys.stderr.write('x' * 9)"],
            cap=32,
            label="TEST_CAP",
        )
    assert capfd.readouterr().err == "", "oversized child stderr reached terminal"
