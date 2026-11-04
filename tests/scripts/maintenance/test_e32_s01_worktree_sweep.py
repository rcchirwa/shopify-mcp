"""Offline tests for the E32-S01 worktree report.

The ``gh`` invocation
is stubbed, and it is not git. The tests that read the module's own syntax tree instead need
no repository at all — how many there are is not written here, because a number in prose goes
stale silently; ``test_the_syntax_tree_scans_need_no_repository`` computes which functions
those are and asserts the property that matters about them.

Ported from Holocron, where coverage did not measure ``scripts/``. In shopify-mcp,
``[tool.coverage.run] source`` includes ``scripts``, so this module is under the 100% gate.

No live-DB paths here, so pytest.importorskip("neo4j") is not needed. If live-DB tests are
ever added to this file they MUST start with::

    neo4j = pytest.importorskip("neo4j")
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.maintenance import e32_s01_worktree_sweep as sweep
from scripts.maintenance.e32_s01_worktree_sweep import (
    BROKEN,
    DIRTY,
    LIVE,
    MAIN,
    MISSING,
    GhBranches,
    LiveSet,
    branches_matching_tokens,
    build_arg_parser,
    build_live_set,
    classify_worktrees,
    commits_off_main,
    establish_identity,
    gh_open_pr_branches,
    git_env,
    in_flight_selectors,
    list_worktrees,
    live_only_commits,
    main,
    main_worktree_path,
    parse_worktree_porcelain,
    render_report,
    resolve_shas,
    run_git,
    short_branch,
)

LIVE_BRANCH = "claude/story-e99-s01"
GENERIC_BRANCH = "worktree-agent-a5908ec3377dc3e74"

#: The reason a DIRTY row carries when the reads named nothing — the arm the PRUNABLE verdict
#: used to occupy. Bound through the module rather than imported so it can be compared BOTH
#: ways: `== UNLINKED_REASON` is what every "the reads found nothing" test asserts, and
#: `!= UNLINKED_REASON` is what gives an anti-silencer test its kill power now that DIRTY is
#: the arm a silenced read falls into as well as the arm a seen edit lands in.
UNLINKED_REASON = sweep.UNLINKED_REASON

#: Every word an operator could act on as a clearance. A row's reason is checked against these
#: because the whole narrowing is that no row may claim a worktree is empty or safe to remove.
CLEARANCE_WORDS = ("clean", "empty", "nothing", "no work", "prunable", "safe", "remove")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def git_may_fail(cwd: Path, *args: str) -> None:
    """For the merges below, which are MEANT to stop on a conflict for the caller to resolve."""
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def init_repo(path: Path, *init_args: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main", *init_args, ".")
    # The sweep's default `--main-ref` is `development`; here it names `main`.
    git(path, "symbolic-ref", "refs/heads/development", "refs/heads/main")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "commit.gpgsign", "false")
    return path


def flat(text: str | None) -> str:
    """Prose with its line wrapping removed, so a pin cannot be broken by a re-wrap."""
    return " ".join((text or "").split())


@dataclass
class Fixture:
    """A throwaway repo with the four worktree shapes the classifier has to tell apart."""

    root: Path
    repo: Path
    named: Path
    detached: Path
    generic: Path
    stale: Path
    live_sha: str

    def live(self) -> LiveSet:
        return LiveSet(shas=frozenset({self.live_sha}), branches=frozenset({LIVE_BRANCH}))

    def classify(self, live: LiveSet | None = None) -> list[sweep.Classified]:
        live = self.live() if live is None else live
        entries = list_worktrees(self.repo)
        return classify_worktrees(
            self.repo,
            entries,
            main_path=main_worktree_path(entries),
            live=live,
            live_only=live_only_commits(self.repo, live.shas, "main"),
        )

    def rows(self, live: LiveSet | None = None) -> dict[str, str]:
        return {row.worktree.path.name: row.status for row in self.classify(live)}

    def row(self, name: str, live: LiveSet | None = None) -> sweep.Classified:
        return {r.worktree.path.name: r for r in self.classify(live)}[name]

    def registrations(self) -> set[Path]:
        return {entry.path for entry in list_worktrees(self.repo)}


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    root = tmp_path / "fx"
    repo = init_repo(root / "repo")
    (repo / "f.txt").write_text("base\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-qm", "base")

    # (i) a worktree on a NAMED story branch, carrying a commit of live work
    named = root / "wt-named"
    git(repo, "branch", LIVE_BRANCH)
    git(repo, "worktree", "add", "-q", str(named), LIVE_BRANCH)
    (named / "w.txt").write_text("live work\n")
    git(named, "add", "w.txt")
    git(named, "commit", "-qm", "E99-S01 work")
    live_sha = git(repo, "rev-parse", LIVE_BRANCH)

    # (ii) a SECOND worktree at the SAME commit, on a DETACHED HEAD (no branch line at all)
    detached = root / "wf_9f88823c-c52-1"
    git(repo, "worktree", "add", "-q", "--detach", str(detached), live_sha)

    # (iii) a third on a GENERIC-SOUNDING branch name that actually holds live work
    generic = root / GENERIC_BRANCH
    git(repo, "branch", GENERIC_BRANCH, live_sha)
    git(repo, "worktree", "add", "-q", str(generic), GENERIC_BRANCH)

    # (iv) a genuinely stale worktree: detached at a commit already on main, nothing carried
    stale = root / "frosty-faraday-656971"
    git(repo, "worktree", "add", "-q", "--detach", str(stale), "main")

    return Fixture(
        root=root,
        repo=repo,
        named=named,
        detached=detached,
        generic=generic,
        stale=stale,
        live_sha=live_sha,
    )


def name_based_live(entry: sweep.Worktree, live: LiveSet) -> bool:
    """The WRONG filter, kept in the test file so the script never ships it.

    This is the obvious implementation: a worktree is live if its branch name is one of the
    live branches. It is what the story exists to disprove.
    """
    return short_branch(entry.branch) in live.branches


def assert_reads_named_nothing(fixture: Fixture, name: str, note: str = "") -> None:
    """Pin the row a worktree gets when every read came back empty — the old PRUNABLE arm.

    Asserted through the REASON and never through the status alone: `status == DIRTY` would
    hold just as well over a silenced read, an injected change or a sibling's state read by
    mistake, which is precisely what the tests calling this are here to catch.
    """
    row = fixture.row(name)
    assert row.status == DIRTY, f"{note} {row.status}: {row.reason}"
    assert row.reason == UNLINKED_REASON, f"{note} {row.reason}"


def assert_reads_found(fixture: Fixture, name: str, expected: str, note: str = "") -> None:
    """The counterpart: the reads NAMED something, and named this. See the docstring above."""
    row = fixture.row(name)
    assert row.status == DIRTY, f"{note} {row.status}: {row.reason}"
    assert row.reason == expected, f"{note} {row.reason}"


def sweep_argv(fixture: Fixture, *extra: str) -> list[str]:
    return ["--repo", str(fixture.repo), "--no-gh", "--live-sha", fixture.live_sha, *extra]


def break_registration(worktree: Path) -> None:
    """The crashed-session shape: the directory survives, its `.git` link does not."""
    (worktree / ".git").unlink()


def race_after_identity(
    monkeypatch: pytest.MonkeyPatch, target: Path, act: Callable[[], object]
) -> None:
    """Run `act` once, the moment the identity check's LAST git read of `target` returns.

    The REAL read runs and answers for the directory as it was,
    and the change lands after it returns.
    """
    real = sweep.work_tree_root
    fired: list[Path] = []

    def racing(path: Path) -> str | None:
        answer = real(path)
        if path == target and not fired:
            fired.append(path)
            act()
        return answer

    monkeypatch.setattr(sweep, "work_tree_root", racing)


def refusal(path: Path, repo_common: str) -> str:
    """The reason `establish_identity` gives for `path`, asserting it returned no pin."""
    verified, why = establish_identity(path, repo_common)
    assert verified is None, verified
    return why


def forge_gitdir(worktree: Path, forged: Path, repo: Path) -> Path:
    """Repoint `worktree`'s `.git` at a gitdir outside the repository that CLAIMS to be in it.

    `commondir` is a plain file naming the repository a gitdir belongs to, and for a linked
    gitdir it is what `rev-parse --git-common-dir` answers from — so whoever writes the
    directory sitting at a registered path writes that answer too. Copying a real gitdir is
    what leaves the rest of it working, so git reads the forgery rather than failing on it.
    """
    legit = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
    shutil.copytree(legit, forged)
    (forged / "commondir").write_text(f"{repo / '.git'}\n")
    (forged / "gitdir").write_text(f"{worktree / '.git'}\n")
    (worktree / ".git").write_text(f"gitdir: {forged}\n")
    return forged


def plant_in_tree_gitlink(worktree: Path, marker: Path) -> str:
    """Plant `sub/` inside `worktree`: its OWN git repo whose `.git` is a DIRECTORY, a
    `filter.pwn.clean` in its own config, a SAME-SIZE modified tracked file that forces a
    re-hash (git cannot short-circuit on size), and the gitlink registered as a `160000` entry
    in the worktree's index. This is the BLOCKER-1 shape: a descending `git status` executes the
    filter from config the identity check does not bound. Returns the gitlink sha; nothing has
    fired on return.
    """
    sub = worktree / "sub"
    init_repo(sub)
    (sub / ".gitattributes").write_text("s.txt filter=pwn\n")
    (sub / "s.txt").write_text("committed\n")  # 10 bytes
    git(sub, "add", ".gitattributes", "s.txt")
    git(sub, "commit", "-qm", "sub base")
    git(sub, "config", "filter.pwn.clean", f"touch '{marker}'; cat")
    target = sub / "s.txt"
    aged = target.stat()
    target.write_text("MODIFIEDx\n")  # also 10 bytes, so the size compare cannot short-circuit
    os.utime(target, None)  # mtime moves → git must re-hash → the clean filter runs
    assert target.stat().st_size == aged.st_size, "same size is what forces the re-hash"
    sub_sha = git(sub, "rev-parse", "HEAD")
    git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{sub_sha},sub")
    assert not marker.exists(), "nothing has fired during the fixture's own setup"
    return sub_sha


# --------------------------------------------------------------------- porcelain shape


def test_detached_entry_carries_no_branch_and_does_not_inherit_the_previous_one(
    fixture: Fixture,
) -> None:
    by_path = {e.path.name: e for e in list_worktrees(fixture.repo)}

    detached = by_path[fixture.detached.name]
    assert detached.detached is True
    assert detached.branch is None, "a detached entry emits no `branch` line; none may be invented"
    assert detached.head == fixture.live_sha

    # The mis-attribution this guards against is only visible against neighbours that DO
    # have branches, so pin every entry's branch at once.
    assert {name: short_branch(e.branch) for name, e in by_path.items()} == {
        "repo": "main",
        fixture.named.name: LIVE_BRANCH,
        fixture.detached.name: None,
        fixture.generic.name: GENERIC_BRANCH,
        fixture.stale.name: None,
    }


def test_git_lists_the_main_checkout_first(fixture: Fixture) -> None:
    entries = list_worktrees(fixture.repo)
    assert main_worktree_path(entries) == fixture.repo


def test_a_newline_in_a_worktree_path_cannot_inject_a_record(fixture: Fixture) -> None:
    """Plain `--porcelain` does not quote paths, so a newline forges a whole extra entry.

    Measured, git 2.53: line-splitting the plain form yields a third record
    `worktree /tmp/INJECTED` that no worktree corresponds to. The `-z` form emits the same
    path inside ONE NUL-delimited field, so the forged text cannot become a record.
    """
    evil = (
        fixture.root
        / "evil\nworktree /tmp/INJECTED\nHEAD 0000000000000000000000000000000000000000\nx"
    )
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(evil), "main")

    paths = [entry.path for entry in list_worktrees(fixture.repo)]

    assert evil in paths, "the real worktree, newlines and all, is one entry"
    assert Path("/tmp/INJECTED") not in paths
    assert len(paths) == 5 + 1, paths


def test_a_carriage_return_in_a_worktree_path_is_not_translated_away(fixture: Fixture) -> None:
    """`text=True` TRANSLATES newlines as well as decoding it, so a CR arrived here as an LF.

    `-z` stops a newline forging a record, which is what the sibling test above pins. It does
    nothing about a path that is REWRITTEN before the parse ever sees it: `subprocess` cannot be
    asked for text mode without universal-newline translation, so the bytes are decoded here
    instead. Measured, git 2.53: git's own stdout carries the CR and `run_git`'s did not, and
    the worktree then classified MISSING while it sat on disk holding an untracked file.
    """
    carriage = fixture.root / "wt-with\rCR"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(carriage), "main")
    (carriage / "ONLY-COPY.txt").write_text("TOKEN=hunter2\n")

    raw = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=str(fixture.repo),
        capture_output=True,
    ).stdout
    assert b"\r" in raw, "git really emits the carriage return, so the guard is not vacuous"
    assert carriage.is_dir(), "and the directory really is there to be found"

    assert carriage in [entry.path for entry in list_worktrees(fixture.repo)]

    row = {r.worktree.path: r for r in fixture.classify()}[carriage]
    assert row.status == DIRTY, f"{row.status}: {row.reason}"
    # What this test is about is that the file WAS seen.
    assert "1 modified/untracked path(s)" in row.reason


def test_a_carriage_return_path_is_not_read_through_its_newline_named_twin(
    fixture: Fixture,
) -> None:
    """Two DISTINCT worktrees collapsed onto one path, and the tool probed the wrong one.

    With the translation in place both entries rendered the same string, so the dirty
    worktree's row was written off the clean twin's state while its only copy of a file was
    still on disk. That is the misclassification this story exists to prevent, so the two
    paths must stay distinct and the dirty one must answer for itself.
    """
    carriage = fixture.root / "twin\rX"
    newline = fixture.root / "twin\nX"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(carriage), "main")
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(newline), "main")
    (carriage / "ONLY-COPY.txt").write_text("TOKEN=hunter2\n")

    rows = {r.worktree.path: r for r in fixture.classify()}

    assert carriage in rows and newline in rows, sorted(map(str, rows))
    # Both rows are DIRTY, so only the REASONS tell the collapse from the correct reading:
    # the file must be counted on the row of the worktree that holds it, and on no other.
    assert "1 modified/untracked path(s)" in rows[carriage].reason, rows[carriage].reason
    assert rows[newline].reason == UNLINKED_REASON, rows[newline].reason


def test_a_record_without_a_worktree_attribute_is_skipped_not_crashed_on() -> None:
    """A record whose first attribute is not `worktree` used to raise KeyError out of the parse."""
    text = "HEAD 1111111111111111111111111111111111111111\0detached\0\0worktree /a\0HEAD 2\0\0"

    entries = parse_worktree_porcelain(text)

    assert [str(e.path) for e in entries] == ["/a"]


# --------------------------------------------------------------------- AC1: classification


def test_sha_test_marks_the_detached_and_generic_worktrees_live(fixture: Fixture) -> None:
    rows = fixture.rows()
    assert rows[fixture.detached.name] == LIVE
    assert rows[fixture.generic.name] == LIVE
    assert rows[fixture.named.name] == LIVE


def test_a_name_based_filter_misclassifies_the_same_two_worktrees(fixture: Fixture) -> None:
    live = fixture.live()
    verdicts = {e.path.name: name_based_live(e, live) for e in list_worktrees(fixture.repo)}
    sha_verdicts = fixture.rows()

    # Same fixture, same run: what the SHA test calls LIVE, the name filter calls unrelated.
    for name in (fixture.detached.name, fixture.generic.name):
        assert sha_verdicts[name] == LIVE
        assert verdicts[name] is False
    assert verdicts[fixture.named.name] is True


def test_a_worktree_at_a_live_commit_already_on_main_is_live_by_sha_alone(
    fixture: Fixture,
) -> None:
    """SHA equality carries its own weight; reachability cannot stand in for it here.

    A branch cut from `main` and not yet committed to has NO commit off `main`, so the
    reachability half has nothing to intersect and only equality with the live SHA sees this
    worktree. A name filter cannot: it is detached, so it has no name to match. Without this
    case the whole live test could be branch-name matching and the suite would not notice,
    because reachability rescues every other worktree in the fixture.
    """
    live = LiveSet(
        shas=frozenset({git(fixture.repo, "rev-parse", "main")}), branches=frozenset({"main"})
    )
    assert live_only_commits(fixture.repo, live.shas, "main") == frozenset()

    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[fixture.stale.name]
    assert fixture.rows(live)[fixture.stale.name] == LIVE
    assert name_based_live(entry, live) is False


def test_a_genuinely_stale_worktree_is_not_live(fixture: Fixture) -> None:
    """Without this the SHA test could pass by calling everything LIVE."""
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == UNLINKED_REASON
    assert not any(word in row.reason.lower() for word in CLEARANCE_WORDS), (
        "the crashed-session residue this tool is filed to find is still reported, and the row "
        "reporting it still does not clear it"
    )


def test_the_main_checkout_is_classified_main(fixture: Fixture) -> None:
    assert fixture.rows()["repo"] == MAIN


def test_a_locked_worktree_is_live_even_at_an_unremarkable_commit(fixture: Fixture) -> None:
    git(
        fixture.repo,
        "worktree",
        "lock",
        "--reason",
        "claude agent wf_x (pid 1)",
        str(fixture.stale),
    )
    assert fixture.rows()[fixture.stale.name] == LIVE


def test_a_worktree_whose_directory_is_gone_is_classified_missing(fixture: Fixture) -> None:
    shutil.rmtree(fixture.stale)
    assert fixture.rows()[fixture.stale.name] == MISSING


def test_a_worktree_carrying_uncommitted_work_is_dirty(fixture: Fixture) -> None:
    (fixture.stale / "f.txt").write_text("edited\n")
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == "carries 1 modified/untracked path(s)"


def test_ambient_show_untracked_files_no_cannot_hide_unsaved_work(fixture: Fixture) -> None:
    """`git status` honours `status.showUntrackedFiles`, and `no` comes from a plain gitconfig.

    Measured, git 2.53: under that setting, plain `git status --porcelain` prints nothing for
    a worktree holding an untracked file, so the row named nothing at all. `-uall` overrides
    the setting, and the count in the reason is what says so — the STATUS cannot, because a
    row whose reads all came back empty is DIRTY too.
    """
    git(fixture.repo, "config", "status.showUntrackedFiles", "no")
    (fixture.stale / "unsaved.txt").write_text("PRECIOUS\n")

    assert git(fixture.stale, "status", "--porcelain") == "", "the setting really is live"
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert "1 modified/untracked path(s)" in row.reason
    assert row.reason != UNLINKED_REASON


def test_a_worktree_that_vanishes_mid_run_is_missing_not_dirty(fixture: Fixture) -> None:
    """`worktree list` and the probe inside each worktree are separate commands, and another
    session can remove its worktree between them — the churn this tool surveys.

    Reporting the row DIRTY on the resulting `git status` failure asserted that a directory
    which is GONE carries work, which is the opposite of what is known about it.
    """
    entries = list_worktrees(fixture.repo)
    shutil.rmtree(fixture.stale)

    rows = {
        row.worktree.path.name: row
        for row in classify_worktrees(
            fixture.repo,
            entries,
            main_path=main_worktree_path(entries),
            live=fixture.live(),
            live_only=frozenset(),
        )
    }

    assert rows[fixture.stale.name].status == MISSING
    assert "directory is gone" in rows[fixture.stale.name].reason
    assert rows[fixture.stale.name].work is None
    assert rows[fixture.named.name].status == LIVE, "the rest of the report is still produced"


def test_a_worktree_that_vanishes_during_the_identity_check_is_missing_not_broken(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race one step later: gone between the directory check and the identity probe.

    A directory that has been removed has no git directory to report either, so the identity
    probe fails for it — and reporting that as a gitdir that is not ours would accuse a
    directory that simply is not there any more.
    """
    real = sweep.git_dir

    def vanishing(path: Path) -> str | None:
        if path == fixture.stale:
            shutil.rmtree(path)
        return real(path)

    monkeypatch.setattr(sweep, "git_dir", vanishing)

    row = fixture.row(fixture.stale.name)

    assert row.status == MISSING
    assert row.reason == "directory is gone"
    assert row.work is None


