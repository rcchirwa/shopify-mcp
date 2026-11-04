#!/usr/bin/env python3
"""E33-S08 — build one long-lived venv per job of ``.github/workflows/test.yml``.

Ported from Holocron. This script reads each job's ``python-version`` and its
``pip install`` lines out of the workflow, and builds
``~/.cache/shopify-mcp-ci/<job>-<key>/g<N>``, where the key is the first 16 hex digits
of a sha256 of the version, those lines, and the sha256 of each ``-r`` file. The
workflow is read, never edited.

shopify-mcp: a venv holds the job's locked third-party packages only. The workflow's
``CANDIDATE_INSTALL`` line (``pip install --no-deps -e .``) is never run here: an
editable install points at the tree that built it, so a venv shared between worktrees
would test the wrong tree. ``run_gates.py`` supplies the candidate through
``PYTHONPATH`` instead. A job with neither a ``python-version`` nor an install line
(a shell-only job) gets no venv.

The workflow is loaded by PyYAML's ``safe_load`` in a venv of its own, built the same
way from ``PARSER_REQUIREMENTS``, and handed back as JSON; ``jobs_from_document`` then
reads the jobs. It refuses what the builder cannot reproduce, each refusal a
``WorkflowError``.

An interpreter on another version is refused, naming the version the job needs.
Install lines run through the venv's own ``python -m pip``, split by ``shlex``,
never through a shell, with only the ``PIP_ENVIRONMENT`` variables passed on and
``PIP_CONFIG_FILE`` set to the null device. A marker written after the last install
marks generation ``g<N>`` complete. A build removes incomplete generations and builds
the next one when the newest complete generation is missing, no longer reports the
job's version, or ``--rebuild`` is given; it never deletes a complete generation. A
per-key ``flock`` serializes sessions that build the same venv. A range such as
``pydantic>=2`` resolves when the venv is built.

stdout carries one ``<job> <venv path>`` line per built or reused job; notes and
errors go to stderr. A job whose steps run ``docker`` gets a note that Docker (and
Neo4j, when an install line names it) is not provided: the venv holds the job's
Python packages only.

Run:  python scripts/verify/ci_venvs.py [--workflow PATH] [--root DIR] [--python PATH] [--rebuild] [--prune]
Exit: the ``status = 1`` and ``return`` sites in ``main``.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKFLOW = DEFAULT_ROOT / ".github" / "workflows" / "test.yml"
MARKER = ".ci-venvs-key"

JOB_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
PARSER_NAME = "yaml.parser"
PARSER_REQUIREMENTS = Path(__file__).resolve().with_name("ci_venvs_parser.txt")
LOAD_DOCUMENT = (
    "import json, sys\n"
    "try:\n"
    "    import yaml\n"
    "    alias, parse, loader = yaml.AliasEvent, yaml.parse, yaml.SafeLoader\n"
    "    safe_load, yaml_error = yaml.safe_load, yaml.YAMLError\n"
    "except (ImportError, AttributeError):\n"
    "    sys.exit(4)\n"
    "with open(sys.argv[1], encoding='utf-8') as workflow:\n"
    "    text = workflow.read()\n"
    "try:\n"
    "    if any(isinstance(event, alias) for event in parse(text, loader)):\n"
    "        sys.exit(3)\n"
    "    document = safe_load(text)\n"
    "except yaml_error:\n"
    "    sys.exit(2)\n"
    "try:\n"
    "    print(json.dumps(document))\n"
    "except (TypeError, ValueError):\n"
    "    sys.exit(5)\n"
)
LOADER_REFUSALS = {
    2: "not valid YAML",
    3: "a YAML alias is refused",
    5: "a value JSON cannot carry, such as a date",
}
PARSER_BROKEN = 4
INSTALL_LINE = re.compile(r"^(?:python -m )?pip install(?:\s|$)")
# Not ``\b``: ``pip-audit`` is another tool, not a pip command.
PIP_WORD = re.compile(r"(?<![\w-])pip3?(?![\w-])")
CANDIDATE_INSTALL = ("--no-deps", "-e", ".")
REQUIREMENT_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
REQUIREMENT = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*\])?"
    r"(?:(?:==|!=|<=|>=|~=|<|>)[A-Za-z0-9.*+!-]+(?:,(?:==|!=|<=|>=|~=|<|>)[A-Za-z0-9.*+!-]+)*)?"
)
INSTALL_OPTIONS = frozenset(("--upgrade", "-U", "--require-hashes"))
ARCHIVE_SUFFIXES = (
    ".whl",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz",
    ".tar.xz",
    ".txz",
    ".tlz",
    ".tar.lz",
    ".tar.lzma",
    ".tar",
)
SHELL_OPERATOR_CHARS = frozenset("();<>|&")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
GENERATION = re.compile(r"g([0-9]+)")
PIP_SETTING = re.compile(r"(?<![A-Za-z0-9_])PIP_[A-Za-z0-9_]*")
PIP_ENVIRONMENT = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
PROBE_TIMEOUT = 60.0
LOAD_TIMEOUT = 60.0


class Runner(Protocol):
    def __call__(self, argv: list[str], *, isolated: bool = True) -> None: ...


Probe = Callable[[str], "str | None"]
Which = Callable[[str], "str | None"]


class WorkflowError(Exception):
    pass


class InterpreterError(Exception):
    pass


class ParserError(OSError):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    python_version: str
    install_lines: tuple[str, ...]
    services: tuple[str, ...]
    # (path as written, sha256 of its bytes) per ``-r`` file; set by ``pin_requirement_files``.
    requirement_files: tuple[tuple[str, str], ...] = ()


def _check_env(env: object, where: str) -> None:
    if env is None:
        return
    if not isinstance(env, dict):
        raise WorkflowError(f"{where}: env is not a mapping")
    for key in env:
        setting = PIP_SETTING.match(str(key).upper())
        if setting:
            raise WorkflowError(f"{where}: pip setting {setting.group(0)}")


def _unquoted(line: str) -> Iterator[tuple[int, str]]:
    """Yield (index, char) for each character bash reads outside quotes and escapes."""
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        else:
            yield index, char


def _shell_syntax(command: str) -> str | None:
    for index, char in _unquoted(command):
        if char == "#":
            return char if index and not command[index - 1].isspace() else None
        if char in "*?[":
            return char
    return None


def _install_lines(where: str, lines: list[str]) -> list[str]:
    """Return the run value's install lines; refuse a line the builder cannot reproduce."""
    install = []
    for command in lines:
        setting = PIP_SETTING.search(command)
        if setting:
            raise WorkflowError(f"{where}: pip setting {setting.group(0)}")
        if not command or command.startswith("#"):
            continue
        if not INSTALL_LINE.match(command):
            if PIP_WORD.search(command):
                raise WorkflowError(f"{where}: unrecognized pip command")
            continue
        if command.endswith("\\"):
            raise WorkflowError(f"{where}: line continuation")
        hazard = _shell_syntax(command)
        if hazard:
            raise WorkflowError(f"{where}: shell syntax {hazard!r}")
        try:
            tokens = shlex.split(command, comments=True)
        except ValueError:
            raise WorkflowError(f"{where}: cannot split") from None
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        if any(t and set(t) <= SHELL_OPERATOR_CHARS for t in lexer):
            raise WorkflowError(f"{where}: unsupported install argument")
        args = tokens[tokens.index("install") + 1 :]
        if tuple(args) != CANDIDATE_INSTALL:
            files = _requirement_positions(args)
            for index in files:
                if index >= len(args) or not REQUIREMENT_FILE.fullmatch(args[index]):
                    raise WorkflowError(f"{where}: unsupported requirement file")
            flags = {index - 1 for index in files}
            for index, arg in enumerate(args):
                if index in files or index in flags:
                    continue
                if arg not in INSTALL_OPTIONS and (
                    not REQUIREMENT.fullmatch(arg) or arg.lower().endswith(ARCHIVE_SUFFIXES)
                ):
                    raise WorkflowError(f"{where}: unsupported install argument")
        install.append(command)
    return install


