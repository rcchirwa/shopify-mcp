#!/usr/bin/env python3
"""Run candidate CI gates with explicit checkout and evidence paths.

Bounded reads: WORKFLOW_CAP = 1 MiB, PYPROJECT_CAP = 1 MiB, BUILDER_CAP = 1 MiB,
GIT_CAP = 16 MiB, PARSER_CAP = 2 MiB, TRACKED_CAP = 16 MiB,
REPORT_CAP = 64 MiB, STDERR_CAP = 64 KiB. Gate output streams to log files.

Ported from Holocron. shopify-mcp: the venvs from ci_venvs.py hold no install of this
project, so every gate runs with ``PYTHONPATH=<root>/src`` (the candidate) and a fresh
``PYTHONPYCACHEPREFIX`` (no stale bytecode from the tree). CI's own install of the
candidate runs as ``PACKAGING_CHECK``, a dry run. A command that is one of the
candidate's ``[project.scripts]`` runs as ``python -c`` calling that entry point.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, NoReturn

WORKFLOW_CAP = 1024 * 1024
PYPROJECT_CAP = 1024 * 1024
BUILDER_CAP = 1024 * 1024
GIT_CAP = 16 * 1024 * 1024
PARSER_CAP = 2 * 1024 * 1024
TRACKED_CAP = 16 * 1024 * 1024
REPORT_CAP = 64 * 1024 * 1024
STDERR_CAP = 64 * 1024

WORKFLOW = ".github/workflows/test.yml"
RUN_STEPS = {
    "Run offline tests under coverage",
    "Enforce 100% coverage",
    "Dependency drift check",
    "Ruff lint",
    "Ruff format check",
    "Mypy",
    "pip-audit against requirements.lock",
    "pip-audit against requirements-dev.lock",
    "pip-audit against requirements-audit.lock",
}
INSTALL_STEPS = {
    "Install pinned dependencies from lockfile",
    "Install pinned pip-audit",
}
# The candidate's own install is not run into the shared venv; a dry run of it still
# builds the candidate's metadata, so a packaging break fails here as it does in CI.
CANDIDATE_STEPS = {"Install package (deps already pinned above)": "pip install --no-deps -e ."}
PACKAGING_CHECK = ("python", "-m", "pip", "install", "--dry-run", "--no-deps", "-e", ".")
METADATA_SLACK = 2.0
SKIP_STEPS: dict[str, str] = {}
SKIP_JOBS = {
    "commit-messages": "needs a pull request's base and head; .githooks/commit-msg runs the same check",
}
COMMANDS = {"coverage", "ruff", "mypy", "pip-audit", "shopify-mcp-check-deps"}
ENTRY_POINT = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)")


class Refusal(ValueError):
    pass


def terminal_text(value: object) -> str:
    return ascii(str(value))


class EscapedParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        super().error(terminal_text(message))


def read_capped(source: IO[bytes], cap: int, label: str) -> bytes:
    data = source.read(cap + 1)
    if len(data) > cap:
        raise Refusal(f"{label} exceeded ({cap} bytes)")
    return data


def require_size(size: int, cap: int, label: str) -> None:
    if size > cap:
        raise Refusal(f"{label} exceeded ({cap} bytes)")


def bounded_blocks(source: IO[bytes], cap: int, label: str) -> Iterator[bytes]:
    total = 0
    while block := source.read(min(65536, cap - total + 1)):
        total += len(block)
        require_size(total, cap, label)
        yield block


def capture(argv: list[str], *, cap: int, label: str, input: bytes | None = None) -> bytes:
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        done = subprocess.run(argv, input=input, stdout=output, stderr=error, check=False)
        output.seek(0)
        data = read_capped(output, cap, label)
        error.seek(0)
        stderr = read_capped(error, STDERR_CAP, "STDERR_CAP")
    if done.returncode:
        detail = terminal_text(stderr.decode(errors="replace"))
        raise Refusal(f"{label}: command exited {done.returncode}: {detail}")
    return data


def load_workflow(
    workflow: Path,
    *,
    parser_python: str | None = None,
    source_mode: str | None = "100644",
    workflow_mode: str | None = "100644",
) -> Any:
    if parser_python is None:
        root = workflow.parents[2]
        spec = importlib.util.spec_from_file_location(
            "gate_ci_venvs", root / "scripts/verify/ci_venvs.py"
        )
        assert spec is not None and spec.origin is not None
        cv = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cv
        with regular_source(Path(spec.origin), source_mode) as (source, _):
            source_bytes = read_capped(source, BUILDER_CAP, "BUILDER_CAP")
        exec(compile(source_bytes, spec.origin, "exec"), cv.__dict__)
        venv, _ = cv.ensure_venv(
            cv.parser_job(), Path.home() / ".cache/shopify-mcp-ci", sys.executable
        )
        parser_python = str(venv / "bin/python")
    with regular_source(workflow, workflow_mode) as (source, _):
        workflow_bytes = read_capped(source, WORKFLOW_CAP, "WORKFLOW_CAP")
    program = (
        "import json, sys, yaml\n"
        "text = sys.stdin.buffer.read(int(sys.argv[1]) + 1).decode('utf-8')\n"
        "if len(text.encode('utf-8')) > int(sys.argv[1]): sys.exit(6)\n"
        "if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text, yaml.SafeLoader)): sys.exit(3)\n"
        "print(json.dumps(yaml.safe_load(text)))\n"
    )
    data = capture(
        [parser_python, "-I", "-c", program, str(WORKFLOW_CAP)],
        input=workflow_bytes,
        cap=PARSER_CAP,
        label="PARSER_CAP",
    )
    return json.loads(data)


def build_venvs(root: Path, *, capture_fn: Callable[..., bytes] | None = None) -> str:
    run = capture_fn or capture
    return run(
        [
            sys.executable,
            str(root / "scripts/verify/ci_venvs.py"),
            "--workflow",
            str(root / WORKFLOW),
            "--root",
            str(root),
            "--python",
            sys.executable,
        ],
        cap=BUILDER_CAP,
        label="BUILDER_CAP",
    ).decode()


def execute_command(argv: list[str], *, cwd: Path, env: dict[str, str], stdout: IO[bytes]) -> int:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        check=False,
    ).returncode


def plan(
    document: Any,
) -> tuple[dict[str, list[list[str]]], list[dict[str, Any]], set[str]]:
    jobs: dict[str, list[list[str]]] = {}
    classified: list[dict[str, Any]] = []
    reports: set[str] = set()
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("jobs"), dict)
        or not document["jobs"]
    ):
        raise Refusal("workflow jobs must be a nonempty mapping")
    for job, body in document["jobs"].items():
        if job in SKIP_JOBS:
            classified.append({"job": job, "step": None, "reason": SKIP_JOBS[job], "run": False})
            continue
        if (
            job not in {"offline-tests", "lint", "dependency-audit"}
            or not isinstance(body, dict)
            or not isinstance(body.get("steps"), list)
        ):
            raise Refusal("unknown job or malformed workflow steps")
        commands: list[list[str]] = []
        for step in body["steps"]:
            if not isinstance(step, dict):
                raise Refusal("workflow step must be a mapping")
            if "run" not in step:
                continue
            name = step.get("name", "")
            if not isinstance(name, str) or not isinstance(step["run"], str):
                raise Refusal("workflow run step needs a name and command text")
            if name in CANDIDATE_STEPS:
                if step["run"].strip() != CANDIDATE_STEPS[name]:
                    raise Refusal(f"unexpected candidate install in {job}/{name}")
                classified.append({"job": job, "step": name, "reason": None, "run": True})
                commands.append(list(PACKAGING_CHECK))
                continue
            if name in INSTALL_STEPS:
                reason = "ci_venvs.py owns install steps"
            elif name in SKIP_STEPS:
                reason = SKIP_STEPS[name]
            elif name in RUN_STEPS:
                reason = None
            else:
                raise Refusal(f"unknown CI run step: {job}/{name}")
            classified.append({"job": job, "step": name, "reason": reason, "run": reason is None})
            if reason is not None:
                continue
            for line in step["run"].splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                # Quoted Python code is an argument, not a shell command.
                lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
                lexer.whitespace_split = True
                tokens = list(lexer)
                if (
                    any(token and set(token) <= set("();<>|&") for token in tokens)
                    or any(char in line for char in "$`\\")
                    or any(char in "*?[~{}" for char in unquoted(line))
                ):
                    raise Refusal(f"shell syntax in {job}/{name}")
                argv = shlex.split(line, comments=True)
                if not argv or argv[0] not in COMMANDS:
                    raise Refusal(f"unknown command in {job}/{name}")
                commands.append(argv)
                for token in argv:
                    if token.startswith("--junitxml="):
                        path = Path(token.split("=", 1)[1])
                        if path.is_absolute() or ".." in path.parts or path == Path("."):
                            raise Refusal("junitxml path must stay inside the worktree")
                        reports.add(path.as_posix())
        if not commands:
            raise Refusal(f"job has no runnable commands: {job}")
        jobs[job] = commands
    return jobs, classified, reports


def project_scripts(root: Path, head_mode: str | None) -> dict[str, tuple[str, str]]:
    """Return the candidate's ``[project.scripts]`` as name -> (module, function)."""
    if head_mode is None:
        raise Refusal("pyproject.toml is not tracked at HEAD")
    with regular_source(root / "pyproject.toml", head_mode) as (source, _):
        data = read_capped(source, PYPROJECT_CAP, "PYPROJECT_CAP")
    scripts: dict[str, tuple[str, str]] = {}
    for name, target in tomllib.loads(data.decode()).get("project", {}).get("scripts", {}).items():
        match = ENTRY_POINT.fullmatch(target)
        if not match:
            raise Refusal(f"unsupported entry point: {name}")
        scripts[name] = (match[1], match[2])
    return scripts