def test_a_worktree_that_vanishes_after_the_identity_check_is_missing_not_dirty(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race one step later again: gone after the identity check, before the probe."""
    race_after_identity(monkeypatch, fixture.stale, lambda: shutil.rmtree(fixture.stale))
    real_check = sweep.establish_identity
    answers: list[tuple[tuple[str, str] | None, str]] = []

    def recording(path: Path, repo_common: str) -> tuple[tuple[str, str] | None, str]:
        answer = real_check(path, repo_common)
        if path == fixture.stale:
            answers.append(answer)
        return answer

    monkeypatch.setattr(sweep, "establish_identity", recording)

    row = fixture.row(fixture.stale.name)

    assert not fixture.stale.exists(), "the race really ran"
    assert [pin is not None for pin, _ in answers] == [True], answers
    assert row.status == MISSING, f"{row.status}: {row.reason}"
    assert row.reason == "directory is gone"
    assert row.work is None


def test_a_worktree_with_its_own_commit_that_vanishes_after_the_identity_check_names_it(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removes a worktree carrying an off-main commit after the identity check."""
    (fixture.stale / "own.txt").write_text("a commit that exists nowhere else\n")
    git(fixture.stale, "add", "own.txt")
    git(fixture.stale, "commit", "-qm", "own work")
    race_after_identity(monkeypatch, fixture.stale, lambda: shutil.rmtree(fixture.stale))

    row = fixture.row(fixture.stale.name)

    assert not fixture.stale.exists(), "the race really ran"
    assert row.status == MISSING, f"{row.status}: {row.reason}"
    assert row.reason == "directory is gone, carries 1 commit(s) not on the main ref"
    assert row.work is None


def test_a_worktree_whose_commits_could_not_be_listed_says_so_once_it_is_gone(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removes a worktree after the identity check, with its commit listing failing."""
    head = git(fixture.stale, "rev-parse", "HEAD")
    real = sweep.commits_off_main

    def failing_for_stale(repo: Path, rev: str, main_ref: str) -> frozenset[str] | None:
        return None if rev == head else real(repo, rev, main_ref)

    monkeypatch.setattr(sweep, "commits_off_main", failing_for_stale)
    race_after_identity(monkeypatch, fixture.stale, lambda: shutil.rmtree(fixture.stale))

    row = fixture.row(fixture.stale.name)

    assert not fixture.stale.exists(), "the race really ran"
    assert row.status == MISSING, f"{row.status}: {row.reason}"
    assert row.reason == (
        "directory is gone, could not list this worktree's commits against the main ref"
    )
    assert row.work is None


def test_a_live_worktree_removed_after_a_clean_probe_keeps_its_live_row(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removes a worktree linked to live work by a shared off-main commit after the real probe."""
    review = fixture.root / "wf_review-agent"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(review), fixture.live_sha)
    (review / "review-fix.txt").write_text("a fix this agent made\n")
    git(review, "add", "review-fix.txt")
    git(review, "commit", "-qm", "review agent fix")
    assert git(review, "rev-parse", "HEAD") not in fixture.live().shas

    real = sweep.probe_work_state

    def probing_then_removing(
        repo: Path, entry: sweep.Worktree, main_ref: str, pin: tuple[str, str]
    ) -> sweep.WorkState:
        work = real(repo, entry, main_ref, pin)
        if entry.path == review:
            shutil.rmtree(review)
        return work

    monkeypatch.setattr(sweep, "probe_work_state", probing_then_removing)

    row = fixture.row(review.name)

    assert not review.exists(), "the directory really is gone"
    assert row.status == LIVE, f"{row.status}: {row.reason}"
    assert row.reason == "shares a commit off the main ref with the live set"


def test_a_live_worktree_that_vanishes_before_the_probe_is_missing_with_its_commits(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removes a worktree linked to live work by a shared off-main commit before the probe."""
    review = fixture.root / "wf_review-agent"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(review), fixture.live_sha)
    (review / "review-fix.txt").write_text("a fix this agent made\n")
    git(review, "add", "review-fix.txt")
    git(review, "commit", "-qm", "review agent fix")
    assert git(review, "rev-parse", "HEAD") not in fixture.live().shas
    race_after_identity(monkeypatch, review, lambda: shutil.rmtree(review))

    row = fixture.row(review.name)

    assert not review.exists(), "the race really ran"
    assert row.status == MISSING, f"{row.status}: {row.reason}"
    assert row.reason == "directory is gone, carries 2 commit(s) not on the main ref"
    assert row.work is None


def test_a_worktree_carrying_only_a_commit_not_on_main_is_dirty(fixture: Fixture) -> None:
    """`git worktree remove` would take this one without complaint — the tree is clean."""
    (fixture.stale / "f.txt").write_text("committed elsewhere\n")
    git(fixture.stale, "add", "f.txt")
    git(fixture.stale, "commit", "-qm", "work that exists nowhere else")
    assert git(fixture.stale, "status", "--porcelain") == ""
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == "carries 1 commit(s) not on the main ref"


def test_a_merge_commit_that_exists_only_here_is_seen(fixture: Fixture) -> None:
    """`git cherry` emits no merge commit, so it reported this worktree as carrying nothing.

    Measured, git 2.53: with the tree clean and every non-merge parent already on `main`,
    `git cherry main HEAD` prints an empty string while the hand-resolved merge is the only
    copy of its resolution. `git rev-list HEAD ^main` prints the merge.
    """
    for branch, text in (("sideA", "A\n"), ("sideB", "B\n")):
        git(fixture.repo, "branch", branch, "main")
        work = fixture.root / f"wt-{branch}"
        git(fixture.repo, "worktree", "add", "-q", str(work), branch)
        (work / "a.txt").write_text(text)
        git(work, "add", "a.txt")
        git(work, "commit", "-qm", f"{branch} adds a.txt")

    # Both sides reach main, the second by a hand-resolved merge, so every NON-merge commit
    # below is already on main and only the merges are left for `git cherry` to miss.
    git(fixture.repo, "merge", "-q", "--no-ff", "-m", "bring sideA to main", "sideA")
    git_may_fail(fixture.repo, "merge", "--no-ff", "-m", "bring sideB to main", "sideB")
    (fixture.repo / "a.txt").write_text("B\n")
    git(fixture.repo, "add", "a.txt")
    git(fixture.repo, "commit", "-qm", "bring sideB to main")

    merger = fixture.root / "wt-merger"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(merger), "sideA")
    git_may_fail(merger, "merge", "--no-ff", "-m", "hand-resolved merge", "sideB")
    (merger / "a.txt").write_text("RESOLUTION THAT EXISTS NOWHERE ELSE\n")
    git(merger, "add", "a.txt")
    git(merger, "commit", "-qm", "hand-resolved merge")

    assert git(merger, "status", "--porcelain") == ""
    cherry = subprocess.run(
        ["git", "cherry", "main", "HEAD"], cwd=str(merger), capture_output=True, text=True
    )
    assert cherry.stdout == "", "the blind spot this classifier had to stop relying on"
    row = fixture.row(merger.name)
    assert row.status == DIRTY
    assert "commit(s) not on the main ref" in row.reason, (
        "the merge is COUNTED; under `git cherry` the reason named nothing at all, and the "
        "status alone cannot tell those apart"
    )


# ------------------------------------------------ AC1: what a CLEAN working tree can hide

#: The two ignore routes that live INSIDE the repository, so no environment scrub can reach
#: them. Neither needs an attacker: `.git/info/exclude` is already in use in this repository,
#: and a committed `.gitignore` is an ordinary tracked file.
REPO_IGNORE_ROUTES = (".git/info/exclude", "committed .gitignore")


@pytest.mark.parametrize("route", REPO_IGNORE_ROUTES)
def test_a_repository_side_ignore_rule_cannot_hide_unsaved_work(
    fixture: Fixture, route: str
) -> None:
    """The reason on a DIRTY row is what an operator reads, so a reason naming nothing is loss.

    Measured, git 2.53: holding nothing but an unsaved `.env` with a token in it, this
    worktree's row named nothing at all under both routes, because `status --porcelain -uall`
    says nothing about an ignored path. `--ignored=matching` is what shows them, and they are
    then counted as work in their own right. The COUNT is the assertion that bites; the status
    would read DIRTY with the file still unseen.
    """
    if route == ".git/info/exclude":
        common = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        (common / "info").mkdir(exist_ok=True)
        (common / "info" / "exclude").write_text(".env\n*.log\n")
    else:
        # Committed on `main` and then checked out, so the worktree gains an ignore rule
        # WITHOUT gaining a commit of its own.
        (fixture.repo / ".gitignore").write_text(".env\n")
        git(fixture.repo, "add", ".gitignore")
        git(fixture.repo, "commit", "-qm", "ignore .env")
        git(fixture.stale, "checkout", "-q", "--detach", "main")

    (fixture.stale / ".env").write_text("TOKEN=hunter2\n")

    assert git(fixture.stale, "status", "--porcelain", "-uall") == "", "the rule really is live"
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY, route
    assert "1 ignored path(s)" in row.reason
    assert row.work is not None and row.work.ignored == ("!! .env",)


#: The two routes an operator's own global ignore rules arrive by, and the report each earns.
#: `core.excludesFile` is a config KEY, which this module's GIT_CONFIG_GLOBAL pin empties;
#: git's DEFAULT excludes file is a PATH with no key behind it, and that pin leaves it alone.
GLOBAL_IGNORE_ROUTES = (
    ("core.excludesFile", ("?? .env",), (), "1 modified/untracked path(s)"),
    ("the default excludes file", (), ("!! .env",), "1 ignored path(s)"),
)


@pytest.mark.parametrize("route, status_lines, ignored, counted", GLOBAL_IGNORE_ROUTES)
def test_a_global_ignore_rule_is_closed_by_the_config_pin_or_else_counted(
    fixture: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    status_lines: tuple[str, ...],
    ignored: tuple[str, ...],
    counted: str,
) -> None:
    """The third ignore route reaches git two ways, and only one of them has a config key.

    A `core.excludesFile` in an operator's own `~/.gitconfig` listing everyday entries
    (`.env`, `*.log`, `.DS_Store`) hid an unsaved `.env` completely (measured, git 2.53).
    That is a KEY, so pinning GIT_CONFIG_GLOBAL to os.devnull closes the route and the file
    is reported as plainly untracked instead. Git's DEFAULT excludes file —
    `$XDG_CONFIG_HOME/git/ignore`, or `$HOME/.config/git/ignore` when that is unset — is
    where an operator's global rules ordinarily live, and it has no key behind it: measured
    through this module's own `run_git`, `config --get core.excludesFile` exits 1 while
    `check-ignore -v .env` exits 0 and names that file. So THAT rule does apply here, and
    `--ignored=matching` is what still counts the file as ignored.
    """
    home = tmp_path / "home"
    home.mkdir()
    if route == "core.excludesFile":
        (home / ".gitignore_global").write_text(".env\n*.log\n.DS_Store\n")
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home}/.gitignore_global\n")
    else:
        (home / ".config" / "git").mkdir(parents=True)
        (home / ".config" / "git" / "ignore").write_text(".env\n*.log\n.DS_Store\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (fixture.stale / ".env").write_text("TOKEN=hunter2\n")

    assert git(fixture.stale, "status", "--porcelain", "-uall") == "", "the rule really is live"
    assert run_git(fixture.stale, "config", "--get", "core.excludesFile").returncode == 1, (
        "under this module's pins no excludes-file KEY is readable on either route"
    )
    still_ignored = run_git(fixture.stale, "check-ignore", "-v", ".env").returncode == 0
    assert still_ignored is bool(ignored), (
        "yet only the route with a key behind it is closed by emptying config"
    )

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY, route
    assert counted in row.reason
    assert row.work is not None
    assert row.work.status_lines == status_lines
    assert row.work.ignored == ignored


def test_an_index_bit_git_was_told_to_honour_cannot_hide_a_modified_file(
    fixture: Fixture,
) -> None:
    """The other half of a false clean: `status` is silent about a path it was told to skip.

    Measured, git 2.53: with `assume-unchanged` set, `git status --porcelain -uall
    --ignored=matching` prints NOTHING for a tracked file whose contents have been replaced,
    and no status flag overrides the bit. `git ls-files -v` is what still sees it.
    """
    git(fixture.stale, "update-index", "--assume-unchanged", "f.txt")
    (fixture.stale / "f.txt").write_text("SECRET EDIT THAT STATUS WILL NOT SHOW\n")

    assert git(fixture.stale, "status", "--porcelain", "-uall", "--ignored=matching") == "", (
        "the bit really is live"
    )
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert "1 path(s) git was told not to check" in row.reason
    assert row.work is not None and row.work.unchecked == ("h f.txt",)


def test_a_skip_worktree_bit_is_counted_the_same_way(fixture: Fixture) -> None:
    """The sibling bit, which `git status` hides identically and `ls-files -v` tags `S`."""
    git(fixture.stale, "update-index", "--skip-worktree", "f.txt")
    (fixture.stale / "f.txt").write_text("EDIT UNDER SKIP-WORKTREE\n")

    assert git(fixture.stale, "status", "--porcelain", "-uall") == ""
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert "1 path(s) git was told not to check" in row.reason
    assert row.work is not None and row.work.unchecked == ("S f.txt",)


def test_an_index_this_tool_cannot_list_fails_its_own_row_rather_than_reading_clean(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git status` is silent about the two bits above, so a failed index read is itself work.

    The fixture is built so `git status` really is silent — the `assume-unchanged` bit set and
    the file then edited — which is what stops the assertion below passing on status output
    instead. Measured, git 2.53: with `ls-files` unable to start, the row's reason NAMES the
    failure; returning no problem from that arm leaves the row naming nothing over the hidden
    edit instead.
    """
    git(fixture.stale, "update-index", "--assume-unchanged", "f.txt")
    (fixture.stale / "f.txt").write_text("SECRET EDIT THAT STATUS WILL NOT SHOW\n")
    assert git(fixture.stale, "status", "--porcelain", "-uall", "--ignored=matching") == ""

    real = subprocess.run

    def index_unlistable(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-files" in argv:
            raise OSError("no process could be started")
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", index_unlistable)

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == "carries could not list this worktree's index (could not be started)"
    assert row.work is not None and row.work.unchecked == (), (
        "and it is the problem, not a count, that carried the row"
    )


#: The ten code points `str.splitlines()` breaks a string on, against what git does with a
#: PATH holding one. A path and a ref name are different name spaces — a POSIX path may hold
#: all ten — so the question here is not what git ACCEPTS but what it renders RAW into
#: `status --porcelain` and `ls-files -v` rather than C-quoting. The flag is that answer under
#: `core.quotePath=false`; under true all ten are quoted. LF is therefore the only one of the
#: ten that can reach a record boundary in either output, which is what makes `str.split("\n")`
#: the record split there and `str.splitlines()` the same defect `ref_shas` names.
PATH_CODE_POINTS = (
    ("LF", "\n", False),
    ("VT", "\v", False),
    ("FF", "\f", False),
    ("CR", "\r", False),
    ("FS", "\x1c", False),
    ("GS", "\x1d", False),
    ("RS", "\x1e", False),
    ("NEL", "\x85", True),
    ("LS", "\u2028", True),
    ("PS", "\u2029", True),
)

#: The three git hands back raw, which are the three the two record splits are pinned over.
RAW_PATH_POINTS = tuple((label, char) for label, char, raw in PATH_CODE_POINTS if raw)
RAW_PATH_IDS = [label for label, _ in RAW_PATH_POINTS]


@pytest.mark.parametrize(
    ("label", "char", "raw"), PATH_CODE_POINTS, ids=[lbl for lbl, _, _ in PATH_CODE_POINTS]
)
def test_git_renders_three_of_those_code_points_into_a_path_record_unquoted(
    fixture: Fixture, label: str, char: str, raw: bool
) -> None:
    """The premise of the two record splits below, measured rather than reasoned about.

    Both probe commands are asked, because both are split on a record boundary. Measured, git
    2.53: the seven ASCII separators come back C-quoted whatever `core.quotePath` says, and
    U+0085, U+2028 and U+2029 come back raw when it is false — so exactly three of the ten can
    put a code point Python breaks a line on inside a record git meant as one.
    """
    assert len(f"a{char}b".splitlines()) == 2, "Python breaks on every one of these"

    name = f"a{char}b.txt"
    (fixture.stale / name).write_text("x\n")
    git(fixture.stale, "add", "--", name)

    for quote_path, expected in (("false", raw), ("true", False)):
        git(fixture.repo, "config", "core.quotePath", quote_path)
        for what, out in (
            ("status", run_git(fixture.stale, "status", "--porcelain", "-uall").stdout),
            ("ls-files", run_git(fixture.stale, "ls-files", "-v").stdout),
        ):
            note = f"{label}/{what}/core.quotePath={quote_path}: {out!r}"
            assert (name in out) is expected, note
            assert ('"' in out) is not expected, f"{note} — quoted is the other outcome"


@pytest.mark.parametrize(("label", "char"), RAW_PATH_POINTS, ids=RAW_PATH_IDS)
def test_an_untracked_path_git_prints_raw_is_counted_once_not_twice(
    fixture: Fixture, label: str, char: str
) -> None:
    """`git status --porcelain` prints ONE record for it, and `splitlines()` made two.

    `core.quotePath=false` is set here in the surveyed repository's own local config, which
    the command line does not override. Measured, git 2.53: the row reported `carries 2
    modified/untracked path(s)` over the single file git had named.
    """
    git(fixture.repo, "config", "core.quotePath", "false")
    (fixture.stale / f"a{char}b.txt").write_text("unsaved work\n")

    printed = run_git(
        fixture.stale,
        "status",
        "--porcelain",
        "-uall",
        "--ignored=matching",
        "--ignore-submodules=dirty",
    ).stdout
    assert printed == f"?? a{char}b.txt\n", f"{label}: git printed one record, raw: {printed!r}"
    assert len(printed.splitlines()) == 2, f"{label}: and Python breaks that one record in two"

    assert_reads_found(fixture, fixture.stale.name, "carries 1 modified/untracked path(s)", label)


@pytest.mark.parametrize(("label", "char"), RAW_PATH_POINTS, ids=RAW_PATH_IDS)
def test_a_clean_tracked_path_git_prints_raw_is_not_counted_as_hidden(
    fixture: Fixture, label: str, char: str
) -> None:
    """The sharper half: a CLEAN worktree, no index bit set anywhere, read as carrying work.

    `core.quotePath=false` is set here in the worktree's OWN `config.worktree`, which reaches
    this worktree and not the repository beside it (measured, git 2.53). `ls-files --stage -v`
    then prints `H 100644 <sha> 0<TAB>a<code point>b.txt`, and splitting that record on the code
    point left a fragment whose first character is lowercase — counted, so the row claimed a
    path git was told not to check over a worktree carrying nothing at all. Splitting on LF and
    reading the path after the first TAB cannot be fooled that way, and the `100644` mode is not
    a gitlink, so nothing here is counted.
    """
    (fixture.repo / f"a{char}b.txt").write_text("tracked and untouched\n")
    git(fixture.repo, "add", "-A")
    git(fixture.repo, "commit", "-qm", "a tracked path Python splits a line on")
    git(fixture.stale, "checkout", "-q", "--detach", "main")

    git(fixture.repo, "config", "extensions.worktreeConfig", "true")
    git(fixture.stale, "config", "--worktree", "core.quotePath", "false")

    assert run_git(fixture.stale, "status", "--porcelain", "-uall").stdout == "", (
        f"{label}: the worktree really is clean"
    )
    listed = run_git(fixture.stale, "ls-files", "--stage", "-v").stdout
    assert listed.endswith("\n") and listed.count("\n") == 2, f"{label}: two records: {listed!r}"
    parsed = []
    for line in listed.split("\n")[:-1]:
        meta, _, path = line[2:].partition("\t")
        parsed.append((line[:1], meta.split(" ")[0], path))
    assert parsed == [("H", "100644", f"a{char}b.txt"), ("H", "100644", "f.txt")], (
        f"{label}: two clean tracked paths, mode 100644, no index bit set: {listed!r}"
    )
    assert len(listed.splitlines()) == 3, f"{label}: Python makes three records of git's two"

    assert_reads_named_nothing(fixture, fixture.stale.name, label)


def test_an_ignored_build_directory_costs_one_row_not_thousands(fixture: Fixture) -> None:
    """Why `--ignored=matching` and not `--ignored=traditional`, which `-uall` expands.

    Measured, git 2.53: with 300 files under an ignored directory, `traditional` prints one
    line per FILE while `matching` prints the directory that matched the pattern — so a
    worktree carrying a virtualenv costs one row instead of thousands.
    """
    (fixture.repo / ".gitignore").write_text("venvish/\n")
    git(fixture.repo, "add", ".gitignore")
    git(fixture.repo, "commit", "-qm", "ignore venvish")
    git(fixture.stale, "checkout", "-q", "--detach", "main")
    build = fixture.stale / "venvish"
    build.mkdir()
    for index in range(300):
        (build / f"file{index}.bin").write_text("x")

    traditional = git(
        fixture.stale, "status", "--porcelain", "-uall", "--ignored=traditional"
    ).splitlines()
    row = fixture.row(fixture.stale.name)

    assert len(traditional) == 300, "the mode NOT chosen really does print one line per file"
    assert row.work is not None and row.work.ignored == ("!! venvish/",)
    assert row.status == DIRTY
    assert row.reason == "carries 1 ignored path(s)", (
        "one row for the directory, and the residue is NAMED rather than passed over"
    )


#: Four ways to tell `git status` to say nothing about a submodule, reached from three config
#: sources. `.gitmodules` is an ordinary tracked in-tree file, exactly as ordinary as the
#: committed `.gitignore` above, so none of these needs an attacker either.
SUBMODULE_SILENCERS = (
    ("committed .gitmodules, ignore=all", "gitmodules", "all"),
    ("committed .gitmodules, ignore=dirty", "gitmodules", "dirty"),
    ("repository config, submodule.sub.ignore=all", "submodule-config", "all"),
    ("repository config, diff.ignoreSubmodules=all", "diff-config", "all"),
)


@pytest.mark.parametrize(
    ("route", "value"),
    [(route, value) for _, route, value in SUBMODULE_SILENCERS],
    ids=[label for label, _, _ in SUBMODULE_SILENCERS],
)
def test_a_submodule_git_was_told_to_ignore_cannot_hide_unsaved_work(
    fixture: Fixture, tmp_path: Path, route: str, value: str
) -> None:
    """A submodule/gitlink is surfaced by COUNTING it, never by descending into it.

    An earlier version used `--ignore-submodules=none` to force `git status` to itemise the
    submodule's dirtiness (` M sub`). That was the BLOCKER-1 vector: `=none` descends, and the
    descent EXECUTES a `filter.*.clean` in the gitlink's own config (measured, git 2.53). The
    probe now runs `--ignore-submodules=dirty`, which does NOT descend, so it prints nothing
    about a submodule whose only change is inside its working tree — every route below.

    Measured, git 2.53: holding a modified tracked file AND an untracked `.env` inside
    the submodule, `status --porcelain -uall --ignored=matching --ignore-submodules=dirty`
    prints the empty string, while `ls-files --stage -v` prints `H 160000 <sha> 0<TAB>sub`. So
    the row is DIRTY and names the gitlink.
    The internal diff is given up on
    purpose; running the gitlink's config to obtain it is the thing being avoided.

    Scope, re-measured here rather than inherited, because the inherited version was half
    true: `git worktree remove` does refuse this shape — "working trees containing submodules
    cannot be moved or removed" — but `--force` then removes it anyway, submodule and unsaved
    work with it (git 2.53). So the refusal bounds a PLAIN removal only. What this row costs
    today is a nested repo an operator may act on by hand; this tool has no deletion path at
    all, and the one E32-S02 adds must not reach for `--force` here.
    """
    origin = init_repo(tmp_path / "sub-origin")
    (origin / "s.txt").write_text("sub base\n")
    git(origin, "add", "s.txt")
    git(origin, "commit", "-qm", "sub base")
    git(
        fixture.repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(origin),
        "sub",
    )
    git(fixture.repo, "commit", "-qm", "add submodule")
    if route == "gitmodules":
        git(fixture.repo, "config", "-f", ".gitmodules", "submodule.sub.ignore", value)
        git(fixture.repo, "add", ".gitmodules")
        git(fixture.repo, "commit", "-qm", "hide the submodule")
    elif route == "submodule-config":
        git(fixture.repo, "config", "submodule.sub.ignore", value)
    else:
        git(fixture.repo, "config", "diff.ignoreSubmodules", value)

    git(fixture.stale, "checkout", "-q", "--detach", "main")
    git(fixture.stale, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "-q")
    (fixture.stale / "sub" / "s.txt").write_text("MODIFIED TRACKED FILE\n")
    (fixture.stale / "sub" / ".env").write_text("TOKEN=hunter2\n")

    # The probe's OWN command — not descending — is silent about the dirty submodule.
    assert (
        git(
            fixture.stale,
            "status",
            "--porcelain",
            "-uall",
            "--ignored=matching",
            "--ignore-submodules=dirty",
        )
        == ""
    ), "the setting really is live, and the probe does not descend to override it"
    assert "160000" in git(fixture.stale, "ls-files", "--stage", "-v"), (
        "the gitlink is a 160000 index entry, which is what the count reads"
    )
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == "carries 1 in-tree gitlink(s) not descended into", route
    assert row.work is not None
    assert row.work.status_lines == (), "the submodule's internal diff is not itemised"
    assert row.work.gitlinks == ("sub",), "it is surfaced as a gitlink instead"


def test_an_in_tree_gitlinks_own_clean_filter_is_never_run_yet_the_gitlink_is_reported(
    fixture: Fixture, tmp_path: Path
) -> None:
    """BLOCKER 1: a nested gitlink's config executed code during a descending `git status`.

    Measured, git 2.53: with `--ignore-submodules=none`, `git status` descends into an in-tree
    gitlink whose `.git` is a DIRECTORY supplying its own config from inside the
    attacker-writable worktree, and a `filter.*.clean` there EXECUTES during an ordinary sweep
    while the row prints an innocuous DIRTY. The identity check cannot bound this: the gitlink
    is not among THIS repository's git directories. The fix is `=dirty` — which does
    not descend and so does not run the config — plus counting the `160000` entry from the index.

    Both directions are proved here: the descending mode the module NO LONGER uses fires the
    marker, and the module's own probe does not — while the gitlink is still named as work.
    """
    scrub = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    marker = tmp_path / "PWNED_GITLINK_FILTER"
    plant_in_tree_gitlink(fixture.stale, marker)

    # The vector is real: the descending mode executes the gitlink's own clean filter.
    subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=",
            "status",
            "--porcelain",
            "-uall",
            "--ignore-submodules=none",
        ],
        cwd=str(fixture.stale),
        capture_output=True,
        env=scrub,
    )
    assert marker.exists(), "the descending mode really does execute the gitlink's clean filter"
    marker.unlink()

    # The module's probe does not descend, so the filter never runs...
    row = fixture.row(fixture.stale.name)
    assert not marker.exists(), "the sweep did not execute the gitlink's config"

    # ...and the gitlink is still counted as work in its own right.
    # `ls-files --stage -v` did not run it either.
    assert row.status == DIRTY
    assert row.work is not None and row.work.gitlinks == ("sub",)
    assert "in-tree gitlink(s) not descended into" in row.reason
    assert not marker.exists(), "and the index read did not run it either"


def test_a_gitlink_whose_head_moved_is_itemised_and_its_config_is_not_run(
    fixture: Fixture, tmp_path: Path
) -> None:
    """`--ignore-submodules=dirty` itemises a gitlink whose HEAD moved; `=all` does not.

    The fixture holds the gitlink's own filter driver, which the descending mode would run.

    The new commit is made with `commit-tree` and `update-ref`, because the porcelain commit
    runs that filter during the fixture's own setup (measured, git 2.53).
    """
    marker = tmp_path / "PWNED_MOVED_HEAD_FILTER"
    plant_in_tree_gitlink(fixture.stale, marker)
    sub = fixture.stale / "sub"
    moved = git(sub, "commit-tree", "-p", "HEAD", "-m", "moved on", "HEAD^{tree}")
    git(sub, "update-ref", "HEAD", moved)
    assert not marker.exists(), "nothing has fired during the fixture's own setup"

    # The control: the mode the probe does not use says nothing for this same fixture.
    silent = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "-uall",
            "--ignored=matching",
            "--ignore-submodules=all",
        ],
        cwd=str(fixture.stale),
        capture_output=True,
        text=True,
    )
    assert silent.stdout == "", "the moved HEAD is invisible to `=all`, so the fixture can tell"

    row = fixture.row(fixture.stale.name)

    assert row.status == DIRTY
    assert row.work is not None
    assert row.work.status_lines == ("AM sub",), "`A`: new in the index; `M`: HEAD moved off it"
    assert row.work.gitlinks == ("sub",)
    assert row.reason == (
        "carries 1 modified/untracked path(s), 1 in-tree gitlink(s) not descended into"
    )
    assert not marker.exists(), "itemising a moved HEAD did not run the gitlink's config"