def _requirement_positions(tokens: list[str]) -> list[int]:
    """Return the index of each ``-r`` file name in ``tokens`` (the one place ``-r`` is parsed).

    An index past the end means a trailing ``-r`` with no file.
    """
    return [index + 1 for index, token in enumerate(tokens) if token == "-r"]


def _requirement_files(line: str) -> list[str]:
    tokens = shlex.split(line, comments=True)
    return [tokens[index] for index in _requirement_positions(tokens) if index < len(tokens)]


def _is_candidate(line: str) -> bool:
    tokens = shlex.split(line, comments=True)
    return tuple(tokens[tokens.index("install") + 1 :]) == CANDIDATE_INSTALL


def _requirement_bytes(job: Job, root: Path, name: str) -> bytes:
    """Read a ``-r`` file; refuse a symlink, and a path that resolves outside ``root``."""
    path = root / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise WorkflowError(
            f"{job.name}: requirement file is not a regular file inside the repo: {name}"
        )
    return path.read_bytes()


def pin_requirement_files(job: Job, root: Path) -> Job:
    """Return ``job`` with the sha256 of each ``-r`` file under ``root``, for the key."""
    pinned = [
        (name, hashlib.sha256(_requirement_bytes(job, root, name)).hexdigest())
        for line in job.install_lines
        for name in _requirement_files(line)
    ]
    return dataclasses.replace(job, requirement_files=tuple(pinned))


