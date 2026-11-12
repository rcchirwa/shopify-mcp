"""E32-S01 — classify the git worktrees registered in this repository. It removes nothing.

Worktrees are created per agent/workflow run. A run that closes out cleanly removes its own;
the residue comes from runs that do not — crashes, interrupts, abandoned sessions. That
residue has needed manual intervention before: `.claude/worktree-archive-2026-08-18/` holds
work rescued by hand from 13 worktrees.

The hard part is CLASSIFICATION, and the obvious filter is wrong. A worktree belonging to a
running session frequently carries no identifying name: it may sit on a DETACHED HEAD (no
branch at all) or on a generic-sounding branch (`worktree-agent-…`, `worktree-wf_…`) while
holding real in-flight work. Keying on names or paths therefore reports live worktrees as
unrelated.

LIVE is decided on COMMITS instead. A worktree is LIVE when its HEAD is one of the live SHAs,
or when its HEAD and a live SHA share at least one commit that is not reachable from the main
ref. The second half is load-bearing: the live SHAs are branch TIPS, so a detached worktree
that has committed anything of its own sits at no tip and equality alone never sees it.

`classify_worktrees` has arms that pre-empt the commit test. `entry.locked` calls a worktree
LIVE, yet a lock outlives the process that set it.

`git worktree prune` is deliberately NOT run: it also deregisters a worktree whose DIRECTORY
STILL EXISTS when its `.git` link is broken, which is the crashed-session shape this tool
exists to find.

This tool never says a worktree is EMPTY, and no verdict here means "safe to remove". That
narrowing is what the reads themselves forced. Whether `git status` SAYS anything is decided
by how git is configured and by the stat cache, so its silence carries no information about
the working tree, and nothing built on it can carry a claim of emptiness. Measured, git 2.53:
a SAME-SIZE edit whose mtime is preserved prints nothing at all — no hostile config is needed
for that one — and a `filter.*.clean` driver in the surveyed repository's own config silences
a same-size edit that git would otherwise re-hash and report.

What the reads still buy is the REASON on a DIRTY row, which is what an operator reads before
opening the directory. No claim is made that every way of silencing git is known; these moves
widen what that reason can name, each measured by a test rather than argued for here:

1. What an option can fold back into the ordinary diff is folded back there, on the COMMAND
   LINE, which git ranks above every config file — so one flag covers a setting wherever it
   was written. `core.fileMode=true` is pinned that way.
2. What only surfaces as its own category is COUNTED in its own right, read from the index
   rather than descended into: ignore rules via `--ignored=matching`; the `assume-unchanged`
   and `skip-worktree` index bits AND any in-tree gitlink (a `160000` entry), all read from
   `ls-files --stage -v`, whose paths are counted as UNKNOWN rather than as modified. A gitlink
   is counted from the index BECAUSE `git status` is told not to descend into it — see the
   probe, and the gitlink note under GIT_FIXED_PREFIX.
3. The identity check requires the git directory git reads there to be the one this
   repository records for THIS registration, AND the working tree it reports to BE the
   registered directory.

Asking instead whether that gitdir CLAIMS this repository is the weaker question — `commondir`
is a file inside the gitdir, so a forged one passed while running its own `config.worktree`
clean filter during `git status` (measured, git 2.53). Asking only whether it is the
repository's is weaker again — the common directory and a sibling worktree's gitdir both pass
that, and each replaced this entry's own reason with a reading of another registration's state
(measured, git 2.53). What stays trusted is the surveyed repository's own git directory.

A default run makes one outbound call, `gh pr list`, for the head branch of every open PR;
`--no-gh` skips it and the tool is then entirely local.

Usage::

    python scripts/maintenance/e32_s01_worktree_sweep.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

#: No environment variable with this prefix reaches a child process. GIT_DIR, GIT_WORK_TREE
#: and GIT_COMMON_DIR override the ``cwd=`` targeting; GIT_INDEX_FILE swaps the index that
#: `git status` reads, so it reports changes that do not exist; GIT_CONFIG_COUNT with
#: GIT_CONFIG_KEY_n/VALUE_n injects config. git's own `rev-parse --local-env-vars` list names
#: GIT_CONFIG_COUNT but NEITHER of the KEY_n/VALUE_n that carry its payload (measured, git
#: 2.53), so no enumeration is trusted: the prefix is dropped whole. This module
#: needs none of them — it never commits, pushes, edits or pages — so the cost is that a
#: harmless one such as GIT_TRACE is dropped with the rest.
GIT_ENV_PREFIX = "GIT_"

#: The config files git reads whose ENV NAMES do not begin with GIT_, and which the prefix
#: scrub therefore cannot reach: $XDG_CONFIG_HOME/git/config and $HOME/.gitconfig (global),
#: and the system file at git's BUILD PREFIX — `/etc/gitconfig` on a stock build, but
#: `/opt/homebrew/etc/gitconfig` where this was measured, so it is not a path to hard-code or
#: to assume absent. Each of these ran an injected `core.fsmonitor` command during `git
#: status` (measured, git 2.53). Dropping HOME instead is not an option — `gh` needs it — so
#: the two paths are pinned to os.devnull, which empties both without touching any other
#: tool's HOME. The system half is the one nothing else closes: the prefix scrub already
#: keeps a GIT_CONFIG_SYSTEM out of the child's environment, but the file at the build prefix
#: is read with no environment variable involved.
GIT_CONFIG_PINS = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

#: `core.fsmonitor` is a config key whose value git EXECUTES during `git status`, and
#: `--no-optional-locks` does not stop it (measured, git 2.53). A `-c` on the command line
#: outranks every config file at once, so this covers the surveyed repository's own config
#: too, not just the sources GIT_CONFIG_PINS empties. Of the other keys git runs commands
#: from, none is reachable from the subcommands in GIT_SUBCOMMANDS: aliases cannot shadow a
#: builtin, the pager is not started when stdout is a pipe, no hook fires, and
#: `diff.external`/textconv belong to commands this module does not run (each measured, git
#: 2.53). A `filter.*.clean` driver IS run by `git status` when a file must be re-hashed, and
#: it is reachable from two DIFFERENT kinds of source. The first is a config file — the
#: repository's own config, and, where `extensions.worktreeConfig` is set as it is here, a
#: worktree's `config.worktree` (each measured, git 2.53). The second is the config of a
#: nested in-tree gitlink, which lives inside the worktree and is NOT one of this repository's
#: git directories, so the identity check does not reach it (measured, git 2.53: a gitlink's
#: own `filter.*.clean` ran during an ordinary `git status --ignore-submodules=none`). It is
#: kept from running not by confinement but by `git status` being told NOT to descend
#: (`--ignore-submodules=dirty` in the probe below); the gitlink is then counted from the index
#: in its own right.
#:
#: `core.fileMode=true` rides here for the same reason and by the same mechanism, but against
#: a SILENCER rather than an executor: with `core.fileMode=false`, `git status` says nothing
#: at all about a tracked file whose mode changed, and the row's reason named nothing at all
#: (measured, git 2.53, from the local config and from a worktree's own `config.worktree`,
#: which no GIT_CONFIG_* pin can empty). The cost is that on a filesystem that cannot store
#: the exec bit every 100755 path reads as modified. That is the direction to fail in here: a
#: reason naming a path that turns out to be untouched costs a second look, and a reason
#: naming nothing is the row an operator skims past.
GIT_FIXED_PREFIX = (
    "git",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.fileMode=true",
)

#: Every git SUBCOMMAND this module runs.
#: Declared here as the SINGLE source of truth the comments in this
#: module refer to instead of counting by hand, so an enumeration can never fall short of the
#: code again: the test file derives this set from the module's own syntax tree and fails if a
#: run_git call adds a subcommand missing here. Every subcommand in it is read-only. The set is
#: derived, not counted, precisely because an earlier hand count omitted `rev-parse` — the
#: command run FIRST at a registered path whose identity is not yet established
#: (git_dir/work_tree_root), and so the one it is least safe to leave off a safety enumeration.
GIT_SUBCOMMANDS = frozenset(
    {"worktree", "status", "ls-files", "rev-list", "rev-parse", "for-each-ref"}
)

#: Every child process is bounded. A hostile `core.fsmonitor`, a `gh` waiting on auth and a
#: stale network mount all hang a plain `subprocess.run` forever, and this tool is run by an
#: operator watching it.
TIMEOUT_SECONDS = 60

#: run_git's own statuses, for a command that never started or never finished. `git_failure`
#: tests for each before it falls through to reporting an exit status, so neither may collide
#: with a code git itself returns. The commands run are the subcommands in GIT_SUBCOMMANDS.
#: Measured, git 2.53: an unknown option makes every one of them EXCEPT `rev-parse` exit 129,
#: so the ceiling is not 128; `rev-parse` is the exception and exits 0 on an unknown option,
#: not the 129 an earlier comment implied of the whole set. The other exits seen from these
#: commands were 0, 1 and 128 (a `rev-parse` that
#: cannot answer at a registered path exits 128), and a signal-killed child reaches Python as a
#: NEGATIVE code. None of those is 200 or 201.
EXIT_NOT_RUN = 200
EXIT_TIMED_OUT = 201

#: Ceiling on the chart file read into memory. The chart is ~0.8 MB; `json.loads` of a 48 MB
#: one drove 523 MB of peak RSS, so the read is refused rather than bounded after the fact.
MAX_CHART_BYTES = 8 * 1024 * 1024

MAIN = "MAIN"
LIVE = "LIVE"
DIRTY = "DIRTY"
BROKEN = "BROKEN"
MISSING = "MISSING"

#: The reason a DIRTY row carries when the reads named nothing. It reports the live-set search
#: this run performed and then refuses the question the reads cannot answer. Every word of it
#: is about what was LOOKED FOR and what this report will not say; none of it describes the
#: working tree, so there is no sentence here for a silenced `git status` to make false.
UNLINKED_REASON = "no source this run read links it to live work; what it holds is not judged here"

#: The problem the probe records when git could not list a worktree's commits.
COMMITS_UNLISTED = "could not list this worktree's commits against the main ref"


# --------------------------------------------------------------------------- git plumbing


def scrubbed_env() -> dict[str, str]:
    """The environment with every GIT_-prefixed name removed. What `gh` is given.

    `gh` reads its own credentials from under HOME and shells out to git for the remote it
    should talk to, so it keeps the ambient config that GIT_CONFIG_PINS empties for git.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith(GIT_ENV_PREFIX)}