#: The config sources a mode-only silencer can be written into. A `-c` on the command line
#: outranks BOTH, which is the point of forcing it there rather than emptying a file.
FILE_MODE_SOURCES = ("the repository's local config", "a worktree's own config.worktree")


@pytest.mark.parametrize("source", FILE_MODE_SOURCES)
def test_a_mode_only_change_git_was_told_not_to_see_cannot_hide_it(
    fixture: Fixture, source: str
) -> None:
    """The same class as the submodule setting: a config key that suppresses a real change.

    Measured, git 2.53: with `core.fileMode=false`, `git status --porcelain -uall
    --ignored=matching` prints NOTHING for a tracked file whose mode was changed, and the row
    named nothing at all. `config.worktree` is the source the GIT_CONFIG_* pins cannot empty
    and the local config is the one they were never meant to, so both are exercised here.
    """
    (fixture.repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
    git(fixture.repo, "add", "run.sh")
    git(fixture.repo, "commit", "-qm", "a script")
    git(fixture.stale, "checkout", "-q", "--detach", "main")
    if source == "the repository's local config":
        git(fixture.repo, "config", "core.fileMode", "false")
    else:
        git(fixture.repo, "config", "extensions.worktreeConfig", "true")
        git_dir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))
        (git_dir / "config.worktree").write_text("[core]\n\tfileMode = false\n")
    os.chmod(fixture.stale / "run.sh", 0o755)

    assert git(fixture.stale, "status", "--porcelain", "-uall", "--ignored=matching") == "", (
        "the setting really is live"
    )
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == "carries 1 modified/untracked path(s)", source
    assert row.work is not None and row.work.status_lines == (" M run.sh",)


#: The config keys the Critical finding named, and the two sources no GIT_CONFIG_* pin can
#: empty. `no config at all` is here because it is what the fixture below MEASURES: on this
#: filesystem git's default stat comparison already misses the edit, so the keys are one route
#: into the class rather than the whole of it.
CHECK_STAT_SOURCES = (
    ("core.checkStat", "minimal", "local"),
    ("core.checkStat", "minimal", "config.worktree"),
    ("core.trustctime", "false", "local"),
    (None, None, "no config at all"),
)


def leave_half_a_second_before_the_arrangement() -> None:
    """Sleep past the next wall-clock second unless at least half of this one is left.

    With no config, git compares a tracked file's ctime to the second, and every write and
    `os.utime` sets ctime to now.
    """
    left = 1 - time.time() % 1
    if left < 0.5:
        time.sleep(left + 0.02)


@pytest.mark.parametrize(
    ("key", "value", "source"),
    CHECK_STAT_SOURCES,
    ids=[f"{k or 'none'}-{s}" for k, _, s in CHECK_STAT_SOURCES],
)
def test_a_same_size_edit_the_stat_cache_hides_gets_a_row_that_claims_nothing(
    fixture: Fixture, key: str | None, value: str | None, source: str
) -> None:
    """The CRITICAL, and the proof that dropping the verdict DISSOLVES it rather than moves it.

    `git status` compares a tracked file by stat before it compares it by content, so a
    SAME-SIZE edit whose mtime is preserved is invisible to it. Measured, git 2.53: `status`
    prints nothing, `ls-files -v` shows a bare `H` carrying no hiding flag, `rev-list HEAD
    ^main` is empty, and the content on disk exists nowhere else — three empty reads over real
    work. Under the PRUNABLE verdict this row read "clean working tree, and no commit off the
    main ref reachable from its HEAD", every word of which this fixture falsifies.

    There is no fix for the read: it is git's own comparison, and the last case here measures
    that no hostile config is needed to reach it. What is fixed is the SENTENCE. The row still
    appears, and its reason now speaks only about the live-set search this run really did run,
    so this fixture can no longer make any part of it false.
    """
    if source == "local":
        git(fixture.repo, "config", str(key), str(value))
    elif source == "config.worktree":
        git(fixture.repo, "config", "extensions.worktreeConfig", "true")
        gitdir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))
        (gitdir / "config.worktree").write_text(f"[core]\n\tcheckStat = {value}\n")

    target = fixture.stale / "f.txt"
    leave_half_a_second_before_the_arrangement()
    os.utime(target, (0, 0))
    git(fixture.stale, "status", "--porcelain")  # record the aged stat in the index
    aged = target.stat()
    target.write_text("HUNT\n")  # exactly as long as the committed "base\n"
    os.utime(target, ns=(aged.st_atime_ns, aged.st_mtime_ns))

    assert target.read_text() == "HUNT\n" != git(fixture.stale, "show", "HEAD:f.txt") + "\n"
    assert target.stat().st_size == aged.st_size, "same size, which is what defeats the compare"
    os.utime(target, None)
    assert git(fixture.stale, "status", "--porcelain") != "", (
        "the edit is REAL: git names it the moment the mtime moves, so this fixture is not "
        "measuring an unchanged file"
    )
    os.utime(target, ns=(aged.st_atime_ns, aged.st_mtime_ns))

    assert git(fixture.stale, "status", "--porcelain", "-uall", "--ignored=matching") == "", source
    assert git(fixture.stale, "ls-files", "-v") == "H f.txt", "no hiding flag to count either"
    assert git(fixture.stale, "rev-list", "HEAD", "^main") == "", "and no commit of its own"

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == UNLINKED_REASON
    assert not any(word in row.reason.lower() for word in CLEARANCE_WORDS), (
        f"the working tree is NOT clean, so nothing in the row may say so: {row.reason}"
    )


REGRESSION_STARTS = ((0.99, True), (0.51, True), (0.45, False))


@pytest.mark.parametrize(("low", "waits"), REGRESSION_STARTS)
def test_the_no_config_case_leaves_half_a_second(fixture: Fixture, low: float, waits: bool) -> None:
    """E23-S63: the `[none-no config at all]` case above, started at `REGRESSION_STARTS`."""
    for _ in range(20):
        time.sleep((low + 0.001 - time.time() % 1) % 1)
        started = time.time()
        if low <= started % 1 < low + 0.03:
            break
    else:
        pytest.fail(f"never started in [{low}, {min(low + 0.03, 1)})")

    test_a_same_size_edit_the_stat_cache_hides_gets_a_row_that_claims_nothing(
        fixture, None, None, "no config at all"
    )
    debug = git(fixture.stale, "ls-files", "--debug", "f.txt")
    match = re.search(r"ctime: (\d+):(\d+)", debug)
    assert match, debug
    sec, nsec = int(match.group(1)), int(match.group(2))
    if waits:
        assert (sec, nsec < 500_000_000) == (int(started) + 1, True), (
            f"not in the next second's first half: {debug}"
        )
    else:
        assert sec == int(started), f"a wait with half a second left: {debug}"


def test_a_clean_driver_in_the_surveyed_config_gets_a_row_that_claims_nothing(
    fixture: Fixture,
) -> None:
    """The HIGH, the sibling shape, and the same proof.

    `filter.*.clean` was named in `GIT_FIXED_PREFIX`'s own comment and treated only as an
    EXECUTOR — a thing to keep from RUNNING. It is a SILENCER too, and unlike `core.fileMode`
    no `-c` can neutralise it: a `-c` overrides a key by name, and driver names cannot be
    enumerated. `ls-files -v` shows no hiding flag for it either.

    Measured, git 2.53: `git status` short-circuits to modified on a size mismatch WITHOUT
    re-hashing, so a same-size edit is the shape that reaches the driver — and the driver then
    converts the content back to the committed blob and status falls silent over an edit that
    exists nowhere else. Its row read PRUNABLE. It now reads a sentence this fixture cannot
    falsify, because that sentence says nothing about the working tree at all.
    """
    (fixture.repo / ".gitattributes").write_text("f.txt filter=hush\n")
    git(fixture.repo, "add", ".gitattributes")
    git(fixture.repo, "commit", "-qm", "assign the filter")
    git(fixture.stale, "checkout", "-q", "--detach", "main")
    driver = "cat >/dev/null; printf 'base\\n'"
    git(fixture.repo, "config", "filter.hush.clean", driver)

    target = fixture.stale / "f.txt"
    target.write_text("HUNT\n")  # exactly as long as the committed "base\n"

    assert git(fixture.stale, "check-attr", "filter", "f.txt").endswith("filter: hush")
    assert git(fixture.stale, "hash-object", "--path", "f.txt", "f.txt") == git(
        fixture.stale, "rev-parse", "HEAD:f.txt"
    ), "the driver converts the edited file back to the committed blob"
    git(fixture.repo, "config", "--unset", "filter.hush.clean")
    assert git(fixture.stale, "status", "--porcelain") != "", (
        "the edit is REAL: with the driver gone git names it, so the silence below is the "
        "driver's doing and not an unchanged file"
    )
    git(fixture.repo, "config", "filter.hush.clean", driver)

    assert git(fixture.stale, "status", "--porcelain", "-uall", "--ignored=matching") == ""
    assert git(fixture.stale, "ls-files", "-v") == "H .gitattributes\nH f.txt", "no hiding flag"
    assert git(fixture.stale, "rev-list", "HEAD", "^main") == ""

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == UNLINKED_REASON
    assert not any(word in row.reason.lower() for word in CLEARANCE_WORDS), (
        f"an edit that exists nowhere else is on disk, and the row does not clear it: {row.reason}"
    )


def test_a_worktree_whose_git_reads_another_directory_is_not_reported_clean(
    fixture: Fixture, tmp_path: Path
) -> None:
    """The member found by asking what ELSE decides which files `git status` compares.

    Not a setting that suppresses a difference but one that moves the working tree: measured,
    git 2.53, `core.worktree` in a worktree's own config made `git status` answer about
    another directory entirely while this one held an edited tracked file and an untracked
    `.env`. Which forced values reach it is measured below rather than described, because the
    first wording of this test got it wrong: a `-c` does NOT undo it and `--work-tree` DOES.
    The check is a comparison anyway — a forced value closes the mechanism someone named,
    and comparing what git reports against the registered path needs no such list.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "f.txt").write_text("base\n")
    git(fixture.repo, "config", "extensions.worktreeConfig", "true")
    git_dir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))
    (git_dir / "config.worktree").write_text(f"[core]\n\tworktree = {decoy}\n")
    (fixture.stale / "f.txt").write_text("REAL UNSAVED WORK\n")
    (fixture.stale / ".env").write_text("TOKEN=hunter2\n")

    assert git(fixture.stale, "status", "--porcelain", "-uall", "--ignored=matching") == "", (
        "git really is answering about the decoy"
    )
    stale = str(fixture.stale)
    assert git(fixture.stale, "-c", f"core.worktree={stale}", "status", "--porcelain") == "", (
        "a `-c` naming the registered path does NOT undo this one"
    )
    forced = git(fixture.stale, f"--work-tree={stale}", "status", "--porcelain", "-uall")
    assert forced.splitlines() == ["M f.txt", "?? .env"], (
        "an explicit --work-tree does — so forcing closes THIS mechanism, and only this one"
    )

    row = fixture.row(fixture.stale.name)
    assert row.status == BROKEN
    assert "not the working tree git reads" in row.reason
    assert row.work is None, "no count may be read out of a directory git is not reading"


# --------------------------------------------------- F1: the live test is on reachability


def test_a_detached_worktree_that_committed_on_top_of_live_work_is_live(
    fixture: Fixture,
) -> None:
    """The live SHAs are branch TIPS, so a detached worktree that commits leaves the set.

    This is the shape a review or verify agent takes: it checks out the story branch's
    commit detached, then makes a commit of its own. Its HEAD is at no branch tip, so SHA
    equality calls it unrelated while the story is in flight and its branch is pushed.
    """
    review = fixture.root / "wf_review-agent"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(review), fixture.live_sha)
    assert fixture.rows()[review.name] == LIVE, "the pre-commit state, for contrast"

    (review / "review-fix.txt").write_text("a fix this agent made\n")
    git(review, "add", "review-fix.txt")
    git(review, "commit", "-qm", "review agent fix")

    assert git(review, "rev-parse", "HEAD") not in fixture.live().shas
    assert fixture.rows()[review.name] == LIVE


def test_a_detached_worktree_left_behind_by_a_moving_branch_is_live(fixture: Fixture) -> None:
    """The other direction: the branch moved on and the worktree's HEAD is now an ancestor."""
    review = fixture.root / "wf_left-behind"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(review), fixture.live_sha)
    (fixture.named / "more.txt").write_text("the implementer kept going\n")
    git(fixture.named, "add", "more.txt")
    git(fixture.named, "commit", "-qm", "more E99-S01 work")
    moved = LiveSet(shas=frozenset({git(fixture.repo, "rev-parse", LIVE_BRANCH)}))

    assert git(review, "rev-parse", "HEAD") not in moved.shas
    assert fixture.rows(moved)[review.name] == LIVE


def test_reachability_does_not_swallow_worktrees_that_merely_share_main(
    fixture: Fixture,
) -> None:
    """The anti-vacuity guard for F1's fix: ancestry alone would call the whole repo LIVE.

    Every worktree sitting at an old commit is an ancestor of every live branch tip, so the
    test subtracts the main ref first. Both worktrees below are ancestors-or-descendants of
    the live SHA through `main` alone and neither may be LIVE.
    """
    unrelated = fixture.root / "wt-unrelated"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(unrelated), "main")
    (unrelated / "abandoned.txt").write_text("its own dead end\n")
    git(unrelated, "add", "abandoned.txt")
    git(unrelated, "commit", "-qm", "abandoned work off main")

    rows = fixture.rows()

    assert rows[fixture.stale.name] != LIVE, "sits at main, an ancestor of the live tip"
    assert rows[unrelated.name] != LIVE, "descends from main, but shares no live commit"
    assert fixture.row(unrelated.name).reason == "carries 1 commit(s) not on the main ref"


def test_a_rebase_severs_the_link(fixture: Fixture) -> None:
    """What the reachability test still gets wrong."""
    review = fixture.root / "wf_pre-rebase"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(review), fixture.live_sha)
    (fixture.repo / "moved.txt").write_text("main moves on\n")
    git(fixture.repo, "add", "moved.txt")
    git(fixture.repo, "commit", "-qm", "main moves on")
    git(fixture.named, "rebase", "-q", "main")
    rebased = LiveSet(shas=frozenset({git(fixture.repo, "rev-parse", LIVE_BRANCH)}))

    row = fixture.row(review.name, rebased)

    assert row.status == DIRTY
    assert "1 commit(s) not on the main ref" in row.reason


def test_naming_the_missed_commit_live_is_what_restores_the_link(
    fixture: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """--live-sha/--live-branch put the severed row back.

    The same fixture as the rebase above, re-swept with the pre-rebase commit named. Handing
    the value straight to `build_live_set` instead leaves the argv wiring unmeasured: cutting
    either flag out of `main` then changes nothing this suite can see (measured — both cuts
    survived while the assertion went through the function).
    """
    review = fixture.root / "wf_pre-rebase-named"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(review), fixture.live_sha)
    (fixture.repo / "moved.txt").write_text("main moves on\n")
    git(fixture.repo, "add", "moved.txt")
    git(fixture.repo, "commit", "-qm", "main moves on")
    git(fixture.named, "rebase", "-q", "main")
    severed = git(review, "rev-parse", "HEAD")
    git(fixture.repo, "branch", "pre-rebase-copy", severed)

    def swept_status(*flags: str) -> str:
        # sweep_argv is NOT used here: it names fixture.live_sha, which IS the severed commit,
        # so it would restore the row before the flag under test could.
        assert main(["--repo", str(fixture.repo), "--no-gh", *flags]) == 0
        out = capsys.readouterr().out
        return next(ln for ln in out.splitlines() if ln.endswith(f"  {review}")).split()[0]

    assert swept_status() == DIRTY, "severed, for contrast"
    assert swept_status("--live-sha", severed) == LIVE
    assert swept_status("--live-branch", "pre-rebase-copy") == LIVE


def test_a_branch_no_source_names_is_live_when_it_keeps_a_live_branchs_commit(
    fixture: Fixture,
) -> None:
    """A local branch cut from the live story branch, never pushed and named by no chart row,
    with a commit of its own on top. Measured, it is LIVE — it keeps the live branch's own
    off-main commit underneath its own, and that is the whole of what the reachability half
    asks.
    """
    unnamed = fixture.root / "wf_cut-from-live"
    git(fixture.repo, "branch", "private-spike", LIVE_BRANCH)
    git(fixture.repo, "worktree", "add", "-q", str(unnamed), "private-spike")
    (unnamed / "spike.txt").write_text("a spike nobody has heard of\n")
    git(unnamed, "add", "spike.txt")
    git(unnamed, "commit", "-qm", "unnamed spike")

    live = fixture.live()
    assert git(unnamed, "rev-parse", "HEAD") not in live.shas, "no source names this branch"
    assert "private-spike" not in live.branches

    row = fixture.row(unnamed.name, live)
    assert row.status == LIVE
    assert row.reason == "shares a commit off the main ref with the live set"


def test_a_worktree_at_the_tip_an_amend_rewrote_is_live_when_a_second_commit_survives(
    fixture: Fixture,
) -> None:
    """The fixture gives the branch a SECOND off-main commit deliberately — with only one, the
    amend replaces the branch's whole off-main history and the rebase shape above is what is
    being measured instead.
    """
    (fixture.named / "second.txt").write_text("a second commit off main\n")
    git(fixture.named, "add", "second.txt")
    git(fixture.named, "commit", "-qm", "second E99-S01 commit")
    pre_amend = git(fixture.named, "rev-parse", "HEAD")
    left_behind = fixture.root / "wf_pre-amend"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(left_behind), pre_amend)

    git(fixture.named, "commit", "-q", "--amend", "-m", "second E99-S01 commit, reworded")
    amended = LiveSet(shas=frozenset({git(fixture.repo, "rev-parse", LIVE_BRANCH)}))

    assert git(left_behind, "rev-parse", "HEAD") not in amended.shas, "the tip really was rewritten"
    assert len(commits_off_main(fixture.repo, LIVE_BRANCH, "main") or ()) == 2, (
        "and the branch really does carry a second off-main commit the amend did not touch"
    )

    row = fixture.row(left_behind.name, amended)
    assert row.status == LIVE
    assert row.reason == "shares a commit off the main ref with the live set"


def test_a_branch_cut_from_the_main_ref_and_committed_on_alone_is_missed(
    fixture: Fixture,
) -> None:
    """Cut from `main`, so it shares no off-main commit with any live branch and its HEAD is
    no live SHA.
    """
    unnamed = fixture.root / "wf_cut-from-main"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(unnamed), "main")
    (unnamed / "secret.txt").write_text("work on a branch nobody names\n")
    git(unnamed, "add", "secret.txt")
    git(unnamed, "commit", "-qm", "unnamed work")

    live = fixture.live()
    own = commits_off_main(fixture.repo, git(unnamed, "rev-parse", "HEAD"), "main")
    assert own is not None and not own & live_only_commits(fixture.repo, live.shas, "main"), (
        "every commit it holds off the main ref is reachable from no live SHA"
    )

    row = fixture.row(unnamed.name, live)
    assert row.status == DIRTY
    assert row.reason == "carries 1 commit(s) not on the main ref"


# ------------------------------------------------- F2: a broken registration is not pruned


def test_a_broken_registration_whose_directory_still_exists_is_reported_broken(
    fixture: Fixture,
) -> None:
    """`git worktree prune` deregisters this shape, and its uncommitted work goes with it.

    Measured, git 2.53: a worktree whose `.git` link is missing is marked prunable while its
    DIRECTORY IS STILL THERE, and `git worktree prune` clears the entry — after which the
    directory is registered nowhere and no later run can find it. That is the crashed-session
    residue this tool exists to report, so it is reported, never pruned.
    """
    (fixture.stale / "unsaved.txt").write_text("PRECIOUS\n")
    break_registration(fixture.stale)

    row = fixture.row(fixture.stale.name)

    assert row.status == BROKEN
    assert "still on disk" in row.reason
    assert row.work is None, "a broken worktree is not inspected: there is no worktree there"


def test_the_guards_that_pre_empt_the_commit_test_take_precedence_in_order(
    fixture: Fixture,
) -> None:
    """The same worktree, whose HEAD IS in the live set, is reported BROKEN once its `.git`
    link breaks and MISSING once its directory goes — so LIVE is not the first word on any
    entry, and MISSING outranks BROKEN even when git flags the entry prunable.
    """
    entry = fixture.root / "wf_in-live-set"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(entry), fixture.live_sha)

    assert fixture.rows()[entry.name] == LIVE

    break_registration(entry)
    assert fixture.rows()[entry.name] == BROKEN

    shutil.rmtree(entry)
    assert fixture.rows()[entry.name] == MISSING
    assert fixture.rows()["repo"] == MAIN, "and the main checkout outranks all of them"


def test_the_identity_check_pre_empts_the_reachability_half_of_the_commit_test(
    fixture: Fixture,
) -> None:
    """SHA equality needs no directory of ours, so it runs first; reachability is followed
    immediately by reading the directory, so the identity check runs between the two halves.
    The consequence: a worktree the reachability half would call LIVE
    comes back BROKEN once its directory is replaced by a foreign repository.
    """
    child = fixture.root / "wf_child-of-live"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(child), fixture.live_sha)
    (child / "c.txt").write_text("child\n")
    git(child, "add", "c.txt")
    git(child, "commit", "-qm", "on top of the live work")

    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[child.name]
    assert entry.head not in fixture.live().shas, "the SHA-equality half cannot see this one"
    assert fixture.rows()[child.name] == LIVE, "so only reachability makes it LIVE"

    shutil.rmtree(child)
    init_repo(child)

    row = fixture.row(child.name)
    assert row.status == BROKEN, "and the identity check outranks that half"
    assert row.work is None