def _copy_requirement_files(job: Job, root: Path, venv: Path) -> dict[str, Path]:
    """Copy each pinned ``-r`` file into ``venv``, checking its bytes against the pin.

    pip then reads the copy, so a file changed after this check cannot reach the venv.
    """
    named = [name for line in job.install_lines for name in _requirement_files(line)]
    if named != [name for name, _ in job.requirement_files]:
        raise OSError(f"{job.name}: requirement files are not pinned")
    copies = {}
    for name, digest in job.requirement_files:
        try:
            data = _requirement_bytes(job, root, name)
        except WorkflowError as exc:
            raise OSError(str(exc)) from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise OSError(f"{job.name}: a requirement file changed during the build")
        copy = venv / "ci-venvs-requirements" / name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(data)
        copies[name] = copy
    return copies


def _job(name: str, body: object) -> Job:
    if not isinstance(body, dict):
        raise WorkflowError(f"{name}: not a mapping")
    if "container" in body:
        raise WorkflowError(f"{name}: runs in a container")
    _check_env(body.get("env"), name)
    steps = body.get("steps", [])
    if not isinstance(steps, list):
        raise WorkflowError(f"{name}: steps is not a list")
    versions: list[str] = []
    install: list[str] = []
    commands: list[str] = []
    seen_install = False
    for number, step in enumerate(steps, 1):
        where = f"{name}: step {number}"
        if not isinstance(step, dict):
            raise WorkflowError(f"{where}: not a mapping")
        _check_env(step.get("env"), where)
        inputs = step.get("with")
        if isinstance(inputs, dict) and "python-version" in inputs:
            version = inputs["python-version"]
            if not isinstance(version, str) or not VERSION.fullmatch(version):
                raise WorkflowError(f"{where}: python-version must be a quoted version")
            versions.append(version)
        run = step.get("run")
        if run is None:
            continue
        if not isinstance(run, str):
            raise WorkflowError(f"{where}: run is not a string")
        lines = [line.strip() for line in run.splitlines()]
        commands += lines
        step_install = _install_lines(where, lines)
        if not step_install:
            continue
        if "if" in step:
            raise WorkflowError(f"{where}: install line in a conditional step")
        if len(step_install) != len([line for line in lines if line and not line.startswith("#")]):
            raise WorkflowError(f"{where}: install step holds a non-install line")
        seen_install = True
        install += [line for line in step_install if not _is_candidate(line)]
    if not versions:
        if seen_install:
            raise WorkflowError(f"{name}: no python-version")
        return Job(name, "", (), ())
    if len(set(versions)) > 1:
        raise WorkflowError(f"{name}: two python-versions {sorted(set(versions))}")
    services = []
    if any(command.split()[:1] == ["docker"] for command in commands):
        services.append("Docker")
        if any("neo4j" in line.lower() for line in install):
            services.append("Neo4j")
    return Job(name, versions[0], tuple(install), tuple(services))