def stale_metadata(root: Path, since: float) -> list[str]:
    """Return each ``src/*.egg-info`` whose PKG-INFO was not rewritten since ``since``.

    With ``src/`` on PYTHONPATH, importlib.metadata reads these folders, so one left
    over from an older install would give the gates metadata that CI never sees.
    ``METADATA_SLACK`` allows for file systems that store whole-second times.
    """
    stale: list[str] = []
    for info in sorted((root / "src").glob("*.egg-info")):
        try:
            fresh = (info / "PKG-INFO").stat().st_mtime >= since - METADATA_SLACK
        except OSError:
            fresh = False
        if not fresh:
            stale.append(info.relative_to(root).as_posix())
    return stale


def unquoted(line: str) -> Iterator[str]:
    quote = None
    for char in line:
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        else:
            yield char


@contextmanager
def regular_source(path: Path, head_mode: str | None) -> Iterator[tuple[IO[bytes], os.stat_result]]:
    if head_mode not in {"100644", "100755"}:
        raise Refusal(f"dirty tracked path has wrong HEAD type: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise Refusal(f"dirty tracked path is not regular: {path}")
        if bool(info.st_mode & stat.S_IXUSR) != (head_mode == "100755"):
            raise Refusal(f"dirty tracked path has wrong mode: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source, info
    finally:
        os.close(descriptor)


@contextmanager
def report_source(root: Path, report: str) -> Iterator[IO[bytes] | None]:
    descriptors: list[int] = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptors.append(os.open(root, directory_flags))
        for part in Path(report).parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(
            Path(report).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptors[-1],
        )
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refusal(f"unsafe report type or link count: {report}")
        require_size(info.st_size, REPORT_CAP, f"REPORT_CAP {report}")
    except FileNotFoundError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        yield None
        return
    except (OSError, Refusal) as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise Refusal(f"unsafe report {report}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def content_hash(path: Path, head_mode: str) -> str:
    label = f"TRACKED_CAP {path}"
    if head_mode == "120000":
        data = os.fsencode(os.readlink(path))
        require_size(len(data), TRACKED_CAP, label)
        return hashlib.sha256(data).hexdigest()
    with regular_source(path, head_mode) as (source, info):
        require_size(info.st_size, TRACKED_CAP, label)
        digest = hashlib.sha256()
        for block in bounded_blocks(source, TRACKED_CAP, label):
            digest.update(block)
        return digest.hexdigest()


def git_blob_hash(path: Path, head_mode: str) -> str:
    label = f"TRACKED_CAP {path}"
    digest = hashlib.sha1(usedforsecurity=False)
    if head_mode == "120000":
        data = os.fsencode(os.readlink(path))
        require_size(len(data), TRACKED_CAP, label)
        digest.update(f"blob {len(data)}\0".encode())
        digest.update(data)
    else:
        with regular_source(path, head_mode) as (source, info):
            require_size(info.st_size, TRACKED_CAP, label)
            digest.update(f"blob {info.st_size}\0".encode())
            for block in bounded_blocks(source, TRACKED_CAP, label):
                digest.update(block)
    return digest.hexdigest()


def verify_head_content(root: Path, git: list[str], head: str) -> dict[str, str]:
    records = capture([*git, "ls-tree", "-rz", "--full-tree", head], cap=GIT_CAP, label="GIT_CAP")
    paths: dict[str, str] = {}
    changed: list[str] = []
    for record in records.decode().split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, blob_oid = metadata.split()
        if kind != "blob":
            raise Refusal(f"tracked path is not a file: {name}")
        path = root / name
        paths[name] = mode
        try:
            if git_blob_hash(path, mode) != blob_oid:
                changed.append(name)
        except Refusal as exc:
            changed.append(f"{name}: {exc}")
        except OSError:
            changed.append(name)
    if changed:
        raise Refusal("dirty tracked content differs from HEAD: " + ", ".join(changed))
    return paths


def main(
    argv: list[str] | None = None,
    *,
    load: Callable[..., Any] | None = None,
    build: Callable[[Path], str] | None = None,
    execute: Callable[..., int] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
    emit: Callable[[str], object] = print,
) -> int:
    parser = EscapedParser(prog="run_gates.py", description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--git-path", default="/opt/homebrew/bin/git" if platform == "darwin" else "/usr/bin/git"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    load = load or load_workflow
    build = build or build_venvs
    execute = execute or execute_command
    try:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", args.candidate):
            raise Refusal("candidate must be 40 hex characters")
        if which("git") != args.git_path:
            raise Refusal(f"unexpected git path; expected {args.git_path}")
        git = [args.git_path, "--no-replace-objects", "-C", str(root)]
        top = (
            capture([*git, "rev-parse", "--show-toplevel"], cap=GIT_CAP, label="GIT_CAP")
            .decode()
            .strip()
        )
        if Path(top).resolve() != root:
            raise Refusal("root is not a worktree root")
        head = capture([*git, "rev-parse", "HEAD"], cap=GIT_CAP, label="GIT_CAP").decode().strip()
        if head != args.candidate.lower():
            raise Refusal("HEAD differs from candidate")
        if os.path.lexists(args.evidence):
            raise Refusal("evidence folder already exists")
        evidence = args.evidence.resolve()
        if evidence.is_relative_to(root):
            raise Refusal("evidence folder must be outside the worktree")
        workflow = root / WORKFLOW
        entries = (
            capture(
                [*git, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cap=GIT_CAP,
                label="GIT_CAP",
            )
            .decode()
            .split("\0")
        )
        if any(entry and not entry.startswith("?? ") for entry in entries):
            raise Refusal("dirty tracked worktree")
        paths = verify_head_content(root, git, head)
        workflow_mode = paths.get(WORKFLOW)
        with regular_source(workflow, workflow_mode) as (source, _):
            read_capped(source, WORKFLOW_CAP, "WORKFLOW_CAP")
        snapshot = {path: content_hash(root / path, paths[path]) for path in paths}
        document = (
            load(
                workflow,
                source_mode=paths.get("scripts/verify/ci_venvs.py"),
                workflow_mode=workflow_mode,
            )
            if load is load_workflow
            else load(workflow)
        )
        jobs, classified, reports = plan(document)
        scripts = project_scripts(root, paths.get("pyproject.toml"))
        for report in reports:
            with report_source(root, report):
                pass
        if any(
            entry and not (entry.startswith("?? ") and entry[3:] in reports) for entry in entries
        ):
            raise Refusal("dirty worktree")
        if platform == "darwin" and which("caffeinate") != "/usr/bin/caffeinate":
            raise Refusal("unexpected caffeinate path")
        envs = {}
        for line in build(root).splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2 or fields[0] in envs or not Path(fields[1]).is_absolute():
                raise Refusal("malformed venv builder output")
            envs[fields[0]] = fields[1]
        if set(envs) != set(jobs):
            raise Refusal("venv builder job set differs from workflow")
        if capture(
            [*git, "rev-parse", "HEAD"], cap=GIT_CAP, label="GIT_CAP"
        ).decode().strip() != head or any(
            content_hash(root / path, paths[path]) != digest for path, digest in snapshot.items()
        ):
            raise Refusal("candidate changed during preflight")
        args.evidence.mkdir(parents=True)
        summary: dict[str, Any] = {
            "candidate": head,
            "root": str(root),
            "classification": classified,
            "jobs": {},
            "exit": 0,
            "monotonic_clock": time.get_clock_info("monotonic").implementation,
        }
        with tempfile.TemporaryDirectory(prefix="run-gates-pycache-") as pycache:
            for job, commands in jobs.items():
                job_wall, job_mono = time.time(), time.monotonic()
                records: list[dict[str, Any]] = []
                summary["jobs"][job] = {"commands": records}
                for index, command in enumerate(commands, 1):
                    packaging = tuple(command) == PACKAGING_CHECK
                    bin_dir = Path(envs[job]) / "bin"
                    if command[0] in scripts:
                        module, function = scripts[command[0]]
                        call = (
                            f"import sys; from {module} import {function}; sys.exit({function}())"
                        )
                        command = [str(bin_dir / "python"), "-c", call, *command[1:]]
                    else:
                        command = [str(bin_dir / command[0]), *command[1:]]
                    environment = dict(os.environ)
                    environment["PYTHONPATH"] = str(root / "src")
                    environment["PYTHONPYCACHEPREFIX"] = pycache
                    environment["PATH"] = str(bin_dir) + os.pathsep + environment.get("PATH", "")
                    if platform == "darwin":
                        command = ["/usr/bin/caffeinate", "-i", *command]
                    wall, mono = time.time(), time.monotonic()
                    stem = f"{job}-{index:02d}"
                    with (args.evidence / f"{stem}.log").open("wb") as output:
                        try:
                            code = execute(command, cwd=root, env=environment, stdout=output)
                        except OSError as exc:
                            output.write(terminal_text(exc).encode() + b"\n")
                            code = 127
                        stale = stale_metadata(root, wall) if packaging and not code else []
                        if stale:
                            note = (
                                f"metadata not refreshed by the packaging check: {', '.join(stale)}"
                            )
                            output.write(terminal_text(note).encode() + b"\n")
                            code = 1
                    (args.evidence / f"{stem}.exit").write_text(f"{code}\n")
                    records.append(
                        {
                            "argv": command,
                            "exit": code,
                            "wall_seconds": time.time() - wall,
                            "monotonic_seconds": time.monotonic() - mono,
                        }
                    )
                    if code:
                        summary["exit"] = code
                        break
                summary["jobs"][job].update(
                    wall_seconds=time.time() - job_wall,
                    monotonic_seconds=time.monotonic() - job_mono,
                )
                if summary["exit"]:
                    break
        errors = []
        if (
            capture([*git, "rev-parse", "HEAD"], cap=GIT_CAP, label="GIT_CAP").decode().strip()
            != head
        ):
            errors.append("HEAD moved during gates")
        changed = []
        for path, digest in snapshot.items():
            try:
                if content_hash(root / path, paths[path]) != digest:
                    changed.append(path)
            except Refusal as exc:
                changed.append(f"{path}: {exc}")
            except OSError:
                changed.append(path)
        if changed:
            errors.append("tracked content changed: " + ", ".join(changed))
        for report in reports:
            target = None
            try:
                with report_source(root, report) as source:
                    if source is not None:
                        target = args.evidence / "reports" / report
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("wb") as output:
                            for block in bounded_blocks(source, REPORT_CAP, f"REPORT_CAP {report}"):
                                output.write(block)
            except (OSError, Refusal) as exc:
                if target is not None:
                    target.unlink(missing_ok=True)
                errors.append(f"unsafe report {report}: {exc}")
        summary["integrity_errors"] = errors
        if errors:
            summary["exit"] = 1
            for error in errors:
                emit(terminal_text(error))
        (args.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary["exit"]
    except (Refusal, OSError, subprocess.CalledProcessError, ValueError) as exc:
        emit(terminal_text(f"refused: {exc}"))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