def test_sha_equality_runs_before_the_identity_check_not_after_it(fixture: Fixture) -> None:
    """The other side of the same ordering.

    The test above pins that the identity check comes BEFORE reachability. It says nothing
    about the half on the other side of it, and the two orders are not equivalent.

    LIVE is the right answer here and the ordering is what buys it. SHA equality reads the
    HEAD `worktree list` already reported, so it needs no directory of ours and cannot be
    misled by whatever now sits at the registered path — while the identity check exists to
    stop this tool READING that directory. A worktree whose commit is in the live set is live
    work whether or not its directory can be inspected, so the cheap answer that needs no
    directory is the one that must be reached first.
    """
    entry = fixture.root / "wf_live-sha-foreign-dir"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(entry), fixture.live_sha)

    listed = {e.path.name: e for e in list_worktrees(fixture.repo)}[entry.name]
    assert listed.head in fixture.live().shas, "the SHA-equality half really can see this one"

    # Replace the directory with a foreign repository: the identity check now fails, and the
    # registration is still neither prunable nor gone.
    shutil.rmtree(entry)
    init_repo(entry)
    assert entry in fixture.registrations(), "still registered"
    assert not {e.path.name: e for e in list_worktrees(fixture.repo)}[entry.name].prunable
    assert refusal(entry, os.path.realpath(fixture.repo / ".git")) != "", (
        "and the identity check really does refuse it"
    )

    row = fixture.row(entry.name)
    assert row.status == LIVE, f"{row.status}: {row.reason}"
    assert row.reason == "HEAD commit is in the live set", row.reason
    assert row.work is None, "and being LIVE by SHA reads no directory at all"


def test_a_default_run_changes_nothing_at_all(
    fixture: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The closing line says nothing was changed, so nothing may be — registrations included.

    The broken entry is the one that used to disappear: `git worktree prune` ran first and
    unconditionally, so a directory full of unsaved work was deregistered by a plain report.
    """
    broken = fixture.root / "wt-crashed"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(broken), "main")
    (broken / "unsaved.txt").write_text("PRECIOUS\n")
    break_registration(broken)
    gone = fixture.root / "wt-gone"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(gone), "main")
    shutil.rmtree(gone)
    before = fixture.registrations()

    assert main(sweep_argv(fixture)) == 0
    out = capsys.readouterr().out

    assert fixture.registrations() == before, "not one registration may be added or removed"
    assert broken in before and (broken / "unsaved.txt").read_text() == "PRECIOUS\n"
    assert gone in before, "even the entry prune exists to clear is left registered"
    for path in (fixture.named, fixture.detached, fixture.generic, fixture.stale, broken):
        assert path.is_dir()
    assert "Nothing was removed or deregistered" in out


def test_the_report_names_every_worktree_without_claiming_a_trend(
    fixture: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(sweep_argv(fixture)) == 0
    out = capsys.readouterr().out
    for path in (fixture.named, fixture.detached, fixture.generic, fixture.stale):
        assert str(path) in out
    assert "point-in-time" in out


# ------------------------------------------------------ F3/F5: the git surface is read-only
#
# These scans are the ONLY evidence for this module's one claim — coverage cannot see
# `scripts/` — so each is a POSITIVE allowlist read out of the module's own syntax tree.
# A blocklist of forbidden names is not a proof: it passes everything nobody thought of.

#: Every git argv the module can build, as the leading run of string literals at each call
#: site. A prefix identifies the operation: `worktree` alone would admit `remove` and `prune`.
READ_ONLY_GIT_ARGV_PREFIXES = frozenset(
    {
        ("worktree", "list", "--porcelain", "-z"),
        ("status", "--porcelain", "-uall", "--ignored=matching", "--ignore-submodules=dirty"),
        ("ls-files", "--stage", "-v"),
        ("rev-list", "--end-of-options"),
        ("rev-parse", "--verify", "--quiet", "--end-of-options"),
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        ("rev-parse", "--path-format=absolute", "--git-dir"),
        ("rev-parse", "--path-format=absolute", "--show-toplevel"),
        (
            "for-each-ref",
            "--format=%(refname)%00%(objectname)",
            "refs/heads",
            "refs/remotes/origin",
        ),
    }
)

#: The `gh` argv, in full. `None` is the one element computed at runtime (the --limit value).
#: Unpinned, `gh pr close` or `gh repo delete` would pass every other guard in this file.
GH_ARGV = ("gh", "pr", "list", "--state", "open", "--limit", None, "--json", "headRefName")

#: Everything the module may import. A new capability has to arrive through one of these or
#: through a builtin, and the two call allowlists below pin the builtins.
EXPECTED_IMPORTS = frozenset(
    {
        "__future__.annotations",
        "argparse",
        "collections.abc.Iterable",
        "collections.abc.Sequence",
        "dataclasses.dataclass",
        "json",
        "os",
        "pathlib.Path",
        "re",
        "subprocess",
        "sys",
    }
)

#: Every ATTRIBUTE the module calls. Read this list as the proof: it holds no name that
#: writes, moves, deletes or spawns except `run`, whose two call sites are pinned below.
#: `write_text`, `unlink`, `rename`, `replace`, `move`, `touch`, `utime`, `mkdir`, `chmod`,
#: `system` and `Popen` are absent, and adding any of them fails this test.
CALLED_ATTRIBUTES = frozenset(
    {
        "ArgumentParser",
        "CompletedProcess",
        "add_argument",
        "append",
        "compile",
        "decode",
        "escape",
        "exit",
        "extend",
        "get",
        "intersection",
        "is_dir",
        "is_file",
        "islower",
        "isprintable",
        "items",
        "join",
        "loads",
        "lower",
        "normpath",
        "parse_args",
        "partition",
        "read_bytes",
        "read_text",
        "realpath",
        "removesuffix",
        "resolve",
        "run",
        "search",
        "split",
        "startswith",
        "stat",
        "strip",
        "update",
        "values",
    }
)

#: Every bare name the module calls that is NOT one of its own functions. `open`, `eval`,
#: `exec`, `getattr` and `__import__` are absent, so no call can be assembled at runtime.
CALLED_BUILTINS = frozenset(
    {
        "Classified",
        "GhBranches",
        "LiveSet",
        "Path",
        "RuntimeError",
        "WorkState",
        "Worktree",
        "ascii",
        "dataclass",
        "frozenset",
        "isinstance",
        "len",
        "max",
        "print",
        "set",
        "sorted",
        "str",
        "tuple",
    }
)

#: The only attribute in CALLED_ATTRIBUTES that starts a process.
SPAWNING_ATTRIBUTE = "run"


def module_ast() -> ast.Module:
    return ast.parse(Path(sweep.__file__).read_text())


def module_functions() -> set[str]:
    return {n.name for n in ast.walk(module_ast()) if isinstance(n, ast.FunctionDef)}


def calls_with_owner(module: ast.Module | None = None) -> list[tuple[str, ast.Call]]:
    """Every Call in the module paired with the name of the function that encloses it.

    Takes the tree rather than parsing its own, so a caller comparing node IDENTITY against
    the same tree is not silently comparing two different parses of the same file.
    """
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                found.append((owner, child))
            visit(child, child.name if isinstance(child, ast.FunctionDef) else owner)

    visit(module if module is not None else module_ast(), "<module>")
    return found


def leading_literals(call: ast.Call) -> tuple[str, ...]:
    """The run of string literals after the cwd argument, stopping at the first non-literal."""
    literals: list[str] = []
    for arg in call.args[1:]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            literals.append(arg.value)
        else:
            break
    return tuple(literals)


def test_the_module_imports_exactly_the_read_only_toolkit() -> None:
    """A capability has to be imported before it can be used; this is the whole list."""
    imported: set[str] = set()
    for node in ast.walk(module_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    assert imported == EXPECTED_IMPORTS


def test_every_call_names_its_target_literally() -> None:
    """No call target is computed, so the two allowlists below see every call there is.

    Without this, `{"r": run_git}["r"](cwd, "worktree", "prune")` is a call whose func is
    neither a Name nor an Attribute, and both allowlists would walk straight past it.
    """
    computed = [
        (owner, ast.dump(call.func)[:60])
        for owner, call in calls_with_owner()
        if not isinstance(call.func, ast.Name | ast.Attribute)
    ]

    assert computed == []


def test_every_attribute_the_module_calls_is_a_pinned_read_only_one() -> None:
    called = {
        call.func.attr for _, call in calls_with_owner() if isinstance(call.func, ast.Attribute)
    }

    assert called == CALLED_ATTRIBUTES


def test_every_bare_name_call_is_a_module_function_or_a_pinned_builtin() -> None:
    called = {call.func.id for _, call in calls_with_owner() if isinstance(call.func, ast.Name)}

    assert called - module_functions() == CALLED_BUILTINS
    assert called <= module_functions() | CALLED_BUILTINS


def test_the_only_two_functions_that_start_a_process_are_run_git_and_the_gh_probe() -> None:
    """Confining the spawn is what makes the argv pins below cover EVERY command run.

    Owner-scoped rather than name-scoped: `subprocess` appears in `git_failure` too, as a
    type annotation, and a scan that counted that would have to special-case it.
    """
    spawners = {
        owner
        for owner, call in calls_with_owner()
        if isinstance(call.func, ast.Attribute) and call.func.attr == SPAWNING_ATTRIBUTE
    }

    assert spawners == {"run_git", "gh_open_pr_branches"}


def test_every_git_command_the_script_can_invoke_is_read_only() -> None:
    """Read out of the module's own syntax tree, so it covers paths no test exercises."""
    calls = [
        call
        for _, call in calls_with_owner()
        if isinstance(call.func, ast.Name) and call.func.id == "run_git"
    ]
    assert calls, "no run_git call sites found — the scan is looking in the wrong place"

    prefixes = {leading_literals(call) for call in calls}
    assert prefixes == READ_ONLY_GIT_ARGV_PREFIXES

    for prefix in prefixes:
        assert prefix[0] in sweep.GIT_SUBCOMMANDS
        if prefix[0] == "worktree":
            assert prefix[1] == "list", (
                "only `worktree list`; `remove` and `prune` are not read-only"
            )


def test_the_subcommand_set_the_prose_names_is_exactly_what_the_code_invokes() -> None:
    """BLOCKER 2: the prose said "the five commands"; the code invokes SIX (`rev-parse` omitted).

    GIT_SUBCOMMANDS is the single source of truth the module's comments now refer to instead of
    counting by hand. It is pinned here against the set DERIVED from the module's own syntax
    tree — the first literal after the cwd at each `run_git` call site — so an enumeration can
    never fall short of the code again: add a `run_git` subcommand and this fails until
    GIT_SUBCOMMANDS (and the prose that names it) catches up. The test file's own
    READ_ONLY_GIT_ARGV_PREFIXES is tied to the same source, so all three agree, and the round-22
    fix — derive the set rather than type it — is what this reuses.
    """
    from_ast = {
        leading_literals(call)[0]
        for _, call in calls_with_owner()
        if isinstance(call.func, ast.Name) and call.func.id == "run_git"
    }

    assert from_ast == set(sweep.GIT_SUBCOMMANDS), "the declared set is exactly what is invoked"
    assert from_ast == {prefix[0] for prefix in READ_ONLY_GIT_ARGV_PREFIXES}, (
        "and the test file's own pin agrees with it"
    )
    assert "rev-parse" in sweep.GIT_SUBCOMMANDS, (
        "the command that had gone missing is the one run FIRST at a registered path whose "
        "identity is not yet established"
    )


def test_rev_parse_is_the_one_subcommand_that_does_not_exit_129(fixture: Fixture) -> None:
    """BLOCKER 2: the exit-survey claimed `these commands` exit 129 on an unknown option.

    Measured here rather than quoted from a comment. Every subcommand in GIT_SUBCOMMANDS is run
    with an unknown option through this module's own `run_git`; five exit 129 and `rev-parse`
    exits 0 (git 2.53), which is why the not-run sentinels sit at 200/201 above any real exit.
    The arg map must cover GIT_SUBCOMMANDS exactly, so a subcommand added there without being
    measured fails this on the set assertion rather than passing blind.
    """
    unknown_option = {
        "worktree": ("worktree", "list", "--porcelain", "--nonesuch"),
        "status": ("status", "--porcelain", "--nonesuch"),
        "ls-files": ("ls-files", "-v", "--nonesuch"),
        "rev-list": ("rev-list", "--nonesuch", "HEAD"),
        "rev-parse": ("rev-parse", "--nonesuch"),
        "for-each-ref": ("for-each-ref", "--nonesuch"),
    }
    assert set(unknown_option) == set(sweep.GIT_SUBCOMMANDS), "every subcommand is measured"

    exits = {name: run_git(fixture.repo, *args).returncode for name, args in unknown_option.items()}

    assert exits["rev-parse"] == 0, "rev-parse ANSWERS an unknown option, it does not reject it"
    assert {name: code for name, code in exits.items() if name != "rev-parse"} == {
        "worktree": 129,
        "status": 129,
        "ls-files": 129,
        "rev-list": 129,
        "for-each-ref": 129,
    }, "the other five exit 129 — so the survey's 129 is true of five of the six, not all"
    assert sweep.EXIT_NOT_RUN not in exits.values()
    assert sweep.EXIT_TIMED_OUT not in exits.values()


def test_no_name_bound_to_run_git_can_escape_a_call_site() -> None:
    """The argv scan above collects calls whose func IS the name `run_git`.

    `g = run_git` followed by `g(cwd, "worktree", "prune")` would run a command that scan
    never sees, so the name may appear nowhere but in its own definition and in the func
    position of a call.
    """
    tree = module_ast()
    call_funcs = {id(call.func) for _, call in calls_with_owner(tree)}

    escaping = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "run_git" and id(node) not in call_funcs
    ]
    rebindings = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "run_git" and isinstance(node.ctx, ast.Store)
    ]

    assert escaping == [], "run_git is referenced somewhere other than a call"
    assert rebindings == [], "the name run_git is rebound"


def test_run_git_keywords_are_pin_alone_and_pin_is_passed_by_the_probe_reads() -> None:
    """Checks the keywords at run_git's call sites, and which calls pass `pin`."""
    sites = [
        (owner, leading_literals(call)[:1], {kw.arg for kw in call.keywords})
        for owner, call in calls_with_owner()
        if isinstance(call.func, ast.Name) and call.func.id == "run_git"
    ]
    assert sites, "no run_git call sites found — the scan is looking in the wrong place"

    assert set().union(*(names for _, _, names in sites)) <= {"pin"}
    probe_owners = {"probe_work_state", "index_survey"}
    assert {owner for owner, _, names in sites if "pin" in names} == probe_owners
    unpinned = [owner for owner, _, names in sites if owner in probe_owners and "pin" not in names]
    assert unpinned == []

    reads = [
        (owner, "pin" in names)
        for owner, sub, names in sites
        if sub in {("status",), ("ls-files",)}
    ]
    assert {owner for owner, _ in reads} == probe_owners, reads
    assert all(pinned for _, pinned in reads), reads


def test_the_gh_argv_is_pinned_in_full() -> None:
    """The one outbound call this tool makes, and the only spawn that is not git."""
    literals = [
        tuple(
            element.value if isinstance(element, ast.Constant) else None
            for element in call.args[0].elts
        )
        for owner, call in calls_with_owner()
        if owner == "gh_open_pr_branches"
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == SPAWNING_ATTRIBUTE
        and isinstance(call.args[0], ast.List)
    ]

    assert literals == [GH_ARGV]
    assert "gh pr list" in flat(sweep.__doc__), "the outbound call is disclosed"


def test_both_spawn_sites_bound_the_child_process() -> None:
    """An unbounded `subprocess.run` hangs forever on a stale mount or a `gh` awaiting auth."""
    bounded = [
        (owner, any(kw.arg == "timeout" for kw in call.keywords))
        for owner, call in calls_with_owner()
        if isinstance(call.func, ast.Attribute) and call.func.attr == SPAWNING_ATTRIBUTE
    ]

    assert sorted(bounded) == [("gh_open_pr_branches", True), ("run_git", True)]


def test_the_deletion_half_is_gone_from_the_api_and_the_command_line() -> None:
    """The rescue-then-retire half was removed, not merely left unreachable."""
    for name in (
        "copy_untracked_entry",
        "path_is_within",
        "rescue_destination",
        "rescue_readme",
        "rescue_root_conflict",
        "rescue_worktree",
        "retire",
        "worktree_remove_argv",
        "write_rescue",
    ):
        assert not hasattr(sweep, name), f"{name} survives"

    for flag in ("--delete", "--rescue-to"):
        with pytest.raises(SystemExit):
            build_arg_parser().parse_args([flag, "x"])
        assert flag not in build_arg_parser().format_help()


def test_the_syntax_tree_scans_need_no_repository() -> None:
    """The header used to COUNT these functions in prose, and the count was wrong.

    The number was never the load-bearing part: the property is that a scan which covers
    paths no repository exercises must not itself need a repository. So the set is computed
    from this file's own syntax tree and the property asserted, instead of narrated.
    """
    readers = {"module_ast", "calls_with_owner", "module_functions"}
    spawners = {"git", "git_may_fail", "init_repo", "main", "run_git", "subprocess"}

    scans: dict[str, tuple[set[str], set[str]]] = {}
    for node in ast.walk(ast.parse(Path(__file__).read_text())):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if used & readers:
            scans[node.name] = ({a.arg for a in node.args.args}, used)

    assert scans, "the scan is looking in the wrong place"
    assert {
        n: p & {"fixture", "tmp_path"} for n, (p, _) in scans.items() if p & {"fixture", "tmp_path"}
    } == {}
    assert {n: u & spawners for n, (_, u) in scans.items() if u & spawners} == {}


def test_a_run_leaves_a_stale_index_unwritten(fixture: Fixture) -> None:
    """`git status` refreshes a worktree's index when its stat cache is stale — a write.

    Measured, git 2.53: with a tracked file's mtime moved, plain `git status` rewrites
    `.git/worktrees/<name>/index`; under `--no-optional-locks` the refresh is skipped and the
    index is byte-identical after a run.
    """
    index = Path(git(fixture.stale, "rev-parse", "--git-path", "index"))
    assert index.is_absolute(), index
    os.utime(fixture.stale / "f.txt", (0, 0))
    before = index.read_bytes()

    assert_reads_named_nothing(fixture, fixture.stale.name)
    assert index.read_bytes() == before

    assert git(fixture.stale, "status", "--porcelain") == ""
    assert index.read_bytes() != before, "the cache really was stale: plain status rewrote it"


def test_a_newline_in_a_lock_reason_or_a_path_cannot_forge_a_row(fixture: Fixture) -> None:
    """The parser was made newline-safe with `-z`; the renderer still printed fields raw.

    Measured, git 2.53: `git worktree lock --reason` with a newline in the reason — an
    ordinary user command, no hand-editing of `.git` — rendered a complete, indistinguishable
    extra row for a path registered nowhere, and a worktree path holding a newline did the
    same. Every rendered field has its non-printables escaped. The forgery below is written in
    the LIVE column so that it stays a real forgery: a status that no longer exists could not
    be mistaken for a row by anyone, which would make the assertion pass on the wrong ground.
    """
    forged = "in use\nLIVE      /Users/victim/realwork\n          locked by git: forged"
    git(fixture.repo, "worktree", "lock", "--reason", forged, str(fixture.stale))
    evil = fixture.root / "evil\nLIVE      /Users/victim/other"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(evil), "main")

    rows = fixture.classify()
    out = render_report(rows, fixture.live(), frozenset())

    statuses = {MAIN, LIVE, DIRTY, BROKEN, MISSING}
    row_lines = [line for line in out.splitlines() if line.split("  ")[0] in statuses]
    assert len(row_lines) == len(rows), out
    assert not any(line.startswith("LIVE      /Users/victim") for line in out.splitlines())
    assert "locked by git: in use\\nLIVE" in out
    assert "evil\\nLIVE" in out


# ------------------------------------------- the environment and the config git reads from


def test_injected_git_config_and_index_do_not_reach_git(
    fixture: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_CONFIG_COUNT/KEY_n/VALUE_n inject config; GIT_INDEX_FILE swaps the index.

    Measured, git 2.53: an injected `core.fsmonitor` RUNS its value as a command during
    `git status`, `--no-optional-locks` notwithstanding; a GIT_INDEX_FILE that does not exist
    makes every tracked file report as deleted. Neither name was in the three-name scrub
    list, and the comment beside it called them benign.
    """
    marker = tmp_path / "PWNED"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch '{marker}'")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "no-such-index"))

    assert_reads_named_nothing(fixture, fixture.stale.name)
    assert not marker.exists()

    # The same environment handed to git unscrubbed: both effects are real.
    leaked = subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain"],
        cwd=str(fixture.stale),
        capture_output=True,
        text=True,
    )
    assert marker.exists(), "core.fsmonitor really runs its value"
    assert "D  f.txt" in leaked.stdout, "GIT_INDEX_FILE really fabricates status rows"


@pytest.mark.parametrize("variable", ["HOME", "XDG_CONFIG_HOME"])
def test_an_ambient_global_gitconfig_cannot_run_a_command(
    fixture: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """git reads global config from $HOME/.gitconfig and $XDG_CONFIG_HOME/git/config.

    NEITHER name begins with GIT_, so dropping that prefix — however carefully the prefix
    itself is chosen — cannot reach either one. Measured, git 2.53: each route on its own ran
    an injected `core.fsmonitor` command during `git status`. Pinning GIT_CONFIG_GLOBAL and
    GIT_CONFIG_SYSTEM to os.devnull empties the files git would read, without dropping HOME,
    which `gh` needs.
    """
    marker = tmp_path / f"PWNED_{variable}"
    root = tmp_path / f"cfg_{variable}"
    if variable == "HOME":
        root.mkdir()
        (root / ".gitconfig").write_text(f"[core]\n\tfsmonitor = touch '{marker}'\n")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        (root / "git").mkdir(parents=True)
        (root / "git" / "config").write_text(f"[core]\n\tfsmonitor = touch '{marker}'\n")
    monkeypatch.setenv(variable, str(root))

    assert_reads_named_nothing(fixture, fixture.stale.name)
    assert not marker.exists()

    subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain"],
        cwd=str(fixture.stale),
        capture_output=True,
        text=True,
    )
    assert marker.exists(), f"the {variable} route really does reach code execution"


def test_a_system_level_gitconfig_is_a_code_execution_route_at_all(
    fixture: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PREMISE for the third source, which the comment above GIT_CONFIG_PINS asserts of
    all three and only two tests measured.

    A file read AS the system config runs an injected `core.fsmonitor` during `git status`
    exactly as the two global files do — measured here by pointing GIT_CONFIG_SYSTEM at a file
    of this test's own, because writing to the machine's real system config is out of the
    question.

    This is a premise and NOT a pin: GIT_CONFIG_SYSTEM begins with GIT_, so the prefix scrub
    already keeps this particular file away from git whether GIT_CONFIG_PINS names it or not.
    What the pin uniquely closes is the file at git's BUILT-IN system path, which no
    environment variable reaches — and the test below is what pins that.
    """
    marker = tmp_path / "PWNED_SYSTEM"
    sysconf = tmp_path / "sysgitconfig"
    sysconf.write_text(f"[core]\n\tfsmonitor = touch '{marker}'\n")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(sysconf))

    assert_reads_named_nothing(fixture, fixture.stale.name)
    assert not marker.exists()

    subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain"],
        cwd=str(fixture.stale),
        capture_output=True,
        text=True,
    )
    assert marker.exists(), "a system-level config really does reach code execution"