def jobs_from_document(document: object) -> list[Job]:
    """Return the jobs of a workflow loaded by ``safe_load``, in file order."""
    if not isinstance(document, dict):
        raise WorkflowError("the workflow is not a mapping")
    _check_env(document.get("env"), "workflow")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise WorkflowError("no jobs found under a top-level `jobs:` key")
    found = []
    for name, body in jobs.items():
        if not isinstance(name, str) or not JOB_NAME.fullmatch(name):
            raise WorkflowError("a job name that cannot name a cache directory")
        found.append(_job(name, body))
    return found


def parser_job() -> Job:
    """The venv that loads the workflow, on the interpreter running this script."""
    version = ".".join(map(str, sys.version_info[:2]))
    install = f"pip install --only-binary=:all: --require-hashes -r {shlex.quote(str(PARSER_REQUIREMENTS))}"
    return Job(PARSER_NAME, version, (install,), ())


def read_document(python: str, path: Path) -> object:
    """Load ``path`` with ``yaml.safe_load`` under ``python -I``, handed back as JSON.

    A YAML alias is refused, since ``json.dumps`` would expand it.
    """
    try:
        done = subprocess.run(
            [python, "-I", "-c", LOAD_DOCUMENT, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=LOAD_TIMEOUT,
        )
    except OSError as exc:
        raise ParserError(f"cannot start {python}: {exc}") from None
    except subprocess.TimeoutExpired:
        raise WorkflowError("not loadable as YAML in time") from None
    if done.returncode == PARSER_BROKEN:
        raise ParserError(f"{python} cannot import yaml")
    if done.returncode in LOADER_REFUSALS:
        raise WorkflowError(LOADER_REFUSALS[done.returncode])
    if done.returncode != 0:
        raise ParserError(f"{python} exited {done.returncode} while loading the workflow")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise ParserError(f"{python} printed something other than JSON") from None


def content_key(job: Job) -> str:
    """The first 16 hex digits of a sha256 of what the venv holds; the job name is not part of it."""
    files = (f"{name} {digest}" for name, digest in job.requirement_files)
    key = "\n".join((job.python_version, *job.install_lines, *files))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def key_dir(cache_dir: Path, job: Job) -> Path:
    return cache_dir / f"{job.name}-{content_key(job)}"


def version_matches(wanted: str, actual: str | None) -> bool:
    if actual is None:
        return False
    return actual.split(".")[: len(wanted.split("."))] == wanted.split(".")


def find_interpreter(version: str, override: str | None, *, which: Which = shutil.which) -> str:
    if override:
        return override
    name = "python" + ".".join(version.split(".")[:2])
    found = which(name)
    if not found:
        raise InterpreterError(
            f"{name} is not on PATH; pass --python PATH to a Python {version} interpreter"
        )
    return found


def probe_version(python: str) -> str | None:
    """Return the interpreter's full version, or None if it does not run."""
    try:
        done = subprocess.run(
            [python, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = done.stdout.strip()
    return version if done.returncode == 0 and VERSION.fullmatch(version) else None


def run_command(argv: list[str], *, isolated: bool = True) -> None:
    """Run ``argv``; when ``isolated``, with only the ``PIP_ENVIRONMENT`` variables."""
    env = None
    if isolated:
        env = {name: os.environ[name] for name in PIP_ENVIRONMENT if name in os.environ}
        env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run(argv, check=True, stdout=sys.stderr, env=env)


def _pip_argv(venv_python: str, line: str, copies: dict[str, Path] | None = None) -> list[str]:
    tokens = shlex.split(line, comments=True)
    rest = tokens[3:] if tokens[0] == "python" else tokens[1:]
    if copies is not None:
        files = set(_requirement_positions(rest))
        rest = [str(copies[token]) if index in files else token for index, token in enumerate(rest)]
    return [venv_python, "-I", "-m", "pip", *rest]


def _generations(base: Path) -> list[tuple[int, Path]]:
    found = []
    for child in base.iterdir() if base.is_dir() else ():
        number = GENERATION.fullmatch(child.name)
        if number and child.is_dir():
            found.append((int(number.group(1)), child))
    return sorted(found)


def _open_lock(lock_path: str, *, wait: bool) -> IO[str] | None:
    """Return a handle that holds ``lock_path``'s lock; None if held and not ``wait``.

    A lock taken on a file that ``prune`` unlinked meanwhile is dropped and taken again.
    """
    while True:
        handle = open(lock_path, "a")  # noqa: SIM115 - returned to the caller, who closes it
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not wait:
                    handle.close()
                    return None
                print(f"waiting for {lock_path}, held by another build", file=sys.stderr)
                fcntl.flock(handle, fcntl.LOCK_EX)
            held = os.fstat(handle.fileno())
            try:
                current = os.stat(lock_path)
            except FileNotFoundError:
                handle.close()
                continue
        except BaseException:
            handle.close()
            raise
        if (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino):
            return handle
        handle.close()


def ensure_venv(
    job: Job,
    cache_dir: Path,
    python: str,
    *,
    run: Runner = run_command,
    probe: Probe = probe_version,
    rebuild: bool = False,
    root: Path | None = None,
) -> tuple[Path, bool]:
    """Return (venv path, True if it was built now). Holds the key's lock throughout.

    ``root`` resolves each ``-r`` file; pip reads a copy checked against the job's pin,
    and a file that changed since the key was made is refused.
    """
    base = key_dir(cache_dir, job)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = _open_lock(f"{base}.lock", wait=True)  # never None when waiting
    assert lock is not None
    with lock:
        if base.is_symlink():
            raise OSError(f"refusing a symlinked key dir: {base}")
        base.mkdir(exist_ok=True)
        generations = _generations(base)
        for _, path in generations:
            if path.is_symlink():
                raise OSError(f"refusing to build beside a symlinked generation: {path}")
        complete = [path for _, path in generations if (path / MARKER).is_file()]
        for _, path in generations:
            if path not in complete:
                shutil.rmtree(path)
        if (
            not rebuild
            and complete
            and version_matches(job.python_version, probe(str(complete[-1] / "bin" / "python")))
        ):
            return complete[-1], False
        path = base / f"g{generations[-1][0] + 1 if generations else 1}"
        run([python, "-I", "-m", "venv", str(path)], isolated=False)
        actual = probe(str(path / "bin" / "python"))
        if not version_matches(job.python_version, actual):
            raise OSError(
                f"{path} reports Python {actual or 'unrunnable'}, not {job.python_version}"
            )
        copies = None if root is None else _copy_requirement_files(job, root, path)
        for line in job.install_lines:
            run(_pip_argv(str(path / "bin" / "python"), line, copies))
        (path / MARKER).write_text(f"{base.name}/{path.name}\n")
    return path, True


def prune(job: Job, cache_dir: Path, keep: Path) -> list[Path]:
    """Delete the job's venvs other than ``keep``; return what was deleted."""
    removed: list[Path] = []
    pattern = re.compile(re.escape(job.name) + r"-[0-9a-f]{16}")
    for base in sorted(cache_dir.iterdir()):
        if not (pattern.fullmatch(base.name) and base.is_dir()):
            continue
        lock = _open_lock(f"{base}.lock", wait=False)
        if lock is None:
            print(f"skipped {base}: another build holds its lock", file=sys.stderr)
            continue
        with lock:
            if base.is_symlink():
                continue
            if not base.is_dir():
                Path(f"{base}.lock").unlink()
                continue
            for _, path in _generations(base):
                if path != keep and not path.is_symlink():
                    shutil.rmtree(path)
                    removed.append(path)
            if not any(base.iterdir()):
                base.rmdir()
                Path(f"{base}.lock").unlink()
    return removed


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = run_command,
    probe: Probe = probe_version,
    which: Which = shutil.which,
    load: Callable[[Path], object] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="repo root that `-r` files are read from (default: the repo holding this script)",
    )
    parser.add_argument(
        "--python", help="interpreter to build with, instead of python<major.minor> on PATH"
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="build a new generation of every venv"
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete each job's other generations and keys after its build, including any"
        " that another session is still running from",
    )
    args = parser.parse_args(argv)
    try:
        args.workflow.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot read {args.workflow}: {exc}", file=sys.stderr)
        return 2
    cache_dir = Path.home() / ".cache" / "shopify-mcp-ci"
    root = args.root.resolve()
    parser_venv: Path | None = None
    if load is None:
        try:
            parser_venv, _ = ensure_venv(
                parser_job(), cache_dir, sys.executable, run=run, probe=probe, rebuild=args.rebuild
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR: cannot build the YAML parser: {exc}", file=sys.stderr)
            return 1
        load = functools.partial(read_document, str(parser_venv / "bin" / "python"))
    try:
        jobs = [pin_requirement_files(job, root) for job in jobs_from_document(load(args.workflow))]
    except ParserError as exc:
        broken = key_dir(cache_dir, parser_job())
        print(
            f"ERROR: the YAML parser venv is broken; remove {broken} and run again: {exc}",
            file=sys.stderr,
        )
        return 1
    except WorkflowError as exc:
        print(f"ERROR: {args.workflow}: {exc}", file=sys.stderr)
        return 2
    status = 0
    if args.prune and parser_venv is not None:
        for gone in prune(parser_job(), cache_dir, parser_venv):
            print(f"{PARSER_NAME}: pruned {gone}", file=sys.stderr)
    shared: dict[str, Path] = {}
    for job in jobs:
        if not job.python_version:
            print(
                f"{job.name}: no python-version and no install line; no venv built", file=sys.stderr
            )
            continue
        if not job.install_lines:
            print(
                f"{job.name}: no pip install line in the workflow; no venv built", file=sys.stderr
            )
            status = 1
            continue
        try:
            python = find_interpreter(job.python_version, args.python, which=which)
            actual = probe(python)
            if not version_matches(job.python_version, actual):
                raise InterpreterError(
                    f"{job.name} needs Python {job.python_version}, but {python} is Python {actual or 'unrunnable'}"
                )
            # shopify-mcp: a job whose venv would hold the same packages as an earlier
            # job's (offline-tests and lint) uses that venv instead of building a copy.
            content = content_key(job)
            if content in shared:
                path, built = shared[content], False
            else:
                path, built = ensure_venv(
                    job, cache_dir, python, run=run, probe=probe, rebuild=args.rebuild, root=root
                )
                shared[content] = path
        except InterpreterError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            status = 1
            continue
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"{job.name}: build failed: {exc}", file=sys.stderr)
            status = 1
            continue
        print(f"{job.name}: {'built' if built else 'reused'} {path}", file=sys.stderr)
        if job.services:
            print(
                f"NOTE {job.name}: Python packages only; {' and '.join(job.services)} are not provided",
                file=sys.stderr,
            )
        print(f"{job.name} {path}")
        if args.prune:
            try:
                pruned = prune(job, cache_dir, path)
            except OSError as exc:
                print(f"{job.name}: prune failed: {exc}", file=sys.stderr)
                status = 1
                continue
            for gone in pruned:
                print(f"{job.name}: pruned {gone}", file=sys.stderr)
    return status


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