def git_env() -> dict[str, str]:
    return {**scrubbed_env(), **GIT_CONFIG_PINS}


def run_git(
    cwd: Path, *args: str, pin: tuple[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one git command. Every git invocation in this module goes through here.

    `pin` is `(git directory, working tree)`, passed as `--git-dir` and `--work-tree` so git
    is told both rather than discovering either.

    `--no-optional-locks` is what keeps `git status` from rewriting a worktree's index when
    its stat cache is stale; without it that refresh is a write (measured, git 2.53). A
    worktree directory can vanish between `worktree list` and the command run inside it —
    the churn this tool surveys — so a failure to start is returned as a failed command, not
    raised. git's own stderr is dropped rather than returned: it can carry paths and
    credentialed remote URLs, and these reports get pasted into cards and PRs.

    The bytes are decoded HERE rather than by `text=True`, because text mode does two things
    and only one of them is wanted. It decodes — and a decode raises from inside
    `subprocess.run`, ahead of every handler here, which raw bytes in a lock reason once did
    (measured, git 2.53: a UnicodeDecodeError killed the whole report). It also performs
    UNIVERSAL-NEWLINE TRANSLATION, and `subprocess` offers no way to ask for one without the
    other. That second half is not cosmetic: `git worktree list --porcelain -z` does not quote
    paths, so a worktree directory holding a CARRIAGE RETURN arrived here as a different path
    than the one on disk — reported MISSING while it sat there holding unsaved work, and, with
    a newline-named twin also registered, collapsed onto that twin's row and was reported on
    someone else's state. Surrogate escapes are the decode that cannot raise AND still
    round-trips back to the filesystem, so an odd path stays usable; `one_line` is what keeps
    it out of `print`.
    """
    pinned = () if pin is None else (f"--git-dir={pin[0]}", f"--work-tree={pin[1]}")
    argv = [*GIT_FIXED_PREFIX, *pinned, *args]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            env=git_env(),
            timeout=TIMEOUT_SECONDS,
        )
    except OSError:
        return subprocess.CompletedProcess(argv, returncode=EXIT_NOT_RUN, stdout="", stderr="")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, returncode=EXIT_TIMED_OUT, stdout="", stderr="")
    return subprocess.CompletedProcess(
        argv,
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "surrogateescape"),
        stderr="",
    )


def git_failure(result: subprocess.CompletedProcess[str]) -> str:
    """Why a git command did not succeed, in this module's own words rather than git's."""
    if result.returncode == EXIT_TIMED_OUT:
        return f"timed out after {TIMEOUT_SECONDS}s"
    if result.returncode == EXIT_NOT_RUN:
        return "could not be started"
    return f"exit status {result.returncode}"


# --------------------------------------------------------------------------- porcelain


@dataclass(frozen=True)
class Worktree:
    """One entry of `git worktree list --porcelain -z`."""

    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    locked: bool = False
    lock_reason: str = ""
    prunable: bool = False
    prunable_reason: str = ""


def parse_worktree_porcelain(text: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain -z`: NUL after each attribute, NUL between records.

    The `-z` form is what makes the parse safe. Plain `--porcelain` does not quote paths, so a
    worktree directory whose name contains a NEWLINE injects entire synthetic `worktree …`
    records into a line-based parse (measured, git 2.53). Splitting on NUL cannot be fooled into
    reading a forged record, and it removes the need to guess where a record starts: a record
    with no `worktree` attribute is skipped rather than read as one. It says nothing about what
    a path CONTAINS — a byte rewritten before this function is called arrives here already
    wrong, which is why `run_git` decodes the bytes itself.
    """
    entries: list[Worktree] = []
    for record in text.split("\0\0"):
        attrs: dict[str, str] = {}
        for attribute in record.split("\0"):
            if not attribute:
                continue
            key, _, value = attribute.partition(" ")
            attrs[key] = value
        if "worktree" not in attrs:
            continue
        entries.append(
            Worktree(
                path=Path(attrs["worktree"]),
                head=attrs.get("HEAD") or None,
                branch=attrs.get("branch") or None,
                detached="detached" in attrs,
                bare="bare" in attrs,
                locked="locked" in attrs,
                lock_reason=attrs.get("locked", ""),
                prunable="prunable" in attrs,
                prunable_reason=attrs.get("prunable", ""),
            )
        )
    return entries


def list_worktrees(repo: Path) -> list[Worktree]:
    result = run_git(repo, "worktree", "list", "--porcelain", "-z")
    if result.returncode != 0:
        raise RuntimeError(f"git worktree list in {repo} failed: {git_failure(result)}")
    return parse_worktree_porcelain(result.stdout)


def main_worktree_path(entries: Sequence[Worktree]) -> Path | None:
    """git lists the main worktree first; everything after it is a linked worktree."""
    return entries[0].path if entries else None


def short_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    return branch[len("refs/heads/") :] if branch.startswith("refs/heads/") else branch


def path_answer(result: subprocess.CompletedProcess[str]) -> str | None:
    """The one PATH a `rev-parse` printed, resolved; None when it printed none.

    Every path this module reads out of git's stdout comes through here, because the trimming
    is the whole of what there is to get wrong and it was got wrong one call site at a time.
    ONE trailing LF is removed and nothing else: one is what git appends, and a directory NAME
    may legally end in whitespace of its own, which `str.strip()` took with it.

    The class at risk is exactly the code points `str.strip()` acts on, so the tests DERIVE it
    from `str.strip` rather than listing it — an earlier hand-typed list of them shipped short.
    Measured, git 2.53: `git worktree add` accepted a directory name ending in every member,
    and git splits the class in two when it derives the worktree NAME. It REWRITES the ASCII
    members, so for those the gitdir path carries nothing to strip and only `--show-toplevel`
    was ever hurt. It KEEPS the rest, so the gitdir path ends in the character too: the strip
    shortened it, `recorded_worktree` then read out of a directory that does not exist, and the
    tool printed BROKEN over a worktree whose registration, `.git` file and gitdir were all
    intact and which may hold the only copy of a file. Which members fall on which side is
    measured in a fixture, never written down here.

    `str.removesuffix` is what drops no more than the one. `str.rstrip("\\n")` is NOT the same
    thing: over a directory name that itself ends in LF it takes git's newline and the name's
    together, which is the same false BROKEN one character along.

    `os.path.realpath` rather than `Path.resolve`, which raises on a symlink cycle on 3.11.
    """
    if result.returncode != 0:
        return None
    answer = result.stdout.removesuffix("\n")
    return os.path.realpath(answer) if answer else None


def common_dir(path: Path) -> str | None:
    """The git directory shared by a repository and all of its worktrees; None if unreadable.

    Trimmed through `path_answer` like the other two path reads. This answer usually ends in
    the git directory's own NAME, so a trailing whitespace in the REPOSITORY's directory name
    lands in the MIDDLE of it: measured, git 2.53, for a repository directory named `repo `,
    raw stdout here is `…/repo /.git\\n`, on which `.strip()` returns exactly what
    `removesuffix("\\n")` does. Two layouts put the oddly named directory LAST instead — a
    BARE repository, where this answers the repository directory itself, and a
    `--separate-git-dir`, where it answers that separate directory. Measured on both, git
    2.53: a `.strip()` here shortened the answer, `owned_gitdir` then recognised no gitdir as
    the repository's, and two linked worktrees whose own names held no whitespace at all read
    BROKEN. This is the one read the `worktrees/` prefix comes from and every entry is
    compared against it, so a strip here costs the whole repository at once, where a strip at
    the other two sites costs the one oddly named worktree.
    """
    return path_answer(run_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def git_dir(path: Path) -> str | None:
    """The git directory git READS at `path`; None if it could not be read.

    Asked of a registered directory INSTEAD of `--git-common-dir`, which answers a strictly
    weaker question: the common directory is named by the `commondir` FILE inside whatever
    gitdir `.git` points at, so a gitdir that is not ours writes that answer itself. Measured,
    git 2.53: a copied gitdir whose `commondir` named the surveyed repository passed that
    question, and `git status` then ran the clean filter in the copy's own `config.worktree`.

    Resolved like `repo_common`, so the two compare. Measured, git 2.53: this answer already
    has `..` collapsed and symlinks resolved, so nothing reaching here has needed that. Of the
    two halves `path_answer` describes, this is the one only the KEPT code points reach — and
    it is where they were being lost, because it was the site still stripping.
    """
    return path_answer(run_git(path, "rev-parse", "--path-format=absolute", "--git-dir"))


def work_tree_root(path: Path) -> str | None:
    """The working tree git actually READS at `path`; None if it could not be read.

    Asked separately from `common_dir` because git answers it separately: `core.worktree`
    moves the working tree without moving the registration, so a worktree can belong to this
    repository and still have `git status` answer about a different directory (measured, git
    2.53). Comparing what git reports against the registered path costs one command and needs
    no list of the ways they can diverge; the test measures which forced values do and do not
    reach this one.

    This read carries a trailing code point whichever half of `path_answer`'s split it falls
    in — every member reaches it — which is why it was the first site fixed and why fixing it
    alone left the KEPT half losing its character at `git_dir`.
    """
    return path_answer(run_git(path, "rev-parse", "--path-format=absolute", "--show-toplevel"))


def owned_gitdir(gitdir: str, repo_common: str) -> bool:
    """Is `gitdir` one of the surveyed repository's PER-WORKTREE git directories?

    The trailing separator makes this a PATH-COMPONENT test: a bare string prefix also
    accepts a directory beside `worktrees` whose name merely starts with it.

    This says the gitdir is the repository's; it does not say whose, so it is only half of
    the identity check. It is also what makes the other half safe to perform: the file
    `recorded_worktree` reads lives inside the surveyed repository's own git directory.
    """
    under = os.path.join(repo_common, "worktrees") + os.sep
    return gitdir.startswith(under)


def recorded_worktree(gitdir: str) -> str | None:
    """The worktree path git RECORDS for `gitdir`, or None if it records none.

    `…/.git/worktrees/<name>/gitdir` holds the path of that worktree's `.git` file, and it is
    where `git worktree list` reads the registration's path from — so comparing the two asks
    whether the gitdir git READS at a registered path is the one recorded for it. A record
    that is relative to the gitdir is joined to it, and `os.path.normpath` collapses that join
    without resolving symlinks.

    The bytes are read and decoded here for the reason `run_git` records about `text=True`:
    text mode also TRANSLATES newlines, and `Path.read_text` takes only `encoding` and
    `errors` on 3.11, so it cannot be asked to decline. Measured, git 2.53 on 3.11: a worktree
    path holding a CARRIAGE RETURN came back with a line feed instead, matched no
    registration, and such a worktree read BROKEN. The decode matches `run_git`'s, so the two
    strings compared are decoded the same way. git's trailing newline is not stripped because
    it cannot reach the answer: the record ends in `/.git`, and taking the parent drops that
    whole component. A record that is EMPTY needs no guard either — it yields the gitdir's
    own path, which is not the registered one, so the same comparison refuses the row.

    The read is guarded for the reason `run_git` guards its own: worktrees here are created
    and removed by concurrent sessions, so a gitdir that answered a moment ago can be gone by
    the time this reads out of it, and an unreadable one is a refusal rather than a traceback.
    """
    try:
        raw = (Path(gitdir) / "gitdir").read_bytes()
    except OSError:
        return None
    recorded = raw.decode("utf-8", "surrogateescape")
    return os.path.normpath(str(Path(gitdir) / Path(recorded).parent))


def establish_identity(path: Path, repo_common: str) -> tuple[tuple[str, str] | None, str]:
    """The identity check, keeping what it verified: `((git directory, working tree), "")`
    when it holds, `(None, why not)` when it does not.

    Both are kept because the probe is handed them rather than asking git again at `path`.

    The gitdir git reads there must BE the one this repository records for THIS registration,
    not merely one of the repository's own: a `.git` file naming the common directory, or a
    sibling worktree's gitdir, is neither forged nor foreign, and git then reads that other
    registration's HEAD and its index while `worktree list` goes on reporting this entry's
    (each measured, git 2.53). Bounding the gitdir to the entry is what bounds the files git
    reads out of it, in place of naming them.

    The working tree git reports must then be `path` itself, which is asked separately
    because `core.worktree` moves it without moving the registration.

    Only a LINKED worktree can satisfy this: the main checkout reads the common directory,
    which is under no `worktrees/`. `classify_worktrees` excludes it before asking, and a
    test pins that order.

    Either read can also come back with NOTHING, which is not a mismatch and is not reported
    as one. Measured, git 2.53: `rev-parse --git-dir` exits 128 where `<path>/.git` exists but
    is not a gitfile git can follow, which `worktree list` does not call prunable; and
    `rev-parse --show-toplevel` exits 128 under `core.bare` in the worktree's own config while
    `--git-dir` still answers. BROKEN stays the verdict on both — a directory whose identity
    cannot be established is not inspected either way — and what changes is only what the row
    claims about why.
    """
    reads = git_dir(path)
    if reads is None:
        return None, (
            "git could not say which git directory it reads at this registered path, so it "
            "was not inspected"
        )
    if not owned_gitdir(reads, repo_common) or recorded_worktree(reads) != str(path):
        return None, (
            "the git directory git reads at this registered path is not the one this "
            "repository records for it and it was not inspected"
        )
    root = work_tree_root(path)
    if root is None:
        return None, (
            "git could not say which working tree it reads at this registered path, so it "
            "was not inspected"
        )
    if root != os.path.realpath(path):
        return None, (
            "the directory at this registered path is not the working tree git reads there "
            "(core.worktree) and was not inspected"
        )
    return (reads, root), ""


# --------------------------------------------------------------------------- live set


@dataclass(frozen=True)
class LiveSet:
    """The commits (and the branch names they came from) that belong to live work."""

    shas: frozenset[str]
    branches: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()


def story_id_token(story_id: str) -> str:
    """ "E29-S88" -> "e29-s88", the token the branch naming convention embeds."""
    return story_id.strip().lower()


def in_flight_selectors(chart: object) -> tuple[list[str], list[str]]:
    """Split a chart's in-flight rows into (explicit branch names, story-id tokens)."""
    branches: list[str] = []
    tokens: list[str] = []
    if not isinstance(chart, dict):
        return branches, tokens
    stories = chart.get("stories")
    if not isinstance(stories, list):
        return branches, tokens
    for row in stories:
        if not isinstance(row, dict) or row.get("status") != "in-flight":
            continue
        branch = row.get("branch")
        if isinstance(branch, str) and branch:
            branches.append(branch)
            continue
        story_id = row.get("id")
        if isinstance(story_id, str) and story_id:
            tokens.append(story_id_token(story_id))
    return branches, tokens


def ref_shas(repo: Path) -> tuple[dict[str, str], str | None]:
    """Every local and origin-remote ref with its commit, in ONE command, and any read problem.

    Returned as `(refs, problem)`. `problem` is None when `for-each-ref` succeeded — `refs` is
    then the real table, empty only if the repository genuinely has none. A NON-ZERO exit
    returns `({}, <reason>)` instead: an empty table that could not be read is NOT the same as a
    repository with no refs, and every live-set resolution runs through this table, so rendering
    the failure as an empty success drops every worktree tied only to a branch to DIRTY without
    a word about why. The reason is carried to `build_live_set`, which reports it as `not read`
    rather than as a zero count.

    Resolving each name with its own `rev-parse` cost up to three processes per name — about
    600 for 200 open PRs. One `for-each-ref` and a dict lookup replace them, and a name that
    starts with `-` never reaches an option parser at all.

    Both separators are chosen against what a REF NAME may contain, because a ref name is not
    this module's to control. `%00` is the field separator for that reason already. The record
    separator is `str.split("\\n")` and NOT `str.splitlines()`, which is the same defect one
    level up: `splitlines()` breaks on ten code points and git accepts three of them in a ref
    name. Measured, git 2.53: `check-ref-format` exits 1 and `update-ref` exits 128 for all
    seven ASCII separators — LF, VT, FF, CR, FS, GS, RS — and BOTH exit 0 for U+0085, U+2028
    and U+2029. Splitting a record on one of those three took `refs/heads/a<U+2028>b` apart
    into a fragment carrying no NUL, dropped for want of a SHA, and a remainder parsed as a
    whole record: the real ref vanished from the table, and a name of the writer's choosing
    entered it against a commit of the writer's choosing. A ref whose name ends
    `<U+2028>refs/heads/victim` put `refs/heads/victim` — which exists nowhere — in front of
    every caller that resolves a name. LF is the only separator git's own output uses and the
    only one it refuses to let into a name, so splitting on it alone is what makes a record
    boundary mean what git meant by one.
    """
    result = run_git(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
        "refs/heads",
        "refs/remotes/origin",
    )
    shas: dict[str, str] = {}
    for line in result.stdout.split("\n"):
        name, _, sha = line.partition("\0")
        if name and sha:
            shas[name] = sha
    problem = None if result.returncode == 0 else f"git for-each-ref {git_failure(result)}"
    return shas, problem


def local_branches(refs: dict[str, str]) -> list[str]:
    prefix = "refs/heads/"
    return [name[len(prefix) :] for name in refs if name.startswith(prefix)]


def branches_matching_tokens(branches: Iterable[str], tokens: Iterable[str]) -> list[str]:
    """Branch names carrying a story token, e.g. `claude/story-e29-s88-…` for `e29-s88`.

    The token must be bounded by non-alphanumerics: a bare substring test lets `e2` claim
    every `e29-…` branch, and a one-character id matches nearly every branch.
    """
    patterns = [re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])") for t in tokens if t]
    return sorted({b for b in branches for p in patterns if p.search(b.lower())})


def resolve_shas(
    repo: Path, names: Iterable[str], refs: dict[str, str] | None = None
) -> dict[str, str]:
    """Resolve names to commit SHAs: the local ref, then origin's, then the bare rev.

    The third candidate is the load-bearing one — it is what resolves a `--live-sha`
    abbreviation, which matches no ref name — and the only one that hands the value to git's
    own parser. These names arrive from `gh` (`headRefName`), from the chart's `branch` field
    and from `--live-branch`, so a leading-`-` value is reachable. `--end-of-options` keeps such
    a value out of the option parser. Without it a value written `--name=<value>`, such as
    `--abbrev-ref=strict`, can reach that parser and make git exit with a fatal error; the
    `^{commit}` suffix does not prevent that.
    `--` does not do this job: it marks what follows as a PATH,
    and rev-parse then fails to resolve even a real ref (all measured, git 2.53).
    """
    known = ref_shas(repo)[0] if refs is None else refs
    resolved: dict[str, str] = {}
    for name in names:
        for candidate in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if candidate in known:
                resolved[name] = known[candidate]
                break
        else:
            result = run_git(
                repo,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{name}^{{commit}}",
            )
            sha = result.stdout.strip()
            if result.returncode == 0 and sha:
                resolved[name] = sha
    return resolved


@dataclass(frozen=True)
class GhBranches:
    """`gh_open_pr_branches`' answer: the branches, whether the read happened, and the cap.

    `problem` is None when the read succeeded — `branches` is then the real answer, empty only
    if there genuinely are no open PRs. When the read could NOT be performed, `problem` names
    the reason and `branches` is empty but MEANINGLESS: a caller renders `not read (<problem>)`,
    never `0 found`. `truncated` says the `--limit` cap may have hidden further open PRs.
    """

    branches: tuple[str, ...] = ()
    problem: str | None = None
    truncated: bool = False


def gh_open_pr_branches(repo: Path, limit: int = 200) -> GhBranches:
    """Open-PR head branches via `gh`. NEVER raises; a failed read is REPORTED, not blanked.

    A read that could not be performed — gh missing, a timeout, a non-zero exit, or output that
    will not parse or is not the shape promised — returns a `problem`, never an empty list a
    caller cannot tell from "no open PRs". Both parse-raise sites are closed the way
    `read_chart` closes its own: `json.JSONDecodeError` alone is too narrow, because
    `json.loads` also raises RecursionError — not a ValueError — on a deeply nested payload, and
    that degraded to exit 2 in Python's wording rather than this module's, which is exactly what
    dropping git's stderr exists to avoid. The decode is done here rather than by `text=True`,
    for the reason `run_git` records: text mode also translates newlines, and `subprocess` will
    not give one without the other.

    The cap is made detectable by asking for limit+1: if that many rows come back, at least one
    more open PR exists than a limit-capped list would have shown, and `truncated` says so
    rather than the count silently topping out at the cap.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit + 1),
                "--json",
                "headRefName",
            ],
            cwd=str(repo),
            capture_output=True,
            env=scrubbed_env(),
            timeout=TIMEOUT_SECONDS,
        )
    except OSError:
        return GhBranches(problem="gh could not be started")
    except subprocess.TimeoutExpired:
        return GhBranches(problem=f"gh timed out after {TIMEOUT_SECONDS}s")
    if result.returncode != 0:
        return GhBranches(problem=f"gh exited {result.returncode}")
    try:
        rows = json.loads(result.stdout.decode("utf-8", "surrogateescape") or "[]")
    except (ValueError, RecursionError):
        return GhBranches(problem="gh output could not be parsed as JSON")
    if not isinstance(rows, list):
        return GhBranches(problem="gh output was not a JSON list")
    names = [r["headRefName"] for r in rows if isinstance(r, dict) and r.get("headRefName")]
    return GhBranches(branches=tuple(names), truncated=len(rows) > limit)


def read_chart(chart_path: Path) -> tuple[object | None, str]:
    """The chart's parsed contents and the note describing what happened reading it."""
    try:
        size = chart_path.stat().st_size
    except OSError as exc:
        return None, f"chart in-flight rows: unreadable ({exc.__class__.__name__})"
    if size > MAX_CHART_BYTES:
        return None, (
            f"chart in-flight rows: not read, {size} bytes is over the "
            f"{MAX_CHART_BYTES}-byte ceiling"
        )
    try:
        # ValueError is both JSONDecodeError and the UnicodeDecodeError of a non-UTF-8 file.
        return json.loads(chart_path.read_text()), ""
    except (OSError, ValueError, RecursionError) as exc:
        return None, f"chart in-flight rows: unreadable ({exc.__class__.__name__})"


def build_live_set(
    repo: Path,
    *,
    use_gh: bool = True,
    chart_path: Path | None = None,
    live_branches: Sequence[str] = (),
    live_shas: Sequence[str] = (),
    refs: dict[str, str] | None = None,
    refs_problem: str | None = None,
) -> LiveSet:
    """Union of the live-set sources. Each source's contribution is reported, not assumed.

    A source whose read could not be performed is reported as `not read (<reason>)`, never as a
    zero count that reads like a genuine empty: `refs_problem` carries a failed `for-each-ref`
    (the table every resolution runs through), a failed `gh` read carries its own reason, and
    the `gh` cap is disclosed when more open PRs exist than were read. `main` reads the ref
    table once and passes it here with its problem; a `refs=None` call reads its own.
    """
    if refs is None:
        known, refs_problem = ref_shas(repo)
    else:
        known = refs
    shas: set[str] = set()
    branches: set[str] = set()
    notes: list[str] = []

    if refs_problem:
        notes.append(
            f"local and origin refs: not read ({refs_problem}); the resolutions below fell back "
            "to per-name lookups and may be incomplete"
        )

    if use_gh:
        gh = gh_open_pr_branches(repo)
        if gh.problem is not None:
            notes.append(f"open-PR branches (gh): not read ({gh.problem})")
        else:
            resolved = resolve_shas(repo, gh.branches, known)
            shas.update(resolved.values())
            branches.update(resolved)
            more = "; more open PRs exist than were read (limit cap hit)" if gh.truncated else ""
            notes.append(
                f"open-PR branches (gh): {len(gh.branches)} found, "
                f"{len(resolved)} resolved to a commit{more}"
            )
    else:
        notes.append("open-PR branches (gh): skipped (--no-gh)")

    if chart_path is not None and chart_path.is_file():
        chart, problem = read_chart(chart_path)
        if problem:
            notes.append(problem)
        else:
            explicit, tokens = in_flight_selectors(chart)
            matched = branches_matching_tokens(local_branches(known), tokens)
            resolved = resolve_shas(repo, [*explicit, *matched], known)
            shas.update(resolved.values())
            branches.update(resolved)
            notes.append(
                f"chart in-flight rows: {len(explicit) + len(tokens)} rows -> "
                f"{len(resolved)} branches resolved"
            )
    else:
        notes.append("chart in-flight rows: no chart file read")

    if live_branches:
        resolved = resolve_shas(repo, live_branches, known)
        shas.update(resolved.values())
        branches.update(resolved)
        notes.append(f"--live-branch: {len(resolved)}/{len(live_branches)} resolved")
    if live_shas:
        # Rev-parsed like --live-branch: an abbreviation is never string-equal to a full HEAD,
        # so an unresolved one is kept as given AND named, never silently dropped.
        resolved = resolve_shas(repo, live_shas, known)
        unresolved = [sha for sha in live_shas if sha not in resolved]
        shas.update(resolved.values())
        shas.update(unresolved)
        notes.append(
            f"--live-sha: {len(resolved)}/{len(live_shas)} resolved to a commit"
            + (f"; kept as given, unresolved: {', '.join(unresolved)}" if unresolved else "")
        )

    return LiveSet(shas=frozenset(shas), branches=frozenset(branches), notes=tuple(notes))


# --------------------------------------------------------------------------- reachability


def commits_off_main(repo: Path, rev: str, main_ref: str) -> frozenset[str] | None:
    """Commits reachable from `rev` but not from `main_ref`; None when git could not answer.

    `git cherry` was the obvious tool and is the wrong one: it emits no MERGE commit, so a
    worktree whose only unshared work is a hand-resolved merge reported nothing at all
    (measured, git 2.53 — `cherry` empty, this call returns the merge). The cost of the swap is
    that patch-equivalence is no longer detected, so a commit whose patch already landed on
    `main_ref` in a different shape still counts as unshared.
    """
    result = run_git(repo, "rev-list", "--end-of-options", rev, f"^{main_ref}")
    if result.returncode != 0:
        return None
    return frozenset(result.stdout.split())


def live_only_commits(repo: Path, live_shas: Iterable[str], main_ref: str) -> frozenset[str]:
    """The live set's own commits, minus everything already on `main_ref`.

    Subtracting `main_ref` is what keeps the reachability test from swallowing the repository:
    every worktree sitting at an old commit is an ancestor of every live branch tip, so
    ancestry alone would call all of them LIVE. One rev-list per live SHA, so a SHA git cannot
    resolve costs only itself.
    """
    found: set[str] = set()
    for sha in live_shas:
        reachable = commits_off_main(repo, sha, main_ref)
        if reachable is not None:
            found.update(reachable)
    return frozenset(found)


# --------------------------------------------------------------------------- work probe


@dataclass(frozen=True)
class WorkState:
    """What a worktree is carrying that is not shared with the rest of the repository."""

    status_lines: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()
    gitlinks: tuple[str, ...] = ()
    off_main: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def index_survey(
    where: Path, pin: tuple[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], list[str]]:
    """Two things `git status` will not report, read from the index, and any read problem.

    Returned as `(unchecked, gitlinks, problems)`:

    * `unchecked` — tracked paths carrying the `assume-unchanged` or `skip-worktree` index
      bit. `git status` says NOTHING about such a path, modified or not, and no status flag
      overrides that (measured, git 2.53); its silence proves nothing, so they are counted as
      work whose state is UNKNOWN, not as paths known to be modified. Kept in the `<tag> <path>`
      shape git's `-v` prints, so the count and the row read the same.
    * `gitlinks` — the paths of any in-tree gitlink (a `160000` index entry). `git status` is
      told NOT to descend into one (`--ignore-submodules=dirty` in the probe), because a
      gitlink's `.git` directory sits inside the worktree and supplies its OWN config from
      there — a `filter.*.clean` in it EXECUTES during a descending `git status` (measured, git
      2.53), and the identity check does not bound it. Not descending is what stops that; the
      gitlink is then counted here in its own right.
      Its recorded commit and internal state are not judged.

    `ls-files --stage -v` carries both the tag (`-v`) and the mode (`--stage`, `160000` for a
    gitlink) in one read; it does not descend into a gitlink or run any filter (measured, git
    2.53: a gitlink's clean filter did not run during it).

    Records are split on LF and NOT with `str.splitlines()`, which is `ref_shas`'s defect over
    a PATH instead of a ref name. Measured, git 2.53 with `core.quotePath=false` — a setting
    the repository's own config or a worktree's `config.worktree` can carry — a CLEAN tracked
    file named `a<U+2028>b.txt` prints here as `H 100644 <sha> 0<TAB>a<U+2028>b.txt`, and
    splitting that record on the code point left a fragment counted by the test below: a
    worktree with no index bit set anywhere, and an empty `git status`, reported a path git was
    told not to check. Splitting on LF alone cannot be fooled that way. git C-quotes all seven
    ASCII separators in this output under `core.quotePath` true AND false, and passes U+0085,
    U+2028 and U+2029 through raw only under false (each measured, git 2.53), so LF is the only
    one of the ten that reaches a record boundary here and it is the boundary git meant. The
    stage columns hold no TAB, so the FIRST TAB is git's field separator and never part of the
    path (a TAB in a path is always C-quoted, measured git 2.53), which is why the path is taken
    after it.
    """
    listed = run_git(where, "ls-files", "--stage", "-v", pin=pin)
    if listed.returncode != 0:
        return (), (), [f"could not list this worktree's index ({git_failure(listed)})"]
    unchecked: list[str] = []
    gitlinks: list[str] = []
    for line in listed.stdout.split("\n"):
        if not line:
            continue
        tag = line[:1]
        meta, _, path = line[2:].partition("\t")
        mode = meta.partition(" ")[0]
        if tag.islower() or tag == "S":
            unchecked.append(f"{tag} {path}")
        if mode == "160000":
            gitlinks.append(path)
    return tuple(unchecked), tuple(gitlinks), []


def probe_work_state(repo: Path, entry: Worktree, main_ref: str, pin: tuple[str, str]) -> WorkState:
    """Read what `entry` is carrying: working-tree changes, and commits not on `main_ref`.

    `pin` is the git directory and working tree `establish_identity` verified for `entry`, and
    every read of the working tree here is pinned to them.
    """
    problems: list[str] = []
    # Run from the verified working tree's root: `ls-files` lists only what is below its cwd.
    where = Path(pin[1])

    off_main: tuple[str, ...] = ()
    if entry.head is not None:
        reachable = commits_off_main(repo, entry.head, main_ref)
        if reachable is None:
            problems.append(COMMITS_UNLISTED)
        else:
            off_main = tuple(sorted(reachable))

    # Two silencers overridden on the command line, and a third case handled by REFUSING to
    # descend. `-uall` overrides an ambient `status.showUntrackedFiles=no`, under which a
    # worktree full of untracked unsaved work reads as clean; `--ignored=matching` is what then
    # also shows the untracked work an IGNORE RULE hides, collapsed to one line per matched
    # directory so a build directory costs one row rather than thousands.
    #
    # `--ignore-submodules=dirty` — NOT `=none`. `=none` descends into a nested gitlink to
    # report its dirtiness, and that descent EXECUTES a `filter.*.clean` in the gitlink's own
    # config, which lives inside the attacker-writable worktree and the identity check does not
    # bound (measured, git 2.53: the filter ran and the row still printed an innocuous DIRTY).
    # `=dirty` does not descend, so that filter does not run, and it itemises a gitlink whose HEAD
    # has moved off the recorded commit, which `=all` does not.
    # The cost is that a submodule's INTERNAL diff
    # is no longer itemised — which is the
    # right trade against running its config.
    status = run_git(
        where,
        "status",
        "--porcelain",
        "-uall",
        "--ignored=matching",
        "--ignore-submodules=dirty",
        pin=pin,
    )
    status_lines: list[str] = []
    ignored: list[str] = []
    if status.returncode != 0:
        problems.append(f"git status failed ({git_failure(status)})")
    else:
        # Split on LF and not with `str.splitlines()`, for the reason `index_survey` gives.
        # Measured, git 2.53 with `core.quotePath=false`: an untracked file named
        # `a<U+2028>b.txt` prints as ONE record here, and `splitlines()` made two of it — a row
        # reporting two modified/untracked paths where git had named one.
        for line in status.stdout.split("\n"):
            if not line.strip():
                continue
            (ignored if line.startswith("!!") else status_lines).append(line)

    unchecked, gitlinks, more = index_survey(where, pin)
    problems.extend(more)

    return WorkState(
        status_lines=tuple(status_lines),
        ignored=tuple(ignored),
        unchecked=unchecked,
        gitlinks=gitlinks,
        off_main=off_main,
        problems=tuple(problems),
    )


# --------------------------------------------------------------------------- classify


@dataclass(frozen=True)
class Classified:
    worktree: Worktree
    status: str
    reason: str
    work: WorkState | None = None


def classify_worktrees(
    repo: Path,
    entries: Sequence[Worktree],
    *,
    main_path: Path | None,
    live: LiveSet,
    live_only: frozenset[str],
    main_ref: str = "development",
) -> list[Classified]:
    """Classify each registered worktree. The live test is on COMMITS, not on the name."""
    repo_common = common_dir(repo)
    if repo_common is None:
        raise RuntimeError(f"could not read the git common directory of {repo}")

    out: list[Classified] = []
    for entry in entries:
        if main_path is not None and entry.path == main_path:
            out.append(Classified(entry, MAIN, "main checkout — excluded"))
            continue
        if not entry.path.is_dir():
            gone = f" ({entry.prunable_reason})" if entry.prunable_reason else ""
            out.append(Classified(entry, MISSING, f"directory is gone{gone}"))
            continue
        if entry.prunable:
            out.append(
                Classified(
                    entry,
                    BROKEN,
                    f"registration is broken ({entry.prunable_reason}) but the directory is "
                    "still on disk and was not inspected; `git worktree prune` would "
                    "deregister it",
                )
            )
            continue
        if entry.locked:
            reason = entry.lock_reason or "no reason given"
            out.append(Classified(entry, LIVE, f"locked by git: {reason}"))
            continue
        if entry.head in live.shas:
            out.append(Classified(entry, LIVE, "HEAD commit is in the live set"))
            continue
        verified, problem = establish_identity(entry.path, repo_common)
        if verified is None:
            if not entry.path.is_dir():
                out.append(Classified(entry, MISSING, "directory is gone"))
            else:
                out.append(Classified(entry, BROKEN, problem))
            continue
        work = probe_work_state(repo, entry, main_ref, verified)
        # Keyed on the directory, not on how the reads failed.
        if work.problems and not entry.path.is_dir():
            carried = f", carries {len(work.off_main)} commit(s) not on the main ref"
            unlisted = f", {COMMITS_UNLISTED}" if COMMITS_UNLISTED in work.problems else ""
            gone = "directory is gone" + (carried if work.off_main else unlisted)
            out.append(Classified(entry, MISSING, gone))
            continue
        if live_only.intersection(work.off_main):
            out.append(
                Classified(entry, LIVE, "shares a commit off the main ref with the live set", work)
            )
            continue
        # Every linked worktree that gets this far is DIRTY, whether the reads found anything
        # or not. There is no arm below this one, so there is no verdict that can be falsely
        # empty: `describe_work` names what the reads DID find, and where they found nothing
        # it reports the live-set search that placed this row here and claims nothing about
        # the contents.
        out.append(Classified(entry, DIRTY, describe_work(work), work))
    return out


def describe_work(work: WorkState) -> str:
    """What this run FOUND in a worktree — never what it did not find there.

    The reads are counted, not summarised: each count is of what a read RETURNED. Where they
    returned nothing there is no second sentence to write, because the one an emptiness claim
    would need is the one this tool may not make — git's silence is decided by configuration
    and by the stat cache, so it is not evidence about the working tree. The fallback
    therefore reports the LIVE-SET search, which is a search this run really did run and
    really did come back from empty-handed, and it says outright that the contents are not
    judged here so that no operator reads the row as a clearance.
    """
    bits: list[str] = []
    if work.status_lines:
        bits.append(f"{len(work.status_lines)} modified/untracked path(s)")
    if work.ignored:
        bits.append(f"{len(work.ignored)} ignored path(s)")
    if work.unchecked:
        bits.append(f"{len(work.unchecked)} path(s) git was told not to check")
    if work.gitlinks:
        bits.append(f"{len(work.gitlinks)} in-tree gitlink(s) not descended into")
    if work.off_main:
        bits.append(f"{len(work.off_main)} commit(s) not on the main ref")
    bits.extend(work.problems)
    if not bits:
        return UNLINKED_REASON
    return "carries " + ", ".join(bits)


# --------------------------------------------------------------------------- report


def one_line(text: str) -> str:
    """Escape every non-printable character, so a field can never span or forge a line.

    A lock reason is free text set by `git worktree lock --reason`, and a worktree path is a
    directory name; either can carry a newline, and printed raw it renders as a complete,
    indistinguishable extra row for a path that is not registered (measured, git 2.53).
    """
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def render_report(rows: Sequence[Classified], live: LiveSet, live_only: frozenset[str]) -> str:
    lines = [
        "Registered worktrees, as of this run. A point-in-time reading of what is registered",
        "now; it measures no trend and makes no claim about any other moment.",
        "",
        "Live set:",
    ]
    lines.extend(f"  - {one_line(note)}" for note in live.notes)
    lines.append(f"  - {len(live.shas)} distinct live commit(s)")
    lines.append(f"  - {len(live_only)} live commit(s) not on the main ref")
    lines.extend(f"  - live branch: {one_line(name)}" for name in sorted(live.branches))
    lines.append("")
    width = max((len(r.status) for r in rows), default=7)
    for row in rows:
        branch = one_line(short_branch(row.worktree.branch) or "(detached)")
        head = one_line(row.worktree.head or "")[:12]
        lines.append(f"{row.status:<{width}}  {one_line(str(row.worktree.path))}")
        lines.append(f"{'':<{width}}  branch={branch} head={head}")
        lines.append(f"{'':<{width}}  {one_line(row.reason)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e32_s01_worktree_sweep",
        description=(
            "Classify the git worktrees registered in this repository as "
            "MAIN / LIVE / DIRTY / BROKEN / MISSING. It removes nothing, and it does not say "
            "a worktree is empty or safe to remove."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", type=Path, default=REPO, help="repository to inspect")
    parser.add_argument(
        "--main-ref", default="development", help="ref that commits are compared against"
    )
    parser.add_argument("--no-gh", action="store_true", help="do not consult `gh pr list`")
    parser.add_argument(
        "--chart",
        type=Path,
        default=None,
        help=(
            "open_stories.json to read in-flight rows from (default: the MAIN checkout's copy, "
            "which is where the gitignored chart exists; a worktree has none)"
        ),
    )
    parser.add_argument(
        "--live-branch", action="append", default=[], help="branch to treat as live (repeatable)"
    )
    parser.add_argument(
        "--live-sha", action="append", default=[], help="commit to treat as live (repeatable)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # `os.path.realpath` for the reason given at `common_dir`: `Path.resolve` raises on a
    # symlink cycle on 3.11, and this line sits outside the handler below, so it escaped as a
    # traceback where a `--repo` that is merely not a repository already returned exit 2.
    repo = Path(os.path.realpath(args.repo))

    try:
        entries = list_worktrees(repo)
        main_path = main_worktree_path(entries)
        refs, refs_problem = ref_shas(repo)

        # git's own resolution first, so `HEAD` is this checkout's; the ref table second.
        main_sha = resolve_shas(repo, [args.main_ref], {}).get(args.main_ref)
        if main_sha is None:
            main_sha = resolve_shas(repo, [args.main_ref], refs).get(args.main_ref)
        if main_sha is None:
            # Every commit test compares against this ref. Unresolvable, rev-list fails for
            # every worktree, which reads as a repository full of work rather than as a bad
            # argument.
            print(f"--main-ref {args.main_ref!r} does not resolve in {repo}", file=sys.stderr)
            return 2
        # The commit it resolved to is what every commit test is given, never the name: the name
        # may resolve only through `refs/remotes/origin/`, which git cannot do from the bare name.

        # The chart is gitignored, so it exists only at the main checkout: defaulting to
        # `repo` loses every in-flight row whenever the tool is run from a worktree.
        chart = args.chart if args.chart is not None else (main_path or repo) / "open_stories.json"
        live = build_live_set(
            repo,
            use_gh=not args.no_gh,
            chart_path=chart,
            live_branches=args.live_branch,
            live_shas=args.live_sha,
            refs=refs,
            refs_problem=refs_problem,
        )
        live_only = live_only_commits(repo, live.shas, main_sha)
        rows = classify_worktrees(
            repo,
            entries,
            main_path=main_path,
            live=live,
            live_only=live_only,
            main_ref=main_sha,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(render_report(rows, live, live_only))
    print("\nReport only. Nothing was removed or deregistered.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