def test_the_system_gitconfig_is_not_readable_under_this_modules_env(fixture: Fixture) -> None:
    """The THIRD config source the comment above GIT_CONFIG_PINS names, and the only one of
    the three that no other test reached.

    The two global routes are pinned above through HOME and XDG_CONFIG_HOME, which a test may
    move. The system file is at a path fixed by git's build prefix — `/opt/homebrew/etc/
    gitconfig` on the machine this was written on, not the `/etc/gitconfig` the module's own
    comment names — and no environment variable a test may set reaches it once the pin is in
    force, which is the point of the pin. Writing to the machine's real one is out of the
    question, so the pin is measured from the other end instead: whatever the system file
    holds, none of it may be READABLE through `run_git`. Reducing GIT_CONFIG_PINS to its
    global half left every other test in this file green; this is the one that goes red.

    The control is what keeps it from passing vacuously: where git answers with nothing at
    all — no system file on this machine — there is nothing for the pin to empty and the
    case is SKIPPED rather than counted as a pass.
    """
    control = subprocess.run(
        ["git", "config", "--system", "--list"],
        cwd=str(fixture.repo),
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    if control.returncode != 0 or not control.stdout.strip():
        pytest.skip("no system gitconfig is readable here, so the pin has nothing to empty")

    seen = run_git(fixture.repo, "config", "--system", "--list")
    assert seen.stdout == "", (
        "the system config is readable through run_git, so a `filter.*.clean` driver written "
        f"there would run during this tool's `git status`: {seen.stdout!r}"
    )

    # And the pin is what did it, not an absent file: the same read outside the pin answers.
    assert control.stdout.strip(), "the control really did read something"


def test_the_surveyed_repositorys_own_fsmonitor_is_not_executed(
    fixture: Fixture, tmp_path: Path
) -> None:
    """`core.fsmonitor` is a config key whose value `git status` EXECUTES, and config is layered.

    Emptying the ambient files leaves the repository's own `.git/config`, so the value is
    overridden on the command line instead, where it outranks every config file at once.
    Measured, git 2.53: `--no-optional-locks` does not stop it; `-c core.fsmonitor=` does.
    """
    marker = tmp_path / "PWNED_LOCAL"
    git(fixture.repo, "config", "core.fsmonitor", f"touch '{marker}'")

    assert_reads_named_nothing(fixture, fixture.stale.name)
    assert not marker.exists()

    subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain"],
        cwd=str(fixture.stale),
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    assert marker.exists(), "the repository's own config really does reach code execution"


def test_a_foreign_repository_at_a_registered_path_is_never_inspected(
    fixture: Fixture, tmp_path: Path
) -> None:
    """A registered path whose directory has been replaced by an unrelated repository.

    Measured, git 2.53: git does NOT flag such an entry prunable, so the probe used to run
    `git status` with `cwd=` inside it — executing that repository's own `core.fsmonitor`,
    and its own `filter.*.clean` driver when a file had to be re-hashed, and then printing
    ITS counts against the registered path. In this repository several sessions create
    worktrees at once and interrupted runs leave directories behind, so anything able to
    write under the worktrees parent reaches this.
    """
    fsmonitor = tmp_path / "PWNED_FOREIGN_FSMONITOR"
    clean = tmp_path / "PWNED_FOREIGN_FILTER"
    shutil.rmtree(fixture.stale)
    foreign = init_repo(fixture.stale)
    (foreign / ".gitattributes").write_text("*.txt filter=pwn\n")
    (foreign / "theirs.txt").write_text("AAAAAAAA\n")
    git(foreign, "add", ".gitattributes", "theirs.txt")
    git(foreign, "commit", "-qm", "foreign base")
    # Configured only now: `git add` above refreshes the index, which runs fsmonitor, so
    # setting these first would fire both markers during the fixture's own setup.
    git(foreign, "config", "core.fsmonitor", f"touch '{fsmonitor}'")
    git(foreign, "config", "filter.pwn.clean", f"touch '{clean}'; cat")
    (foreign / "untracked-of-theirs.txt").write_text("not ours\n")
    os.utime(foreign / "theirs.txt", (0, 0))
    assert not fsmonitor.exists() and not clean.exists(), "nothing has fired yet"

    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[fixture.stale.name]
    assert entry.prunable is False, "git itself does not flag the replaced directory"

    row = fixture.row(fixture.stale.name)

    assert row.status == BROKEN
    assert "not the one this repository records for it" in row.reason
    assert row.work is None, "no count may be read out of another repository"
    assert not fsmonitor.exists() and not clean.exists()

    # Both effects are real: the same commands, run there without the identity check.
    subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain", "-uall"],
        cwd=str(foreign),
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    assert fsmonitor.exists(), "the foreign repository's fsmonitor really does run"
    assert clean.exists(), "and so does its clean filter, which no -c flag disables"


def test_a_gitdir_outside_the_repository_that_claims_it_is_never_inspected(
    fixture: Fixture, tmp_path: Path
) -> None:
    """The forgery a `--git-common-dir` answer alone cannot see, and it reaches execution.

    Measured, git 2.53: with `commondir` naming the surveyed repository, asking only for that
    answer passed — git flags no such entry prunable either — and `git status` then read the
    forged gitdir's own `config.worktree`, whose `filter.pwn.clean` RAN, creating this marker.
    `extensions.worktreeConfig` is what makes that file readable at all: with it unset the
    same filter did not run (measured), and it is set on the surveyed repository here because
    it is set on this one.

    The answer is not another named mechanism: the gitdir ITSELF must be one of the surveyed
    repository's own, which bounds every file git reads out of it at once.
    """
    marker = tmp_path / "PWNED_FORGED_COMMONDIR"
    git(fixture.repo, "config", "extensions.worktreeConfig", "true")
    forged = forge_gitdir(fixture.stale, tmp_path / "forged-gitdir", fixture.repo)
    (forged / "config.worktree").write_text(f"[filter \"pwn\"]\n\tclean = touch '{marker}'; cat\n")
    (fixture.stale / ".gitattributes").write_text("*.txt filter=pwn\n")
    os.utime(fixture.stale / "f.txt", (0, 0))  # stat-dirty, so `git status` must re-hash it

    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[fixture.stale.name]
    assert entry.prunable is False, "git itself does not flag the repointed directory"
    assert not marker.exists(), "nothing has fired yet"

    row = fixture.row(fixture.stale.name)

    assert row.status == BROKEN
    assert row.work is None, "no count may be read out of a gitdir that is not ours"
    assert not marker.exists()

    # The effect is real: the same command, run there without the identity check.
    subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain", "-uall"],
        cwd=str(fixture.stale),
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    assert marker.exists(), "the forged gitdir's own clean filter really does run"


def foreign_repo_with_a_clean_filter(root: Path, marker: Path) -> Path:
    """A repository whose `filter.evil.clean` writes `marker`, plus a `skip-worktree` bit.

    The bit is on `hidden.txt` and is what an `ls-files` read of THIS index would count as a
    path git was told not to check, so a row naming one was read out of this repository.
    """
    foreign = init_repo(root)
    (foreign / ".gitattributes").write_text("*.txt filter=evil\n")
    (foreign / "f.txt").write_text("base\n")
    (foreign / "hidden.txt").write_text("hidden\n")
    git(foreign, "add", ".gitattributes", "f.txt", "hidden.txt")
    git(foreign, "commit", "-qm", "foreign base")
    git(foreign, "update-index", "--skip-worktree", "hidden.txt")
    (foreign / ".git" / "info").mkdir(exist_ok=True)
    (foreign / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
    git(foreign, "config", "filter.evil.clean", f"touch '{marker}'; cat")
    os.utime(foreign / "f.txt", (0, 0))  # stat-dirty, so a `git status` there must re-hash it
    assert not marker.exists(), "nothing has fired during the fixture's own setup"
    return foreign


def test_a_foreign_repository_renamed_in_after_the_identity_check(
    fixture: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity check passed on the directory that WAS there; the probe met another one.

    The probe's reads used to start new processes that found the repository afresh at the
    registered path, so a foreign repository renamed in between the two had its own clean
    filter executed and its own index counted on this entry's row.
    """
    marker = tmp_path / "PWNED_RENAMED_IN"
    foreign = foreign_repo_with_a_clean_filter(tmp_path / "foreign", marker)

    def rename_in() -> None:
        fixture.stale.rename(tmp_path / "moved-aside")
        foreign.rename(fixture.stale)

    race_after_identity(monkeypatch, fixture.stale, rename_in)

    row = fixture.row(fixture.stale.name)

    assert (fixture.stale / "hidden.txt").is_file(), "the race really ran"
    assert not marker.exists(), "the renamed-in repository's clean filter was executed"
    assert "git was told not to check" not in row.reason, f"its index was read: {row.reason}"
    assert row.work is not None
    assert row.work.status_lines == ("?? .gitattributes", "?? hidden.txt"), row.reason


def test_a_git_file_repointed_after_the_identity_check_is_never_followed(
    fixture: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race through the `.git` FILE: the directory stays, the gitdir it names moves.

    A process that re-reads `.git` at the registered path follows whatever it names by then,
    so the git directory the identity check verified is the one the probe must be given.
    """
    marker = tmp_path / "PWNED_REPOINTED"
    foreign = foreign_repo_with_a_clean_filter(tmp_path / "foreign", marker)
    (fixture.stale / "ours.txt").write_text("this registration's own untracked file\n")

    def repoint() -> None:
        (fixture.stale / ".git").write_text(f"gitdir: {foreign / '.git'}\n")

    race_after_identity(monkeypatch, fixture.stale, repoint)

    row = fixture.row(fixture.stale.name)

    assert str(foreign) in (fixture.stale / ".git").read_text(), "the race really ran"
    assert not marker.exists(), "the repointed gitdir's clean filter was executed"
    assert "git was told not to check" not in row.reason, f"its index was read: {row.reason}"
    assert row.work is not None and row.work.status_lines == ("?? ours.txt",), row.reason


def test_a_core_worktree_written_after_the_identity_check_is_never_followed(
    fixture: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes `core.worktree` into the entry's `config.worktree`."""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "planted.txt").write_text("not this worktree's\n")
    (fixture.stale / "ours.txt").write_text("this registration's own untracked file\n")
    git(fixture.repo, "config", "core.repositoryformatversion", "1")
    git(fixture.repo, "config", "extensions.worktreeConfig", "true")
    gitdir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))

    def redirect() -> None:
        (gitdir / "config.worktree").write_text(f"[core]\n\tworktree = {decoy}\n")

    race_after_identity(monkeypatch, fixture.stale, redirect)

    row = fixture.row(fixture.stale.name)

    assert str(decoy) in (gitdir / "config.worktree").read_text(), "the race really ran"
    assert row.work is not None and row.work.status_lines == ("?? ours.txt",), row.reason


def test_a_forged_gitdir_is_refused_with_no_config_of_its_own_at_all(
    fixture: Fixture, tmp_path: Path
) -> None:
    """The same class through a DIFFERENT member, so no config-shaped fix passes for it.

    A gitdir carries more than config: `git status` and `ls-files -v` both read ITS index,
    and its HEAD is the one git obeys at that path while `worktree list` still reports the
    REGISTRATION's. Both are set up and measured below, and neither needs
    `extensions.worktreeConfig`, which is left unset here — so a fix shaped around a config
    key closes neither.
    """
    # An empty commit: a different sha carrying the SAME tree, so swapping HEAD to it leaves
    # the index and the working tree agreeing with it and the silencing below intact.
    git(fixture.repo, "commit", "-q", "--allow-empty", "-m", "same tree, a new sha")
    twin = git(fixture.repo, "rev-parse", "HEAD")

    forged = forge_gitdir(fixture.stale, tmp_path / "forged-gitdir", fixture.repo)
    (forged / "HEAD").write_text(f"{twin}\n")
    (fixture.stale / "f.txt").write_text("REAL UNSAVED WORK\n")
    subprocess.run(
        ["git", "update-index", "--skip-worktree", "f.txt"],
        cwd=str(fixture.stale),
        capture_output=True,
        env={**os.environ, "GIT_DIR": str(forged), "GIT_WORK_TREE": str(fixture.stale)},
    )

    assert git(fixture.stale, "status", "--porcelain", "-uall") == "", (
        "the forged index really does silence git about the unsaved work"
    )
    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[fixture.stale.name]
    assert entry.head != git(fixture.stale, "rev-parse", "HEAD"), (
        "and the row's HEAD and the HEAD git obeys there really are two different answers"
    )

    row = fixture.row(fixture.stale.name)

    assert row.status == BROKEN
    assert row.work is None, "neither the forged index nor the forged HEAD is read"


def test_a_worktree_repointed_at_the_repositorys_own_gitdir_is_refused(
    fixture: Fixture,
) -> None:
    """A gitdir that IS this repository's own, and is not this entry's: the common directory.

    A worktree's `.git` is an ordinary text file inside the worktree, so whatever writes that
    directory chooses which gitdir git answers there — and nothing is forged by naming the
    repository's own common directory. `git status` then compares this worktree's files
    against the MAIN checkout's HEAD and index, while `worktree list` goes on reporting this
    entry's own HEAD, so the commit test and the file test read two different registrations'
    state. Measured, git 2.53: the row read DIRTY through its own gitdir a moment earlier and
    named nothing at all after the repoint.
    """
    edit = "work of its own that main happens to carry too\n"
    (fixture.repo / "f.txt").write_text(edit)
    git(fixture.repo, "add", "f.txt")
    git(fixture.repo, "commit", "-qm", "main moves on")
    (fixture.stale / "f.txt").write_text(edit)

    before = fixture.row(fixture.stale.name)
    assert before.status == DIRTY, "through its own gitdir the edit is there"
    assert before.work is not None and before.work.status_lines == (" M f.txt",)

    repo_common = os.path.realpath(fixture.repo / ".git")
    (fixture.stale / ".git").write_text(f"gitdir: {repo_common}\n")

    assert sweep.git_dir(fixture.stale) == repo_common, "git really does read the common dir"
    assert git(fixture.stale, "status", "--porcelain", "-uall") == "", (
        "and really does fall silent, reading the main checkout's HEAD and index"
    )
    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[fixture.stale.name]
    assert entry.head != git(fixture.stale, "rev-parse", "HEAD"), (
        "the registered HEAD and the HEAD git obeys there are two different answers"
    )

    row = fixture.row(fixture.stale.name)
    assert row.status == BROKEN
    assert row.work is None, "no count may be read out of another registration's gitdir"


def test_a_worktree_repointed_at_a_sibling_worktrees_gitdir_is_refused(
    fixture: Fixture,
) -> None:
    """The same class through the OTHER arm: a per-worktree gitdir, belonging to a sibling.

    Being under `…/.git/worktrees/` says the gitdir is the repository's; it says nothing
    about WHOSE. Measured, git 2.53: pointed at a sibling's gitdir, `git status` answers off
    the sibling's HEAD and index while this worktree's own edit sits on disk, and the row
    that had NAMED the edit named nothing.
    """
    edit = "work of its own the sibling happens to carry too\n"
    (fixture.repo / "f.txt").write_text(edit)
    git(fixture.repo, "add", "f.txt")
    git(fixture.repo, "commit", "-qm", "main moves on")
    sibling = fixture.root / "wt-sibling"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(sibling), "main")
    (fixture.stale / "f.txt").write_text(edit)

    before = fixture.row(fixture.stale.name)
    assert before.status == DIRTY
    assert before.work is not None and before.work.status_lines == (" M f.txt",)

    repo_common = os.path.realpath(fixture.repo / ".git")
    siblings_gitdir = git(sibling, "rev-parse", "--path-format=absolute", "--git-dir")
    (fixture.stale / ".git").write_text(f"gitdir: {siblings_gitdir}\n")

    reads = sweep.git_dir(fixture.stale)
    assert reads == os.path.realpath(siblings_gitdir), "git reads the sibling's gitdir"
    assert reads.startswith(os.path.join(repo_common, "worktrees") + os.sep), (
        "which is one of this repository's own, so nothing about it is foreign"
    )
    assert git(fixture.stale, "status", "--porcelain", "-uall") == "", (
        "and git falls silent, reading the sibling's HEAD and index"
    )

    row = fixture.row(fixture.stale.name)
    assert row.status == BROKEN
    assert row.work is None, "no count may be read out of a sibling registration's gitdir"


def test_a_registered_path_replaced_by_a_symlink_to_a_sibling_is_refused(
    fixture: Fixture,
) -> None:
    """The member of this class nobody named: two registrations resolving to one directory.

    Nothing is repointed here and nothing is forged — the symlink makes git read the
    SIBLING's own `.git` file, so the gitdir, the HEAD and the index git uses are all
    genuinely this repository's AND genuinely bound to a registered path. Just not to this
    one. Measured, git 2.53: `--show-toplevel` and `os.path.realpath` of the registered path
    both answer the sibling's directory, because resolving is exactly what collapses the two
    registrations, and the row was written off the sibling's state while its own registered
    HEAD was another commit.
    """
    (fixture.repo / "f.txt").write_text("main moved on\n")
    git(fixture.repo, "add", "f.txt")
    git(fixture.repo, "commit", "-qm", "main moves on")
    sibling = fixture.root / "wt-sibling"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(sibling), "main")
    (fixture.stale / "f.txt").write_text("REAL UNSAVED WORK\n")
    assert_reads_found(fixture, fixture.stale.name, "carries 1 modified/untracked path(s)")

    shutil.rmtree(fixture.stale)
    fixture.stale.symlink_to(sibling, target_is_directory=True)

    repo_common = os.path.realpath(fixture.repo / ".git")
    assert sweep.work_tree_root(fixture.stale) == os.path.realpath(fixture.stale) == str(sibling), (
        "both halves of the working-tree comparison answer the SIBLING's directory"
    )
    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[fixture.stale.name]
    assert entry.head != git(sibling, "rev-parse", "HEAD"), (
        "while the registration still carries this entry's own HEAD"
    )

    assert refusal(fixture.stale, repo_common) != ""
    assert fixture.row(fixture.stale.name).status == BROKEN


def test_an_ordinary_linked_worktree_is_still_classified_on_its_own_state(
    fixture: Fixture,
) -> None:
    """The boring case: a plain `git worktree add`, relative path, default flags.

    Run from a directory that is not the repository root, because that is where a relative
    path is resolved. Refusing everything closes the hole above too, so this is the half of
    the fix that has to be measured: the row is read, and read on what it actually carries.
    """
    inner = fixture.repo / "deep" / "er"
    inner.mkdir(parents=True)
    git(inner, "worktree", "add", "-q", "--detach", "../../../wt-plain", "main")
    plain = fixture.root / "wt-plain"

    repo_common = os.path.realpath(fixture.repo / ".git")
    recorded = os.path.realpath(fixture.repo / ".git" / "worktrees" / plain.name)
    assert establish_identity(plain, repo_common) == ((recorded, os.path.realpath(plain)), "")
    assert_reads_named_nothing(fixture, plain.name, "nothing here refuses it:")

    (plain / "u.txt").write_text("unsaved\n")
    row = fixture.row(plain.name)
    assert row.reason == "carries 1 modified/untracked path(s)", (
        "and its own state is what it is classified on"
    )
    assert row.work is not None and row.work.status_lines == ("?? u.txt",)


def test_a_worktree_added_through_a_symlink_or_a_dots_path_is_still_read(
    fixture: Fixture,
) -> None:
    """The two other shapes an `add` path can arrive in, and git resolves both when it records.

    So the record and the path `worktree list` reports stay one string, and comparing them
    needs no resolving of its own. Measured, git 2.53: both are read and classified, not
    refused.
    """
    link = fixture.root / "link-to-root"
    link.symlink_to(fixture.root, target_is_directory=True)
    (fixture.root / "x").mkdir()
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(link / "wt-via-symlink"), "main")
    git(
        fixture.repo,
        "worktree",
        "add",
        "-q",
        "--detach",
        str(fixture.root / "x" / ".." / "wt-via-dots"),
        "main",
    )

    repo_common = os.path.realpath(fixture.repo / ".git")
    for name in ("wt-via-symlink", "wt-via-dots"):
        entry = {e.path.name: e for e in list_worktrees(fixture.repo)}[name]
        assert establish_identity(entry.path, repo_common)[1] == "", name
        assert_reads_named_nothing(fixture, name, name)
        (entry.path / "u.txt").write_text("unsaved\n")
        assert_reads_found(fixture, name, "carries 1 modified/untracked path(s)", name)


#: Trailing code points a DIRECTORY NAME may legally end in that `str.strip()` also removes,
#: so stripping more than the one LF git appends changes which path the identity check
#: compares.
#:
#: DERIVED, never listed. A hand-typed spelling of this table shipped SHORT, and every member
#: it omitted sat in the half that reaches the second read — the half the table exists to
#: reach. A longer hand-typed list is the same defect postponed, so what is typed here is the
#: DEFINITION the shipped trimming answers to, and Python enumerates the members: nobody can
#: leave one out, because nobody types one.
#:
#: The definition is `str.strip()` itself, because `str.strip()` is precisely what
#: `path_answer` must not do. That the two coincide — the set `str.strip` acts on, and the set
#: on which the defect and the fix disagree — is proved over the whole of Unicode by
#: `test_the_trailer_set_is_exactly_where_strip_and_removesuffix_disagree`, not assumed here.
#:
#: The pytest id IS the code point, so there is no label column to get wrong either.
STRIP_TRAILERS = tuple(c for c in map(chr, range(sys.maxunicode + 1)) if c.strip() == "")
STRIP_IDS = [f"U{ord(c):04X}" for c in STRIP_TRAILERS]


@pytest.fixture(scope="session")
def kept_in_worktree_name(tmp_path_factory: pytest.TempPathFactory) -> frozenset[str]:
    """Which of `STRIP_TRAILERS` git KEEPS when it derives a worktree NAME — measured, once.

    This split decides WHICH read an over-strip breaks. Where git REWRITES the code point,
    only `work_tree_root` ever sees it, because the gitdir path then carries no trailing
    whitespace at all. Where git KEEPS it, the gitdir path ends in it too, so `git_dir` also
    answers a path a strip shortens — `recorded_worktree` then reads out of a directory that
    does not exist and the row reads BROKEN over an intact registration.

    MEASURED here rather than declared beside the table, because a declared split is what went
    wrong: a git that started rewriting a KEPT member — or keeping a REWRITTEN one — would
    move a case from one half of the parametrisation to the other with nothing failing.

    Read off the DIRECTORY ENTRIES git created under `worktrees/`, not out of any answer git
    printed: what the split has to be measured against is git's naming, and every stdout route
    to it runs through the very trimming this table exists to pin. `git worktree add` is
    asserted to succeed for every member on the way past (the `git` helper raises otherwise),
    so a member git refused could not sit silently absent from both halves.

    Session-scoped: one repository and one `worktree add` per member for the whole file.
    """
    root = tmp_path_factory.mktemp("kept-split")
    repo = init_repo(root / "repo")
    (repo / "f.txt").write_text("base\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-qm", "base")

    kept = set()
    for trailer, label in zip(STRIP_TRAILERS, STRIP_IDS, strict=True):
        odd = root / f"keep-{label}{trailer}"
        git(repo, "worktree", "add", "-q", "--detach", str(odd), "main")
        names = os.listdir(repo / ".git" / "worktrees")
        matching = [n for n in names if n.startswith(f"keep-{label}")]
        assert len(matching) == 1, f"{label}: {matching}"
        if matching[0].endswith(trailer):
            kept.add(trailer)
    return frozenset(kept)


@pytest.mark.parametrize("trailer", STRIP_TRAILERS, ids=STRIP_IDS)
def test_a_directory_name_ending_in_whitespace_is_read_not_called_broken(
    fixture: Fixture, trailer: str, kept_in_worktree_name: frozenset[str]
) -> None:
    """Every path git prints gets the one LF git appends removed, and nothing else.

    Measured, git 2.53: `str.strip()` took the trailing whitespace of the DIRECTORY NAME with
    it, the comparison in the identity check then failed, and the row read BROKEN — while the
    directory was on disk, the registration was intact, and git had answered with the
    registered path.

    Both reads are exercised because git splits these code points in two. Measured, git 2.53:
    `--show-toplevel` carries the trailing code point for every member, while `--git-dir`
    carries it only for the members git KEEPS in the worktree NAME.

    That split is why fixing one call site was not fixing the defect. Measured, git 2.53, with
    `work_tree_root` already trimming correctly and `git_dir` alone put back on `.strip()`: the
    REWRITTEN half still read DIRTY with the reason below, and the KEPT half read BROKEN with
    the reason for a RECORD that does not match — which does not mention `core.worktree`, so a
    test pinning that string would have watched the whole KEPT half go by.

    The split comes from `kept_in_worktree_name`, which measures git's own directory entries,
    so checking the `--git-dir` answer against it is two independent observations agreeing
    rather than one observation restated.
    """
    label = f"U{ord(trailer):04X}"
    kept_in_name = trailer in kept_in_worktree_name
    odd = fixture.root / f"wt-{label}{trailer}"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(odd), "main")

    assert odd.is_dir(), f"{label}: the directory really is on disk"
    assert odd in fixture.registrations(), f"{label}: and registered under that exact name"

    gitdir = sweep.git_dir(odd) or ""
    assert os.path.basename(gitdir).endswith(trailer) == kept_in_name, (
        f"{label}: git {'keeps' if kept_in_name else 'rewrites'} it in the worktree name, "
        f"so the gitdir is {gitdir!r}"
    )

    repo_common = os.path.realpath(fixture.repo / ".git")
    recorded = sweep.recorded_worktree(gitdir)
    assert recorded == str(odd), f"{label}: the gitdir half matches: {recorded!r}"
    assert sweep.work_tree_root(odd) == os.path.realpath(odd), (
        f"{label}: and git answers with the registered path, trailing whitespace and all"
    )
    assert establish_identity(odd, repo_common)[1] == "", label

    row = fixture.row(odd.name)
    assert row.status == DIRTY, f"{label}: {row.status}: {row.reason}"
    assert "core.worktree" not in row.reason, f"{label}: {row.reason}"
    assert row.reason == UNLINKED_REASON, f"{label}: {row.reason}"

    # And the row really is reading THIS directory, not merely declining to refuse it.
    (odd / "u.txt").write_text("unsaved\n")
    assert_reads_found(fixture, odd.name, "carries 1 modified/untracked path(s)", label)


def test_the_trailer_set_is_exactly_where_strip_and_removesuffix_disagree() -> None:
    """Deriving the table is the whole guard against it going short, so prove the derivation.

    Scanned over the WHOLE of Unicode rather than sampled: the claim is that nothing is
    missing, and a sample cannot make that claim. On a path git printed, `str.strip()` and the
    shipped `str.removesuffix("\\n")` differ exactly when the directory name's last code point
    is one `str.strip()` removes — so the set derived above and the set on which the defect and
    the fix disagree are the same set, and deriving from either can leave nothing out.

    LF is also the member that separates the shipped spelling from `str.rstrip("\\n")`, which
    takes git's newline and the NAME's own together. Asserted rather than remarked on, because
    without LF in the parametrisation no behavioural test tells the fix from `rstrip` and the
    only red left is a spelling pin over an attribute name.
    """
    printed = "/p/wt{}\n".format
    disagree = tuple(
        c
        for c in map(chr, range(sys.maxunicode + 1))
        if printed(c).strip() != printed(c).removesuffix("\n")
    )
    assert disagree == STRIP_TRAILERS
    assert "\n" in STRIP_TRAILERS
    assert printed("\n").rstrip("\n") != printed("\n").removesuffix("\n")


def test_both_halves_of_the_trailer_split_are_populated(
    kept_in_worktree_name: frozenset[str],
) -> None:
    """Neither half may empty out, or the parametrisation stops reaching one of the reads.

    A git that kept every member would leave the REWRITTEN half — the `work_tree_root`-only
    route — unreached; one that rewrote every member would leave the KEPT half, the one where
    an over-strip reaches `git_dir` and prints BROKEN, unreached. Either way every case above
    would still be green while covering half of what it says it covers.

    The boundary is the one `path_answer`'s prose names: measured, git 2.53, git rewrites the
    ASCII members and keeps the rest. Checked here rather than remembered there.
    """
    kept = kept_in_worktree_name
    rewritten = frozenset(STRIP_TRAILERS) - kept
    assert kept, "no member survives into a worktree name; the `git_dir` half is unreached"
    assert rewritten, "every member survives; the `work_tree_root`-only half is unreached"
    assert kept | rewritten == frozenset(STRIP_TRAILERS)
    assert rewritten == {c for c in STRIP_TRAILERS if ord(c) < 0x80}, sorted(
        f"U{ord(c):04X}" for c in rewritten
    )


#: The two layouts measured to put a trailing whitespace on what `--git-common-dir` PRINTS.
#: The ordinary layout cannot, which is why `STRIP_TRAILERS` above reaches only two of the three
#: path reads: measured, git 2.53, for a repository directory named `repo `, raw stdout here
#: is `…/repo /.git\n` — the answer ends in the git directory's own name, so `.strip()` and
#: `removesuffix("\n")` return the same string and no row moves. These two put the oddly
#: named directory LAST in the answer instead, and there a strip shortens it.
COMMON_DIR_SHAPES = (
    # In a BARE repository `--git-common-dir` answers the repository directory itself.
    "bare",
    # With `--separate-git-dir` it answers that separate directory, wherever it was put.
    "separate-git-dir",
)


def repo_whose_common_dir_ends_in(root: Path, shape: str, trailer: str) -> Path:
    """A repository to survey whose COMMON DIRECTORY path ends in `trailer`.

    Built for real both ways rather than forced through config, because what is being pinned
    is what git PRINTS for `--git-common-dir`, not what this module does with it afterwards.
    """
    if shape == "separate-git-dir":
        separate = root / f"gitdir{trailer}"
        repo = init_repo(root / "work", f"--separate-git-dir={separate}")
    else:
        repo = init_repo(root / f"bare{trailer}", "--bare")
        seed = init_repo(root / "seed")
        (seed / "f.txt").write_text("base\n")
        git(seed, "add", "f.txt")
        git(seed, "commit", "-qm", "base")
        git(seed, "push", "-q", str(repo), "main")
        return repo
    (repo / "f.txt").write_text("base\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-qm", "base")
    return repo


@pytest.mark.parametrize("shape", COMMON_DIR_SHAPES)
def test_a_common_dir_ending_in_whitespace_does_not_condemn_every_linked_worktree(
    tmp_path: Path, shape: str
) -> None:
    """The third path read, pinned at a shape where an over-strip is observable.

    `common_dir` answers what every gitdir is measured AGAINST, so a strip here misreads no
    single worktree: it moves the one `worktrees/` prefix `owned_gitdir` compares every entry
    with. Two linked worktrees are registered below, and neither carries any whitespace in its
    own name, so neither of the other two path reads is in play on either of them.

    Measured, git 2.53, on both shapes: raw stdout for `--git-common-dir` ends in the trailing
    SPACE, and `.strip()` there returns a path one character shorter than `removesuffix("\\n")`
    does. With `common_dir` alone put back on `.strip()`, BOTH rows read BROKEN naming the
    RECORD — so the verdicts are asserted before the reads that produced them, and it is that
    assertion the strip fails.
    """
    trailer = " "
    root = tmp_path / "fx"
    repo = repo_whose_common_dir_ends_in(root, shape, trailer)

    # The premise, read straight off git and so unmoved by anything this module does with it.
    printed = run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert printed.stdout.endswith(f"{trailer}\n"), f"{shape}: git printed {printed.stdout!r}"
    assert printed.stdout.strip() != printed.stdout.removesuffix("\n"), (
        f"{shape}: the two spellings really do differ here: {printed.stdout!r}"
    )

    linked = [root / "linked-plain", root / "linked-other"]
    for path in linked:
        assert not any(char.isspace() for char in path.name), "its own name is an ordinary one"
        git(repo, "worktree", "add", "-q", "--detach", str(path), "main")
        (path / "u.txt").write_text("unsaved\n")

    entries = list_worktrees(repo)
    rows = {
        row.worktree.path.name: row
        for row in classify_worktrees(
            repo,
            entries,
            main_path=main_worktree_path(entries),
            live=LiveSet(shas=frozenset()),
            live_only=frozenset(),
        )
    }
    for path in linked:
        row = rows[path.name]
        assert row.status == DIRTY, f"{shape}/{path.name}: {row.status}: {row.reason}"
        assert row.reason == "carries 1 modified/untracked path(s)", f"{shape}: {row.reason}"

    repo_common = sweep.common_dir(repo)
    assert repo_common is not None and repo_common.endswith(trailer), (
        f"{shape}: and the read behind those rows kept the trailing character: {repo_common!r}"
    )
    for path in linked:
        assert establish_identity(path, repo_common)[1] == "", f"{shape}/{path.name}"


#: The two reads the identity check compares, and a way to make each ANSWER NOTHING while the
#: registration stays intact and unprunable. Neither route is a mismatch — git did not report
#: a differing value, it reported none — so on both of them the mismatch sentence asserts a
#: comparison that never happened, and on the second it also names `core.worktree`, which is
#: not what was set.
UNANSWERABLE_READS = (
    # `<path>/.git` exists, so `worktree list` does not call the entry prunable, but it is
    # not a gitfile git can follow: `rev-parse --git-dir` exits non-zero.
    ("git_dir", "gitfile", ""),
    # `--git-dir` still answers; `core.bare` in the worktree's own config is what leaves
    # `--show-toplevel` with no working tree to report.
    ("work_tree_root", "config", "core.bare"),
)


@pytest.mark.parametrize(
    ("read", "how", "key"), UNANSWERABLE_READS, ids=[r for r, _, _ in UNANSWERABLE_READS]
)
def test_a_read_git_could_not_answer_is_not_reported_as_a_mismatch(
    fixture: Fixture, read: str, how: str, key: str
) -> None:
    """A row may not say git READ something different; only that git answered nothing.

    The mismatch sentence asserts a comparison that never happened — and on the
    `work_tree_root` route it goes further and names `core.worktree`, a mechanism not in
    play. Measured, git 2.53: `rev-parse` exits 128 on both routes below.

    BROKEN is still the right verdict — a directory this tool cannot establish the identity
    of is not inspected either way. What changes is only what the row CLAIMS about why.
    """
    git(fixture.repo, "config", "extensions.worktreeConfig", "true")
    entry = fixture.root / f"wf_unanswerable-{read}"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(entry), "main")

    if how == "gitfile":
        (entry / ".git").write_text("this is not a gitfile\n")
    else:
        git(entry, "config", "--worktree", key, "true")

    listed = {e.path.name: e for e in list_worktrees(fixture.repo)}[entry.name]
    assert not listed.prunable, "the registration is intact, so BROKEN is not reached early"
    assert entry.is_dir(), "and the directory is on disk, so MISSING is not either"

    answered = getattr(sweep, read)(entry)
    assert answered is None, f"{read} really did come back with nothing: {answered!r}"

    problem = refusal(entry, os.path.realpath(fixture.repo / ".git"))
    assert problem != ""
    assert "is not the one this repository records" not in problem, problem
    assert "core.worktree" not in problem, problem
    assert "could not" in problem, problem

    row = fixture.row(entry.name)
    assert row.status == BROKEN, f"{row.status}: {row.reason}"
    assert row.reason == problem
    assert row.work is None
    assert not any(word in row.reason for word in CLEARANCE_WORDS), row.reason


def test_a_relative_gitdir_record_names_the_same_worktree(fixture: Fixture) -> None:
    """`worktree.useRelativePaths` writes the record relative to the gitdir it sits in.

    Measured, git 2.53: the record then reads `../../../../<name>/.git`, and `worktree list`
    goes on reporting the absolute path — so the two are compared only after the record is
    joined to its gitdir. Collapsing that join lexically is also why `os.path.normpath` and
    not `os.path.realpath` is enough to do it.
    """
    git(fixture.repo, "config", "worktree.useRelativePaths", "true")
    entry = fixture.root / "wt-relative"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(entry), "main")

    gitdir = git(entry, "rev-parse", "--path-format=absolute", "--git-dir")
    assert (Path(gitdir) / "gitdir").read_text().strip().startswith(".."), (
        "the record really is relative, so the join is not vacuous"
    )

    assert sweep.recorded_worktree(gitdir) == str(entry)
    assert establish_identity(entry, os.path.realpath(fixture.repo / ".git"))[1] == ""
    assert_reads_named_nothing(fixture, entry.name)

    (entry / "u.txt").write_text("unsaved\n")
    assert_reads_found(fixture, entry.name, "carries 1 modified/untracked path(s)")


def test_a_record_written_through_a_symlinked_parent_is_read_not_refused(
    fixture: Fixture,
) -> None:
    """The other half of `normpath`: what resolving symlinks as well would cost.

    The record here is hand-written with a symlink component in it, and `worktree list` reads
    the registration's path back out of that same file. Measured, git 2.53: this row is read
    and classified on its own state, while the `realpath` variant refuses it as BROKEN — a
    false BROKEN over a worktree that is registered exactly as it is recorded.
    """
    real = fixture.root / "real"
    real.mkdir()
    link = fixture.root / "link-to-real"
    link.symlink_to(real, target_is_directory=True)
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(real / "wt"), "main")
    gitdir = git(real / "wt", "rev-parse", "--path-format=absolute", "--git-dir")
    (Path(gitdir) / "gitdir").write_text(f"{link / 'wt' / '.git'}\n")

    entry = {e.path.name: e for e in list_worktrees(fixture.repo)}["wt"]
    assert str(entry.path) == str(link / "wt"), "the registration is read back through the link"
    assert os.path.realpath(entry.path) == str(real / "wt"), "which really is a symlinked path"

    assert sweep.recorded_worktree(gitdir) == str(entry.path), "and so the two match"
    assert establish_identity(entry.path, os.path.realpath(fixture.repo / ".git"))[1] == ""
    assert_reads_named_nothing(fixture, "wt")

    (entry.path / "u.txt").write_text("unsaved\n")
    row = fixture.row("wt")
    assert row.reason == "carries 1 modified/untracked path(s)", (
        "classified on its own state, not refused"
    )
    assert row.work is not None and row.work.status_lines == ("?? u.txt",)


def test_a_symlink_in_the_registered_path_retargeted_after_the_identity_check_is_not_read(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retargets a symlink in the registered path once the identity check returns."""
    real = fixture.root / "real"
    real.mkdir()
    link = fixture.root / "link-to-real"
    link.symlink_to(real, target_is_directory=True)
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(real / "wt"), "main")
    gitdir = git(real / "wt", "rev-parse", "--path-format=absolute", "--git-dir")
    (Path(gitdir) / "gitdir").write_text(f"{link / 'wt' / '.git'}\n")
    elsewhere = fixture.root / "elsewhere"
    (elsewhere / "wt").mkdir(parents=True)
    (elsewhere / "wt" / "planted.txt").write_text("not this worktree's\n")
    registered = link / "wt"
    assert registered in fixture.registrations()

    real_check = sweep.establish_identity

    def checking_then_retargeting(
        path: Path, repo_common: str
    ) -> tuple[tuple[str, str] | None, str]:
        answer = real_check(path, repo_common)
        if path == registered:
            link.unlink()
            link.symlink_to(elsewhere, target_is_directory=True)
        return answer

    monkeypatch.setattr(sweep, "establish_identity", checking_then_retargeting)

    row = fixture.row("wt")

    assert (registered / "planted.txt").is_file(), "the race really ran"
    assert row.reason == UNLINKED_REASON, row.reason
    assert row.work is not None and row.work.status_lines == ()


def test_a_symlink_retargeted_into_the_verified_tree_still_has_its_whole_index_read(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retargets a symlink in the registered path to a directory inside the verified tree."""
    real = fixture.root / "real"
    real.mkdir()
    link = fixture.root / "link-to-real"
    link.symlink_to(real, target_is_directory=True)
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(real / "wt"), "main")
    gitdir = git(real / "wt", "rev-parse", "--path-format=absolute", "--git-dir")
    (Path(gitdir) / "gitdir").write_text(f"{link / 'wt' / '.git'}\n")
    git(real / "wt", "update-index", "--skip-worktree", "f.txt")
    (real / "wt" / "sub" / "wt").mkdir(parents=True)
    registered = link / "wt"
    assert registered in fixture.registrations()

    real_check = sweep.establish_identity

    def checking_then_retargeting(
        path: Path, repo_common: str
    ) -> tuple[tuple[str, str] | None, str]:
        answer = real_check(path, repo_common)
        if path == registered:
            link.unlink()
            link.symlink_to(real / "wt" / "sub", target_is_directory=True)
        return answer

    monkeypatch.setattr(sweep, "establish_identity", checking_then_retargeting)

    row = fixture.row("wt")

    assert registered.resolve() == (real / "wt" / "sub" / "wt").resolve(), "the race really ran"
    assert row.reason == "carries 1 path(s) git was told not to check", row.reason


def test_a_gitdir_that_records_nothing_is_refused_rather_than_raised(fixture: Fixture) -> None:
    """A per-worktree gitdir with no readable record — the shape a concurrent removal leaves.

    Worktrees here are created and removed by several sessions at once, so the gitdir git
    answers with can lose its record between one read and the next. Measured, git 2.53:
    `worktree list` drops a registration whose OWN record is unreadable, so such an entry is
    never classified in its own right — it is reached the way it is reached in flight, through
    a worktree pointed at a SIBLING's gitdir whose record has since gone.

    A real sibling rather than an invented empty directory, because git has to keep answering:
    with an empty one it refuses to read at all, `git_dir` returns None, and the refusal lands
    on the `reads is None` arm with the record comparison short-circuited away. Here both
    other arms PASS — the gitdir is one of this repository's own, and git still answers
    `--show-toplevel` with this directory — so the comparison is the only thing refusing.
    """
    sibling = fixture.root / "wt-sibling"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(sibling), "main")
    siblings_gitdir = git(sibling, "rev-parse", "--path-format=absolute", "--git-dir")
    (fixture.stale / ".git").write_text(f"gitdir: {siblings_gitdir}\n")
    (Path(siblings_gitdir) / "gitdir").unlink()

    repo_common = os.path.realpath(fixture.repo / ".git")
    reads = sweep.git_dir(fixture.stale)
    assert reads == os.path.realpath(siblings_gitdir), "git still answers, and with the sibling's"
    assert sweep.owned_gitdir(reads, repo_common), "which is one of this repository's own"
    assert sweep.work_tree_root(fixture.stale) == os.path.realpath(fixture.stale), (
        "and git reads THIS directory there, so the core.worktree arm accepts it too"
    )
    assert sweep.recorded_worktree(reads) is None, "only the record has gone"

    row = fixture.row(fixture.stale.name)
    assert row.status == BROKEN
    assert row.work is None

    # An EMPTY record needs no guard of its own: it yields the gitdir's own path, which is
    # not the registered one either, so the same comparison refuses the row.
    (Path(reads) / "gitdir").write_text("")
    assert sweep.recorded_worktree(reads) == reads
    assert fixture.row(fixture.stale.name).status == BROKEN


def test_the_main_checkout_is_excluded_before_the_identity_check_asks_about_it(
    fixture: Fixture,
) -> None:
    """The identity check refuses the main checkout, so the order it runs in is load-bearing.

    The main checkout's gitdir is the common directory itself, which is under no
    `worktrees/` and records no worktree of its own — that record is a per-worktree file.
    `classify_worktrees` never asks: excluding the main checkout is its first decision.
    """
    repo_common = os.path.realpath(fixture.repo / ".git")
    assert sweep.git_dir(fixture.repo) == repo_common
    assert not sweep.owned_gitdir(repo_common, repo_common), (
        "the common directory is not one of the PER-WORKTREE git directories"
    )
    assert refusal(fixture.repo, repo_common) != "", (
        "asked directly, the identity check refuses the main checkout"
    )
    assert fixture.rows()["repo"] == MAIN, "so the exclusion has to come first, and does"


def test_a_gitdir_beside_the_repositorys_worktrees_directory_is_not_one_of_its_own(
    fixture: Fixture,
) -> None:
    """`…/.git/worktrees-elsewhere/wt` shares every character of the prefix and no component.

    The comparison ends at a separator for this reason: a bare string prefix accepts any
    directory whose name merely starts with `worktrees`. This is also where the two halves of
    the check are shown to be independent: the forgery records THIS worktree, so the record
    comparison alone accepts it and only the component test refuses it.
    """
    repo_common = os.path.realpath(fixture.repo / ".git")
    forged = forge_gitdir(
        fixture.stale, Path(repo_common) / "worktrees-elsewhere" / "wt", fixture.repo
    )

    assert (forged / "gitdir").read_text().strip() == f"{fixture.stale / '.git'}"
    assert sweep.recorded_worktree(str(forged)) == str(fixture.stale), (
        "the record names this very worktree, so it is the component test that refuses this"
    )
    assert refusal(fixture.stale, repo_common) != ""


def test_ambient_git_dir_does_not_redirect_the_run(
    fixture: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Measured, git 2.53: with GIT_DIR and GIT_WORK_TREE pointing elsewhere, `git status` run
    with `cwd` inside one repository reports the OTHER one's working tree.
    """
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main", ".")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_COMMON_DIR", str(other / ".git"))

    assert set(git_env()).isdisjoint({"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"})
    assert main(sweep_argv(fixture)) == 0

    out = capsys.readouterr().out
    assert str(fixture.named) in out, "the run followed the ambient GIT_DIR, not --repo"
    assert str(other) not in out


def test_git_stderr_is_never_carried_into_a_report_or_an_exception(
    fixture: Fixture, tmp_path: Path
) -> None:
    """These reports get pasted into cards and PRs, and git's stderr names paths.

    A failing command's diagnosis is this module's own — an exit status — because git's own
    text can carry a working path or a credentialed remote URL from a config it just read.
    """
    result = run_git(fixture.repo, "rev-list", "--end-of-options", "no-such-ref")
    assert result.returncode != 0
    assert result.stderr == ""

    raw = subprocess.run(
        ["git", "rev-list", "no-such-ref"], cwd=str(fixture.repo), capture_output=True, text=True
    )
    assert "no-such-ref" in raw.stderr, "git really does say something here"

    with pytest.raises(RuntimeError) as caught:
        list_worktrees(tmp_path)
    assert "fatal" not in str(caught.value), "git's own wording never reaches the operator"
    assert "exit status 128" in str(caught.value), "this module's own diagnosis does"


# --------------------------------------------------------------------- live-set sources


def test_in_flight_chart_rows_yield_branch_selectors() -> None:
    chart = {
        "stories": [
            {"id": "E29-S88", "status": "in-flight"},
            {"id": "E23-S27", "status": "in-flight", "branch": "claude/story-e23-s27-explicit"},
            {"id": "E29-S73", "status": "merged"},
            "not a row",
        ]
    }
    assert in_flight_selectors(chart) == (["claude/story-e23-s27-explicit"], ["e29-s88"])
    assert in_flight_selectors({"stories": "wrong shape"}) == ([], [])
    assert in_flight_selectors(["not a chart"]) == ([], [])


def test_a_story_token_is_matched_bounded_not_as_a_substring() -> None:
    """A bare substring test lets `e2` claim every `e29-…` branch, and a one-character id in
    the chart matches nearly every branch."""
    branches = ["claude/story-e29-s88-x", "claude/story-e29-s8-y", "e2", "wf/E29-S88"]

    assert branches_matching_tokens(branches, ["e29-s88"]) == [
        "claude/story-e29-s88-x",
        "wf/E29-S88",
    ]
    assert branches_matching_tokens(branches, ["e29-s8"]) == ["claude/story-e29-s8-y"]
    assert branches_matching_tokens(branches, ["e2"]) == ["e2"]
    assert branches_matching_tokens(branches, [""]) == []


@pytest.mark.parametrize(
    "payload",
    [b"\xff\xfe{}", b"[" * 100_000],
    ids=["not utf-8", "nested past the recursion limit"],
)
def test_a_hostile_chart_is_reported_unreadable_not_raised(
    fixture: Fixture, payload: bytes
) -> None:
    """Only JSONDecodeError was caught; UnicodeDecodeError and RecursionError escaped."""
    chart = fixture.root / "chart.json"
    chart.write_bytes(payload)

    live = build_live_set(fixture.repo, use_gh=False, chart_path=chart)

    assert live.shas == frozenset()
    assert "chart in-flight rows: unreadable" in " ".join(live.notes)


def test_an_oversized_chart_is_refused_before_it_is_read(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`json.loads` of a 48 MB chart drove 523 MB of peak RSS, so the size is checked first."""
    chart = fixture.root / "chart.json"
    chart.write_text(json.dumps({"stories": [{"id": "E99-S01", "status": "in-flight"}]}))
    monkeypatch.setattr(sweep, "MAX_CHART_BYTES", 4)

    live = build_live_set(fixture.repo, use_gh=False, chart_path=chart)

    assert live.shas == frozenset()
    assert "over the 4-byte ceiling" in " ".join(live.notes)


def test_the_chart_source_resolves_an_in_flight_story_to_its_branch_sha(fixture: Fixture) -> None:
    chart = fixture.root / "chart.json"
    chart.write_text(json.dumps({"stories": [{"id": "E99-S01", "status": "in-flight"}]}))

    live = build_live_set(fixture.repo, use_gh=False, chart_path=chart)

    assert fixture.live_sha in live.shas
    assert LIVE_BRANCH in live.branches
    assert f"live branch: {LIVE_BRANCH}" in render_report([], live, frozenset())
    rows = fixture.rows(live)
    assert rows[fixture.detached.name] == LIVE
    assert rows[fixture.generic.name] == LIVE


def test_the_default_chart_is_the_main_checkouts_copy_not_the_worktrees(
    fixture: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chart is gitignored, so it exists ONLY at the main checkout — a worktree has none.

    Defaulting to `--repo` therefore lost every in-flight row whenever the tool was run from
    a worktree checkout, silently: the run still succeeded and simply saw no live work.
    """
    (fixture.repo / "open_stories.json").write_text(
        json.dumps({"stories": [{"id": "E99-S01", "status": "in-flight"}]})
    )
    assert not (fixture.named / "open_stories.json").exists()

    assert main(["--repo", str(fixture.named), "--no-gh"]) == 0

    out = capsys.readouterr().out
    assert "chart in-flight rows: 1 rows -> 1 branches resolved" in out
    assert f"live branch: {LIVE_BRANCH}" in out


def test_explicit_overrides_and_a_missing_chart_are_both_reported(fixture: Fixture) -> None:
    live = build_live_set(
        fixture.repo,
        use_gh=False,
        chart_path=fixture.root / "absent.json",
        live_branches=[GENERIC_BRANCH],
        live_shas=["deadbeef"],
    )
    assert live.shas == {fixture.live_sha, "deadbeef"}
    joined = " ".join(live.notes)
    assert "skipped (--no-gh)" in joined
    assert "no chart file read" in joined
    assert "deadbeef" in joined, "a --live-sha that resolves to nothing is named, not dropped"


def test_an_abbreviated_live_sha_marks_the_worktree_at_that_commit_live(
    fixture: Fixture,
) -> None:
    """`--live-branch` is rev-parsed, so `--live-sha` must be too: an abbreviation is never
    string-equal to a full HEAD, so comparing the two raw fails silently.

    The abbreviation matches no ref name, so it is also what keeps the third resolution
    candidate — the bare rev — load-bearing after the first two became dict lookups.
    """
    live = build_live_set(fixture.repo, use_gh=False, live_shas=[fixture.live_sha[:8]])

    assert fixture.live_sha in live.shas
    assert fixture.rows(live)[fixture.detached.name] == LIVE


#: Leading-dash names that take `=<value>`, each with the words git's own option parser
#: rejects it with when `--end-of-options` is absent. Measured, git 2.53, in a throwaway
#: repository.
OPTION_SHAPED_NAMES = (
    ("--abbrev-ref=strict", b"unknown mode for --abbrev-ref"),
    ("--path-format=relative", b"unknown argument to --path-format"),
    ("--output-object-format=sha1", b"unsupported object format"),
)


@pytest.mark.parametrize(
    ("name", "parser_says"),
    OPTION_SHAPED_NAMES,
    ids=[name for name, _ in OPTION_SHAPED_NAMES],
)
def test_a_leading_dash_branch_name_is_not_parsed_as_an_option(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, name: str, parser_says: bytes
) -> None:
    """These names arrive from `gh`, from the chart and from `--live-branch`.

    `resolve_shas` hands rev-parse `<name>^{commit}`, never a bare name. That suffix alone
    defuses `--local-env-vars` (measured, git 2.53), but not the names in the table above:
    without `--end-of-options` git's option parser rejects them with a fatal exit. The argv is
    CAPTURED from the real spawn and re-run from the captured list, as spawned and then with the
    flag taken out of it, so nothing here is retyped from the module.
    """
    real = subprocess.run
    spawns: list[tuple[list[str], dict[str, object]]] = []

    def counting(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        spawns.append((argv, kwargs))
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", counting)
    assert resolve_shas(fixture.repo, [name]) == {}
    monkeypatch.undo()

    [(argv, kwargs)] = [spawn for spawn in spawns if "rev-parse" in spawn[0]]
    env = {**dict(kwargs["env"]), "LC_ALL": "C"}  # type: ignore[call-overload]
    rerun = {**kwargs, "env": env}

    as_spawned = real(argv, **rerun)  # type: ignore[call-overload]
    assert as_spawned.returncode == 1, as_spawned.stderr
    assert as_spawned.stderr == b""

    without = [word for word in argv if word != "--end-of-options"]
    unguarded = real(without, **rerun)  # type: ignore[call-overload]
    assert unguarded.returncode == 128, unguarded.stdout
    assert parser_says in unguarded.stderr, unguarded.stderr


def test_a_name_resolves_through_the_local_ref_before_origins(fixture: Fixture) -> None:
    """The three candidates are tried in order, and only the first two became dict lookups.

    An open-PR head branch that exists locally AND on origin must resolve to the LOCAL one,
    as it did when each candidate was its own `rev-parse`; a branch that exists only on
    origin must still resolve at all.
    """
    only_remote = git(fixture.repo, "rev-parse", "main")
    git(fixture.repo, "update-ref", "refs/remotes/origin/pushed-only", only_remote)
    git(fixture.repo, "update-ref", f"refs/remotes/origin/{LIVE_BRANCH}", only_remote)

    resolved = resolve_shas(fixture.repo, ["pushed-only", LIVE_BRANCH])

    assert resolved["pushed-only"] == only_remote, "origin's ref is the second candidate"
    assert resolved[LIVE_BRANCH] == fixture.live_sha, "the local ref wins over origin's"
    assert only_remote != fixture.live_sha, "the two candidates really do disagree"


def test_resolving_many_names_costs_one_process_not_three_per_name(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three rev-parse attempts per name is ~600 processes at 200 open PRs.

    One `for-each-ref` answers the local and origin candidates for every name at once; only
    a name that matches no ref still needs git's own parser.
    """
    real = subprocess.run
    spawns: list[list[str]] = []

    def counting(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        spawns.append(argv)
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", counting)
    names = [LIVE_BRANCH, GENERIC_BRANCH, "main"] * 4

    assert set(resolve_shas(fixture.repo, names)) == {LIVE_BRANCH, GENERIC_BRANCH, "main"}
    assert len(spawns) == 1, spawns
    assert spawns[0][-2:] == ["refs/heads", "refs/remotes/origin"], spawns[0]

    spawns.clear()
    assert resolve_shas(fixture.repo, ["no-such-branch"]) == {}
    assert len(spawns) == 2, "the ref listing, then the one fallback rev-parse"


#: The ten code points `str.splitlines()` breaks a string on, and what git does with each in a
#: ref name. The seven ASCII separators are refused; the three Unicode ones are ACCEPTED, which
#: is the whole gap between what Python splits and what git will hand back.
SPLITLINES_CODE_POINTS = (
    ("LF", "\n", False),
    ("VT", "\v", False),
    ("FF", "\f", False),
    ("CR", "\r", False),
    ("FS", "\x1c", False),
    ("GS", "\x1d", False),
    ("RS", "\x1e", False),
    ("NEL", "\x85", True),
    ("LS", "\u2028", True),
    ("PS", "\u2029", True),
)


@pytest.mark.parametrize(
    ("label", "char", "accepted"),
    SPLITLINES_CODE_POINTS,
    ids=[label for label, _, _ in SPLITLINES_CODE_POINTS],
)
def test_git_accepts_three_of_the_code_points_python_splits_lines_on(
    fixture: Fixture, label: str, char: str, accepted: bool
) -> None:
    """The premise of the record-split fix, measured rather than quoted from documentation.

    Every one of these breaks a Python string, which is what makes `str.splitlines()` the
    wrong record separator for output whose records carry a ref NAME. git refuses the seven
    ASCII ones and accepts the other three (measured, git 2.53), so the gap is real and the
    parametrisation below is not a list of hypotheticals.
    """
    assert len(f"a{char}b".splitlines()) == 2, "Python breaks on every one of these"

    name = f"refs/heads/a{char}b"
    checked = run_git(fixture.repo, "check-ref-format", name)
    updated = subprocess.run(
        ["git", "update-ref", "--end-of-options", name, fixture.live_sha],
        cwd=str(fixture.repo),
        capture_output=True,
    )

    assert (checked.returncode == 0) is accepted, f"{label}: check-ref-format {checked.returncode}"
    assert (updated.returncode == 0) is accepted, f"{label}: update-ref {updated.returncode}"
    assert (name in sweep.ref_shas(fixture.repo)[0]) is accepted, (
        f"{label}: and the ref table answers for exactly the names git let be created"
    )


def test_a_ref_name_cannot_forge_a_second_entry_in_the_ref_table(fixture: Fixture) -> None:
    """A record separator git does not control is a record separator someone else does.

    The module already knows this about the FIELD separator — it chose `%00` because a ref
    name can carry anything else — and then split RECORDS with `str.splitlines()`. Measured,
    git 2.53: a ref named `refs/heads/decoy<U+2028>refs/heads/victim` is accepted, and
    splitting its record on that code point yields a fragment carrying no NUL, which is
    dropped for want of a SHA, and a remainder read as a whole record. So the real ref
    vanished from the table and `refs/heads/victim` — a ref that exists NOWHERE — entered it
    against a commit of the writer's choosing, in front of every caller that resolves a name.
    """
    hostile = "refs/heads/decoy\u2028refs/heads/victim"
    git(fixture.repo, "update-ref", "--end-of-options", hostile, fixture.live_sha)

    assert run_git(fixture.repo, "rev-parse", "--verify", "--quiet", "refs/heads/victim").returncode
    assert git(fixture.repo, "for-each-ref", "--format=%(refname)", hostile) == hostile, (
        "the one ref that really exists is the hostile name itself"
    )

    refs, problem = sweep.ref_shas(fixture.repo)

    assert problem is None, "for-each-ref succeeded, so the table is trusted"
    assert refs.get(hostile) == fixture.live_sha, "the real ref is in the table, whole"
    assert "refs/heads/victim" not in refs, "and the ref that exists nowhere is not"
    assert resolve_shas(fixture.repo, ["victim"], refs) == {}, (
        "so no caller resolves `victim` to the commit the ref name chose for it"
    )
    assert build_live_set(fixture.repo, use_gh=False, live_branches=["victim"]).shas == frozenset()


def test_a_for_each_ref_read_that_fails_is_reported_not_read_not_an_empty_table(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER 3 sibling: a failed ref read must not render as a repository with no refs.

    ref_shas never inspected the exit status, so a non-zero `for-each-ref` returned {} exactly
    as a repository with no refs would; every live-set resolution runs through that table, so a
    silent failure drops every worktree tied only to a branch to DIRTY without a word. The
    failure now carries a reason DISTINCT from a genuine empty (problem None), and build_live_set
    renders it as `not read`. A test asserting only `refs == {}` could not tell the two apart —
    which is exactly why the review could.
    """
    # A genuine empty first, under NO monkeypatch: an unborn HEAD has no refs, problem-free.
    empty = init_repo(fixture.root / "empty-refs")
    refs0, problem0 = sweep.ref_shas(empty)
    assert refs0 == {} and problem0 is None, "no refs is an empty table, not a failed read"

    # Now force the read itself to fail and confirm it is distinguishable from that empty.
    real = subprocess.run

    def failing_for_each_ref(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "for-each-ref" in argv:
            return subprocess.CompletedProcess(argv, returncode=1, stdout=b"", stderr=b"boom")
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", failing_for_each_ref)

    refs, problem = sweep.ref_shas(fixture.repo)
    assert refs == {}, "the same empty dict either way"
    assert problem is not None and "for-each-ref" in problem, "but this one carries a reason"

    live = build_live_set(fixture.repo, use_gh=False, refs=None)
    assert any("refs: not read" in note for note in live.notes), (
        "and the live set reports it as not read, never as a silent zero"
    )


def test_main_carries_a_failed_ref_read_into_the_rendered_report(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator's ONLY path: `main` reads the ref table once and hands its problem across.

    `main` reads `ref_shas` itself and calls `build_live_set(refs=<dict>, refs_problem=<reason>)`,
    so the failure reaches the report ONLY through that `refs_problem=` keyword — with `refs`
    non-None, `build_live_set` does not re-read and cannot rediscover it. The build_live_set
    test above drives `refs=None`, which re-reads its own problem and so never exercises this
    hand-off; dropping `refs_problem=refs_problem,` from `main` left a run that hid a failed ref
    read yet rendered clean. Drive `main` end to end with a failing `for-each-ref` and read the
    `not read` note off the rendered stdout.
    """
    real = subprocess.run

    def failing_for_each_ref(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if "for-each-ref" in argv:
            return subprocess.CompletedProcess(argv, returncode=1, stdout=b"", stderr=b"boom")
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", failing_for_each_ref)

    assert main(sweep_argv(fixture)) == 0
    out = capsys.readouterr().out
    assert "local and origin refs: not read" in out, (
        "the failed ref read reaches the operator's report only through main()'s refs_problem "
        "pass-through; without it the report renders clean over a failed read"
    )


# ------------------------------------------------------------ the gh source, the default


def stub_gh(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = "[]", boom: bool = False
) -> list[dict[str, object]]:
    """Answer only the `gh` invocation; every git call still really runs."""
    real = subprocess.run
    seen: list[dict[str, object]] = []

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] != "gh":
            return real(argv, **kwargs)  # type: ignore[arg-type]
        # Record the argv alongside the kwargs so a test can pin the exact `--limit` value the
        # module sends: without it, `str(limit + 1)` mutated back to `str(limit)` is invisible,
        # since this stub's row count is fixed and `truncated` reads the same either way.
        seen.append({"argv": argv, **kwargs})
        if boom:
            raise OSError("gh is not installed")
        # Bytes, because the module no longer asks `subprocess` for text mode: text mode also
        # TRANSLATES newlines, and a translated path is what let a worktree be read as another
        # one. A stub that still answered `str` would be modelling an interface that is gone.
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout.encode(), stderr=b"")

    monkeypatch.setattr(sweep.subprocess, "run", fake)
    return seen


def test_the_gh_source_reads_open_pr_head_branches(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gh pr list` reads the repository too, so it gets the same GIT_-scrubbed environment.

    It does NOT get the config pins: `gh` resolves its own credentials and its remote from
    the ambient configuration, so emptying that would be changing which repository it asks.
    """
    monkeypatch.setenv("GIT_DIR", "/nowhere/.git")
    seen = stub_gh(
        monkeypatch, stdout=json.dumps([{"headRefName": LIVE_BRANCH}, {"headRefName": "x"}])
    )

    result = gh_open_pr_branches(fixture.repo)
    assert result.branches == (LIVE_BRANCH, "x")
    assert result.problem is None, "a read that succeeded carries no problem"
    assert result.truncated is False, "two rows, well under the cap"
    assert "GIT_DIR" not in seen[0]["env"]  # type: ignore[operator]
    assert "GIT_CONFIG_GLOBAL" not in seen[0]["env"]  # type: ignore[operator]
    assert "GIT_CONFIG_GLOBAL" in git_env(), "which git itself still gets"


@pytest.mark.parametrize(
    ("label", "kwargs", "expect_problem"),
    [
        # Non-empty stdout on a failure, so dropping the returncode guard really changes
        # the answer: gh prints usage to stdout when it cannot reach the API. A FAILED read.
        ("gh failed", {"returncode": 1, "stdout": '[{"headRefName": "leaked-on-failure"}]'}, True),
        ("gh not installed", {"boom": True}, True),
        ("malformed json", {"stdout": "{not json"}, True),
        ("json that is not a list", {"stdout": '{"headRefName": "x"}'}, True),
        # A scalar is not iterable at all, so this one used to escape as a TypeError.
        ("json that is a scalar", {"stdout": "5"}, True),
        # The payload `read_chart` — the module's OTHER `json.loads` — is already tested
        # against, and which this reader's `json.JSONDecodeError` handler does not catch:
        # RecursionError is not a ValueError, so it escaped the documented contract entirely.
        ("json nested past the recursion limit", {"stdout": "[" * 100_000}, True),
        # The ONE genuinely-empty read among these: gh answered with a well-formed list that
        # simply names no head branch. This one must NOT carry a problem — it is the case a
        # failed read may not be confused with, which is the whole point of BLOCKER 3.
        ("a well-formed empty answer", {"stdout": '[{"number": 1}, "not a row"]'}, False),
    ],
)
def test_a_failed_gh_read_is_reported_never_blanked_into_a_genuine_empty(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    kwargs: dict[str, object],
    expect_problem: bool,
) -> None:
    """Never raises, and a read that could not be performed is DISTINGUISHABLE from empty.

    Asserting only `branches == ()` — as an earlier `== []` did — passes on every row here,
    which is exactly why the review could see through it: a `gh` that failed and a `gh` that
    found nothing rendered identically. The problem field is what separates them, and
    `build_live_set` turns it into `not read (<reason>)` rather than a `0 found`.
    """
    stub_gh(monkeypatch, **kwargs)  # type: ignore[arg-type]

    result = gh_open_pr_branches(fixture.repo)

    assert isinstance(result, GhBranches), f"{label}: never raises, always answers"
    assert result.branches == (), label
    assert (result.problem is not None) is expect_problem, (
        f"{label}: a failed read must carry a reason and a real empty must not"
    )


def test_a_gh_branch_name_that_is_not_utf_8_arrives_intact_rather_than_vanishing(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git accepts a branch name UTF-8 does not, and this reader has to carry one through.

    The VALUE is what is pinned, not the absence of a raise: the decode sits inside the
    `except (ValueError, RecursionError)` block below it, and a `UnicodeDecodeError` IS a
    ValueError, so a strict decode does not escape — it is swallowed into an empty list, and
    an open PR silently stops counting as live work. Only an assertion on what came back
    separates the two.

    `gh` is a third-party binary whose stdout this module does not control, so this stub runs
    a REAL process emitting raw bytes and forwards the module's own keyword arguments: what
    comes back is decided by the arguments the module actually passes, not by the stub.
    """
    real = subprocess.run

    def emitting_raw_bytes(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "gh":
            return real(argv, **kwargs)  # type: ignore[arg-type]
        payload = rb"""import sys; sys.stdout.buffer.write(b'[{"headRefName": "br-\xff-anch"}]')"""
        return real([sys.executable, "-c", payload.decode()], **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", emitting_raw_bytes)

    result = gh_open_pr_branches(fixture.repo)

    assert result.branches == ("br-\udcff-anch",)
    assert result.problem is None, "a decodable-with-surrogates answer is a success, not a failure"
    assert result.branches[0].encode("utf-8", "surrogateescape") == b"br-\xff-anch", (
        "and re-encodes to the bytes gh emitted, which is what keeps the name usable as a rev"
    )


def test_a_child_process_that_never_finishes_fails_its_own_row(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile fsmonitor, a `gh` awaiting auth or a stale mount hangs an unbounded run.

    The timeout is handled exactly like a failure to start: the command is reported as
    failed, in this module's own words, and the rest of the report is still produced.
    """
    real = subprocess.run

    def timing_out(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "status" in argv or argv[0] == "gh":
            raise subprocess.TimeoutExpired(argv, sweep.TIMEOUT_SECONDS)
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.subprocess, "run", timing_out)

    timed = gh_open_pr_branches(fixture.repo)
    assert timed.branches == ()
    assert timed.problem == f"gh timed out after {sweep.TIMEOUT_SECONDS}s", (
        "the timeout is reported as a reason, not blanked into an empty list"
    )
    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert f"git status failed (timed out after {sweep.TIMEOUT_SECONDS}s)" in row.reason


def test_output_a_child_process_cannot_decode_does_not_kill_the_run(
    fixture: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same class as the gh reader's nesting payload, in the module's OTHER spawner.

    A lock reason is free text in a plain file inside the repository's own git directory, so
    nothing makes it valid UTF-8. Measured, git 2.53: with raw bytes written there,
    `worktree list --porcelain -z` emits them and `text=True` raised UnicodeDecodeError from
    inside `subprocess.run` — before any handler run_git has — so the whole report died on a
    traceback, which is what run_git exists to prevent. Decoding that cannot raise is the fix,
    and `one_line` is what then keeps the undecodable bytes from reaching `print`.
    """
    git(fixture.repo, "worktree", "lock", "--reason", "placeholder", str(fixture.stale))
    git_dir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))
    (git_dir / "locked").write_bytes(b"agent \xff\xfe pid 1\n")

    raw = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=str(fixture.repo),
        capture_output=True,
    )
    assert b"\xff\xfe" in raw.stdout, "git really does emit the undecodable bytes"

    entries = {entry.path.name: entry for entry in list_worktrees(fixture.repo)}
    assert entries[fixture.stale.name].locked is True

    assert main(sweep_argv(fixture)) == 0
    out = capsys.readouterr().out
    assert "\\udcff" in out, "escaped by one_line, never handed raw to print"


def test_the_default_live_set_gains_the_open_pr_head_sha(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh is the DEFAULT source, and every other test here passes --no-gh."""
    stub_gh(monkeypatch, stdout=json.dumps([{"headRefName": LIVE_BRANCH}]))

    live = build_live_set(fixture.repo, use_gh=True, chart_path=None)

    assert fixture.live_sha in live.shas
    assert LIVE_BRANCH in live.branches
    assert "1 found, 1 resolved to a commit" in " ".join(live.notes)
    assert fixture.rows(live)[fixture.detached.name] == LIVE


def test_the_gh_pr_list_cap_is_detected_by_asking_for_one_more_than_the_limit(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER 3 sibling: `gh pr list --limit N` truncated silently at exactly N.

    The read asks for limit+1: getting that many back proves at least one more open PR exists
    than a limit-capped list would have shown, and `truncated` says so instead of the count
    topping out in silence. The stub answers whatever row count the case names, independent of
    the `--limit` the module passed, so the boundary is exercised directly.
    """
    seen = stub_gh(monkeypatch, stdout=json.dumps([{"headRefName": f"b{i}"} for i in range(3)]))
    capped = gh_open_pr_branches(fixture.repo, limit=2)
    assert capped.truncated is True, "three rows for a limit of two — more open PRs exist"
    assert capped.problem is None
    # The +1 is the whole mechanism, and `truncated` alone cannot pin it: this stub's fixed
    # three rows read as truncated whether the module asked for 2 or 3. Only the argv sent to
    # gh distinguishes `str(limit + 1)` from `str(limit)`, so assert on it directly.
    sent_argv = seen[0]["argv"]
    assert isinstance(sent_argv, list) and sent_argv[sent_argv.index("--limit") + 1] == "3", (
        "the read asks git for limit+1 (3), not limit (2) — that surplus row is what a truncated "
        "page is detected by"
    )

    stub_gh(monkeypatch, stdout=json.dumps([{"headRefName": f"b{i}"} for i in range(2)]))
    exact = gh_open_pr_branches(fixture.repo, limit=2)
    assert exact.truncated is False, "exactly the limit came back, so nothing was hidden"


def test_the_live_set_discloses_a_failed_gh_read_and_a_hit_cap(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two gh signals reach the operator through build_live_set, not just the return value.

    A failed read renders `not read (<reason>)` and never `0 found`, so an operator does not
    read a gh outage as "no open PRs"; a hit cap renders a warning that more exist. Both are
    driven by a stubbed `gh_open_pr_branches` so the note wording is pinned without 200+ rows.
    """
    monkeypatch.setattr(
        sweep,
        "gh_open_pr_branches",
        lambda repo, limit=200: GhBranches(problem="gh could not be started"),
    )
    failed = build_live_set(fixture.repo, use_gh=True, chart_path=None)
    joined = " ".join(failed.notes)
    assert "open-PR branches (gh): not read (gh could not be started)" in joined
    assert "0 found" not in joined, "a failed read is never rendered as a zero count"

    monkeypatch.setattr(
        sweep,
        "gh_open_pr_branches",
        lambda repo, limit=200: GhBranches(branches=("main",), truncated=True),
    )
    capped = build_live_set(fixture.repo, use_gh=True, chart_path=None)
    assert any("more open PRs exist than were read" in note for note in capped.notes)


# --------------------------------------------------------------------- AC5: disclosed bounds


def test_both_live_set_flags_exist_and_parse() -> None:
    assert not hasattr(sweep, "LIVE_SET_BOUND"), "the constant is gone from the module's API"

    restored = build_arg_parser().parse_args(["--live-branch", "b", "--live-sha", "s"])
    assert (restored.live_branch, restored.live_sha) == (["b"], ["s"]), "and those flags exist"


def test_the_reason_left_standing_makes_no_claim_about_a_worktrees_contents() -> None:
    """The narrowing, asserted where an emptiness claim would have to be written to survive.

    PRUNABLE_BOUND existed to disclose what the PRUNABLE verdict missed. With no verdict that
    says a worktree is empty there is nothing to disclose, so it is gone — and this is the
    check that it did not simply move.
    """
    help_text = build_arg_parser().format_help()

    assert not hasattr(sweep, "PRUNABLE"), "the verdict is gone from the module's API"
    assert not hasattr(sweep, "PRUNABLE_BOUND"), "and so is the bound that disclosed it"
    assert "PRUNABLE" not in help_text, "and from every word the tool prints"
    assert "MAIN / LIVE / DIRTY / BROKEN / MISSING." in help_text, "the five that are left"
    assert not any(word in UNLINKED_REASON.lower() for word in CLEARANCE_WORDS), (
        "nor the reason that replaced the verdict — it reports the live-set search and then "
        f"declines the question the reads cannot answer: {UNLINKED_REASON}"
    )
    assert "not judged here" in UNLINKED_REASON, "which it has to say outright, not merely omit"


def test_a_locked_worktree_is_live_though_none_of_the_reads_sees_the_lock(
    fixture: Fixture,
) -> None:
    """The member of the class that made PRUNABLE_BOUND's own second sentence false.

    A lock is state in the worktree's OWN git directory, and none of the three reads reports
    it — so it was inside the class PRUNABLE_BOUND defined, exactly as that bound defined it.
    Yet the row reads LIVE, and `git worktree remove` refuses it without --force (measured,
    git 2.53). The sentence that had to go was the one about what such state does to a
    VERDICT; the reads are unchanged, so the measurement is kept.
    """
    git(fixture.repo, "worktree", "lock", "--reason", "held by a session", str(fixture.stale))
    gitdir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))
    listed = git(fixture.stale, "ls-files", "--stage", "-v").splitlines()

    assert (gitdir / "locked").exists(), "in its own git directory, not in its working tree"
    assert (
        git(
            fixture.stale,
            "status",
            "--porcelain",
            "-uall",
            "--ignored=matching",
            "--ignore-submodules=dirty",
        )
        == ""
    )
    assert [ln for ln in listed if ln[:1].islower() or ln[:1] == "S"] == []
    assert git(fixture.stale, "rev-list", "HEAD", "^main") == ""

    assert fixture.row(fixture.stale.name).status == LIVE, "and yet the row reads LIVE"
    removal = subprocess.run(
        ["git", "worktree", "remove", str(fixture.stale)],
        cwd=str(fixture.repo),
        capture_output=True,
        text=True,
    )
    assert removal.returncode == 128, "nor is it removable without --force"


#: Ref namespaces git keeps PER-WORKTREE, so a commit only one of them names dies with the
#: worktree. Each case measures that per-worktree-ness rather than assuming it.
PER_WORKTREE_REFS = ("refs/worktree/mine", "refs/bisect/keep", "refs/rewritten/keep")


@pytest.mark.parametrize("ref", PER_WORKTREE_REFS)
def test_a_commit_only_a_per_worktree_ref_names_leaves_the_row_naming_nothing(
    fixture: Fixture, ref: str
) -> None:
    """How far the reads reach, measured rather than argued — and what the row may say now.

    The commit test reads the entry's HEAD and nothing else, so a commit that no branch and
    no tag names is invisible to it. Measured, git 2.53: the reads all come back empty, the
    worktree is accepted for removal with NO --force, and the commit is unreachable
    afterwards. That is the shape which made a verdict of emptiness false, and it is why the
    reason left standing describes the live-set search and not the working tree.
    """
    (fixture.stale / "only.txt").write_text("work that exists nowhere else\n")
    git(fixture.stale, "add", "only.txt")
    git(fixture.stale, "commit", "-qm", "only here")
    only = git(fixture.stale, "rev-parse", "HEAD")
    git(fixture.stale, "update-ref", ref, only)
    git(fixture.stale, "checkout", "-q", "--detach", "main")

    assert git(fixture.stale, "rev-parse", ref) == only, "the worktree's own ref names it"
    assert git(fixture.repo, "for-each-ref", "--points-at", only) == "", (
        "and the repository sees no ref on it at all, so the namespace really is per-worktree"
    )
    assert commits_off_main(fixture.repo, only, "main") == frozenset({only}), "and it is off main"

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == UNLINKED_REASON
    assert not any(word in row.reason.lower() for word in CLEARANCE_WORDS), (
        "there IS a commit only here, so any wording an operator could read as a clearance "
        f"would be false of this very row: {row.reason}"
    )

    removal = subprocess.run(
        ["git", "worktree", "remove", str(fixture.stale)],
        cwd=str(fixture.repo),
        capture_output=True,
        text=True,
    )
    assert removal.returncode == 0, f"`git worktree remove` refused it: {removal.stderr}"
    git(fixture.repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    git(fixture.repo, "gc", "--prune=now", "--quiet")
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", only], cwd=str(fixture.repo), capture_output=True
        ).returncode
        != 0
    ), "and the commit went with it"


def test_a_commit_only_the_worktrees_own_reflog_names_leaves_the_row_naming_nothing(
    fixture: Fixture,
) -> None:
    """The same shape through the route no ref covers at all.

    `<gitdir>/logs/HEAD` is per-worktree too, so a commit made and then detached away from is
    named by nothing else. Measured, git 2.53: every read comes back empty, and the reflog
    dies with the gitdir.
    """
    (fixture.stale / "only.txt").write_text("reflog-only work\n")
    git(fixture.stale, "add", "only.txt")
    git(fixture.stale, "commit", "-qm", "only here")
    only = git(fixture.stale, "rev-parse", "HEAD")
    git(fixture.stale, "checkout", "-q", "--detach", "main")

    gitdir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))
    assert only in (gitdir / "logs" / "HEAD").read_text(), "the worktree's own reflog holds it"
    assert git(fixture.repo, "for-each-ref", "--points-at", only) == "", "and no ref names it"

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == UNLINKED_REASON
    assert not any(word in row.reason.lower() for word in CLEARANCE_WORDS), row.reason

    removal = subprocess.run(
        ["git", "worktree", "remove", str(fixture.stale)],
        cwd=str(fixture.repo),
        capture_output=True,
        text=True,
    )
    assert removal.returncode == 0, f"`git worktree remove` refused it: {removal.stderr}"
    assert not gitdir.exists(), "and the reflog went with the gitdir"


def suspend_sequencer(kind: str, fixture: Fixture, tmp_path: Path) -> None:
    """Leave `fixture.stale` suspended mid-`kind`, and clean.

    Each stops with the working tree untouched and the worktree's HEAD on a commit that is on
    `main`, so the only thing it carries is the sequencer state in its own git directory.
    """
    for content in ("second", "third"):
        (fixture.repo / "f.txt").write_text(f"{content}\n")
        git(fixture.repo, "add", "f.txt")
        git(fixture.repo, "commit", "-qm", content)
    git(fixture.stale, "checkout", "-q", "--detach", "main")

    if kind == "am":
        git(fixture.repo, "checkout", "-q", "-b", "patch-source")
        (fixture.repo / "f.txt").write_text("patched\n")
        git(fixture.repo, "add", "f.txt")
        git(fixture.repo, "commit", "-qm", "patchable")
        patch = git(fixture.repo, "format-patch", "-1", "--stdout")
        git(fixture.repo, "checkout", "-q", "main")
        git(fixture.repo, "branch", "-qD", "patch-source")
        (fixture.repo / "f.txt").write_text("fourth\n")
        git(fixture.repo, "add", "f.txt")
        git(fixture.repo, "commit", "-qm", "main moves under the patch")
        git(fixture.stale, "checkout", "-q", "--detach", "main")
        subprocess.run(
            ["git", "am"], cwd=str(fixture.stale), input=patch, capture_output=True, text=True
        )
    elif kind == "rebase":
        editor = tmp_path / "insert-break.sh"
        editor.write_text('#!/bin/sh\nprintf "break\\n" | cat - "$1" > "$1.t" && mv "$1.t" "$1"\n')
        editor.chmod(0o755)
        subprocess.run(
            ["git", "rebase", "-i", "HEAD~1"],
            cwd=str(fixture.stale),
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_SEQUENCE_EDITOR": str(editor)},
        )
    elif kind == "cherry-pick":
        # The everyday stop: the commit being picked is already in HEAD's history, so git
        # halts on "the previous cherry-pick is now empty" with nothing staged.
        subprocess.run(
            ["git", "cherry-pick", git(fixture.repo, "rev-parse", "main")],
            cwd=str(fixture.stale),
            capture_output=True,
        )
    elif kind == "merge":
        # A side branch that makes main's own change, so the merge resolves to HEAD's tree.
        git(fixture.repo, "branch", "-q", "merge-source", "main~1")
        git(fixture.repo, "checkout", "-q", "merge-source")
        (fixture.repo / "f.txt").write_text("third\n")
        git(fixture.repo, "add", "f.txt")
        git(fixture.repo, "commit", "-qm", "the same change, on a branch of its own")
        git(fixture.repo, "checkout", "-q", "main")
        subprocess.run(
            ["git", "merge", "--no-commit", "--no-ff", "merge-source"],
            cwd=str(fixture.stale),
            capture_output=True,
        )
    else:
        for args in (("start",), ("bad", "HEAD"), ("good", "HEAD~2")):
            subprocess.run(["git", "bisect", *args], cwd=str(fixture.stale), capture_output=True)


#: kind, the per-worktree state it leaves, and what git's LONG status says about it.
SUSPENDED_SEQUENCERS = (
    ("am", "rebase-apply", "in the middle of an am session"),
    ("rebase", "rebase-merge", "interactive rebase in progress"),
    ("bisect", "BISECT_START", "You are currently bisecting"),
    ("cherry-pick", "CHERRY_PICK_HEAD", "You are currently cherry-picking"),
    ("merge", "MERGE_HEAD", "you are still merging"),
)


@pytest.mark.parametrize("kind, state, long_format", SUSPENDED_SEQUENCERS)
def test_a_suspended_sequencer_leaves_the_row_naming_nothing(
    fixture: Fixture,
    tmp_path: Path,
    kind: str,
    state: str,
    long_format: str,
) -> None:
    """Crashed-session residue whose whole trace is in the per-worktree git directory.

    The sequencer's state sits there rather than in the working tree, and the porcelain this
    module reads prints no sequencer line — git's LONG format is where it is said. Measured,
    git 2.53: every read comes back empty, and the worktree is then accepted for removal
    with no --force. The row is still REPORTED, which is what this tool was filed for; what
    it no longer does is tell an operator that there is nothing here.
    """
    suspend_sequencer(kind, fixture, tmp_path)
    gitdir = Path(git(fixture.stale, "rev-parse", "--path-format=absolute", "--git-dir"))

    assert (gitdir / state).exists(), f"{kind} really is suspended, and in its own git directory"
    assert long_format in git(fixture.stale, "status"), "git's long format says so"
    assert git(fixture.stale, "status", "--porcelain") == "", "and the porcelain says nothing"

    row = fixture.row(fixture.stale.name)
    assert row.status == DIRTY
    assert row.reason == UNLINKED_REASON
    assert not any(word in row.reason.lower() for word in CLEARANCE_WORDS), row.reason

    removal = subprocess.run(
        ["git", "worktree", "remove", str(fixture.stale)],
        cwd=str(fixture.repo),
        capture_output=True,
        text=True,
    )
    assert removal.returncode == 0, f"`git worktree remove` refused it: {removal.stderr}"
    assert not gitdir.exists(), "and the suspended session went with the git directory"


def test_help_names_every_status_the_report_can_print() -> None:
    """The statuses are derived from the module, so a new one forces the help to name it."""
    statuses = {v for k, v in vars(sweep).items() if k.isupper() and isinstance(v, str) and v == k}
    assert len(statuses) == 5, statuses

    description = build_arg_parser().description or ""
    for status in statuses:
        assert status in description, status


def test_a_stale_lock_keeps_a_worktree_live_forever_and_the_docstring_says_so(
    fixture: Fixture,
) -> None:
    """The lock guard's cost, demonstrated and then pinned as disclosed.

    A lock outlives whatever set it, so a crashed session's worktree is never called stale —
    which is the residue this script exists for. The text assertion is a prose pin: it shows
    the bound is stated in the docstring once, not that the wording is any good.
    """
    (fixture.stale / "f.txt").write_text("edited\n")
    git(fixture.repo, "worktree", "lock", "--reason", "agent (pid 1, dead)", str(fixture.stale))

    assert fixture.rows()[fixture.stale.name] == LIVE

    assert flat(sweep.__doc__).count("outlives the process that set it") == 1


def test_the_report_says_how_much_of_the_live_set_is_off_the_main_ref(
    fixture: Fixture,
) -> None:
    """The reachability test uses only the off-main part, so an operator can see it is empty."""
    live = fixture.live()
    live_only = live_only_commits(fixture.repo, live.shas, "main")

    assert live_only == frozenset({fixture.live_sha})
    assert "1 live commit(s) not on the main ref" in render_report([], live, live_only)
    assert commits_off_main(fixture.repo, "no-such-ref", "main") is None
    assert live_only_commits(fixture.repo, ["no-such-ref"], "main") == frozenset()


# --------------------------------------------------------------- the command line's edges


def test_an_unreadable_repo_is_reported_not_silently_empty(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"git worktree list in .* failed"):
        list_worktrees(tmp_path)


def test_a_bad_repo_argument_is_an_exit_code_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`list_worktrees` raises straight out of `main`, and an operator got a traceback."""
    assert main(["--repo", str(tmp_path), "--no-gh"]) == 2

    captured = capsys.readouterr()
    assert "git worktree list in" in captured.err
    assert captured.out == ""


def test_a_repo_argument_that_is_a_symlink_cycle_is_an_exit_code_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same class one step further: an argument `main` touched OUTSIDE its own handler.

    `common_dir` documents this exact trap — `os.path.realpath` "rather than `Path.resolve`,
    which raises on a symlink cycle on 3.11" — while `main` called `.resolve()` on the repo
    argument before the try block, so the operator got a RuntimeError traceback instead of
    the exit code the plain not-a-repository case above already returned.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    os.symlink(b, a)
    os.symlink(a, b)

    with pytest.raises(RuntimeError):
        a.resolve()  # the trap is real on this interpreter

    assert main(["--repo", str(a), "--no-gh"]) == 2

    captured = capsys.readouterr()
    assert "git worktree list in" in captured.err
    assert captured.out == ""


def test_an_unresolvable_main_ref_stops_the_run_instead_of_dirtying_every_row(
    fixture: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every commit test compares against this ref, so an unresolvable one is not a detail.

    Measured: with `--main-ref` naming nothing, `rev-list` fails for every worktree — a
    report of a repository full of work, exit 0, no warning anywhere.
    """
    assert main(sweep_argv(fixture, "--main-ref", "no-such-ref")) == 2

    captured = capsys.readouterr()
    assert "does not resolve" in captured.err
    assert captured.out == ""

    # What the guard prevents: the same run without it, classifying against that ref.
    entries = list_worktrees(fixture.repo)
    unguarded = classify_worktrees(
        fixture.repo,
        entries,
        main_path=main_worktree_path(entries),
        live=LiveSet(shas=frozenset()),
        live_only=frozenset(),
        main_ref="no-such-ref",
    )
    unguarded_reasons = {row.reason for row in unguarded if row.status != MAIN}
    assert unguarded_reasons == {
        "carries could not list this worktree's commits against the main ref"
    }, unguarded_reasons
    assert_reads_named_nothing(fixture, fixture.stale.name, "against a real ref:")


def main_recording_rows(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, sweep.Classified], str]:
    """Run `main` and keep the rows it classified: the REAL classifier, its answer recorded."""
    real = sweep.classify_worktrees
    seen: list[sweep.Classified] = []

    def recording(*args: object, **kwargs: object) -> list[sweep.Classified]:
        rows = real(*args, **kwargs)  # type: ignore[arg-type]
        seen.extend(rows)
        return rows

    monkeypatch.setattr(sweep, "classify_worktrees", recording)
    code = main(argv)
    monkeypatch.setattr(sweep, "classify_worktrees", real)
    return code, {row.worktree.path.name: row for row in seen}, capsys.readouterr().err


def rows_by_main_ref(
    argv_for: Callable[[str], list[str]],
    refs: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, dict[str, tuple[str, str]]]:
    """Each ref's `(status, reason)` rows, from a `main` run that exited 0 and wrote no error."""
    rows = {}
    for ref in refs:
        code, classified, err = main_recording_rows(argv_for(ref), monkeypatch, capsys)
        assert (code, err) == (0, ""), (ref, code, err)
        rows[ref] = {name: (row.status, row.reason) for name, row in classified.items()}
    return rows


def add_origin_only_trunk(fixture: Fixture) -> None:
    """`trunk` exists only as `refs/remotes/origin/trunk`, at the commit `main` names."""
    git(fixture.repo, "update-ref", "refs/remotes/origin/trunk", "main")
    bare = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "trunk"],
        cwd=str(fixture.repo),
        capture_output=True,
    )
    assert bare.returncode != 0, "git itself cannot resolve the bare name"
    assert resolve_shas(fixture.repo, ["trunk"]) == {
        "trunk": git(fixture.repo, "rev-parse", "main")
    }


def test_a_main_ref_that_exists_only_on_origin_is_compared_by_its_commit(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` took a `--main-ref` it resolves through origin, then handed git the NAME.

    git cannot resolve that name on its own, so every commit read against it failed and the
    run still exited 0 with every inspected row reporting the failure as work.
    """
    add_origin_only_trunk(fixture)

    rows = rows_by_main_ref(
        lambda ref: sweep_argv(fixture, "--main-ref", ref), ("main", "trunk"), monkeypatch, capsys
    )

    assert [r for _, r in rows["trunk"].values() if "could not list" in r] == [], rows["trunk"]
    assert rows["trunk"] == rows["main"]


def test_a_live_branch_is_linked_through_an_origin_only_main_ref(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live set's own off-main commits are read against the same ref, by a second caller.

    Its failure is silent — a live SHA git cannot read against the ref costs only itself — so
    a worktree linked to live work only through a shared off-main commit read DIRTY.
    """
    add_origin_only_trunk(fixture)
    lane = fixture.root / "wt-lane"
    git(fixture.repo, "worktree", "add", "-q", "-b", "g-live", str(lane), "main")
    (lane / "g.txt").write_text("shared\n")
    git(lane, "add", "g.txt")
    git(lane, "commit", "-qm", "shared off-main commit")
    shared = fixture.root / "wt-shared"
    git(fixture.repo, "worktree", "add", "-q", "--detach", str(shared), "g-live")
    (lane / "h.txt").write_text("later\n")
    git(lane, "add", "h.txt")
    git(lane, "commit", "-qm", "the live tip moves on")
    assert git(shared, "rev-parse", "HEAD") != git(lane, "rev-parse", "HEAD"), (
        "SHA equality cannot see this one; only the shared off-main commit links it"
    )

    code, rows, err = main_recording_rows(
        sweep_argv(fixture, "--main-ref", "trunk", "--live-branch", "g-live"), monkeypatch, capsys
    )

    assert (code, err) == (0, "")
    row = rows[shared.name]
    assert row.status == LIVE, f"{row.status}: {row.reason}"
    assert row.reason == "shares a commit off the main ref with the live set"


def test_main_ref_head_is_resolved_by_git_before_the_origin_table(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compares `--main-ref HEAD` with `--main-ref main` while origin's HEAD is elsewhere."""
    git(fixture.repo, "update-ref", "refs/remotes/origin/HEAD", fixture.live_sha)
    assert git(fixture.repo, "rev-parse", "HEAD") != fixture.live_sha

    rows = rows_by_main_ref(
        lambda ref: ["--repo", str(fixture.repo), "--no-gh", "--main-ref", ref],
        ("main", "HEAD"),
        monkeypatch,
        capsys,
    )

    assert rows["HEAD"][fixture.detached.name] == (
        DIRTY,
        "carries 1 commit(s) not on the main ref",
    ), rows["HEAD"]
    assert rows["HEAD"] == rows["main"]


def test_the_main_ref_defaults_to_development(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Feature PRs merge into `development` (E33-S39), so its commits are no worktree's own."""
    git(fixture.repo, "update-ref", "--no-deref", "refs/heads/development", fixture.live_sha)
    assert git(fixture.repo, "rev-parse", "main") != fixture.live_sha
    nobody = LiveSet(shas=frozenset(), branches=frozenset())

    code, rows, err = main_recording_rows(
        ["--repo", str(fixture.repo), "--no-gh"], monkeypatch, capsys
    )

    assert (code, err) == (0, "")
    assert rows[fixture.detached.name].reason == UNLINKED_REASON
    assert fixture.row(fixture.detached.name, nobody).reason == UNLINKED_REASON


def test_a_repo_argument_inside_a_linked_worktree_reads_that_worktrees_whole_index(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runs the sweep from inside a linked worktree with a skip-worktree bit at its root."""
    sub = fixture.stale / "sub"
    sub.mkdir()
    (sub / "s.txt").write_text("s\n")
    git(fixture.stale, "add", "sub/s.txt")
    git(fixture.stale, "commit", "-qm", "sub")
    git(fixture.stale, "update-index", "--skip-worktree", "f.txt")

    code, rows, err = main_recording_rows(
        ["--repo", str(sub), "--no-gh", "--live-sha", fixture.live_sha], monkeypatch, capsys
    )

    assert (code, err) == (0, "")
    assert rows[fixture.stale.name].reason == (
        "carries 1 path(s) git was told not to check, 1 commit(s) not on the main ref"
    )


def test_a_linked_worktree_that_holds_the_main_checkout_has_its_whole_index_read(
    tmp_path: Path,
) -> None:
    """The main checkout moved inside a linked worktree, so its git directory is in that tree."""
    repo = init_repo(tmp_path / "src")
    for name in ("a.txt", "h.txt"):
        (repo / name).write_text(f"{name}\n")
    git(repo, "add", "a.txt", "h.txt")
    git(repo, "commit", "-qm", "base")
    outer = tmp_path / "outer"
    git(repo, "worktree", "add", "-q", "--detach", str(outer), "main")
    git(outer, "update-index", "--skip-worktree", "h.txt")
    (outer / "nested").mkdir()
    moved = repo.rename(outer / "nested" / "src")
    git(moved, "worktree", "repair", str(outer))
    gitdir = Path(git(outer, "rev-parse", "--path-format=absolute", "--git-dir"))
    assert gitdir.is_relative_to(outer.resolve()), "the git directory really is in the tree"

    entries = list_worktrees(moved)
    rows = classify_worktrees(
        moved,
        entries,
        main_path=main_worktree_path(entries),
        live=LiveSet(shas=frozenset(), branches=frozenset()),
        live_only=frozenset(),
    )

    assert {r.worktree.path.name: r.reason for r in rows}["outer"] == (
        "carries 1 modified/untracked path(s), 1 path(s) git was told not to check"
    )
