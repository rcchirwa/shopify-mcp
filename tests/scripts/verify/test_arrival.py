"""E23-S56: the arrival ritual script, ``scripts/verify/arrival.py``.

What the script is for, and what it does NOT guarantee, is stated once in that
module's docstring and is not restated here so the two cannot drift.

Three things specific to these tests:

⚠️ **No test may read the real repository or the real chart.** Every test below
builds a synthetic repo with a bare ``origin`` in ``tmp_path``. A test that
inspected the developer's own checkout would pass here and mean nothing in CI,
and a test that read ``open_stories.json`` would crash in CI, which is gitignored
per-machine and has no chart at all.

⚠️ **``HOME`` and ``GIT_CONFIG_GLOBAL`` are redirected into ``tmp_path`` and every
commit passes ``-c user.name -c user.email``.** Without both, these tests read the
developer's global git config — and CI, which configures no identity, fails at
the first commit.

Ported from Holocron, where coverage did not measure ``scripts/``. In shopify-mcp,
``[tool.coverage.run] source`` includes ``scripts``, so this script is under the 100% gate.
Its ``# kills:`` comments are Holocron's measurements; nothing here keeps them true.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.verify import arrival
from scripts.verify.arrival import main

COMMIT_IDENTITY = [
    "-c",
    "user.name=E23-S56 Test",
    "-c",
    "user.email=e23-s56@example.invalid",
]

VALIDATOR = Path(arrival.__file__).resolve().parent / arrival.VALIDATOR_FILENAME


def _validator_says(chart: Path) -> tuple[int, str]:
    """What the E23-S27 validator ITSELF says about this chart, measured at run time.

    This story is the first programmatic caller of that sibling, whose own docstring
    records that "Nothing runs it automatically, and nothing can" — so its author has
    no pointer back to this suite, and quoting its sentences here would let an
    ordinary rewording there turn this file red from a file that never heard of it.
    Nothing below pins the sibling's wording. These tests assert this script's own
    exit code and behaviour, plus the fact that whatever the validator said reaches
    the operator — with the "whatever" computed here, on the same chart, rather than
    written down.

    ⚠️ THE PREFIX IS APPLIED HERE, from the module's own constant. `arrival` wears
    `VALIDATOR_LINE_PREFIX` on every line of that subprocess's output so no line of
    it can be read as a line of the arrival report, and this helper reproduces that
    by reading the same constant rather than a literal — so the two cannot drift,
    and a test comparing against this helper measures the validator's content
    rather than the prefix.
    """
    done = subprocess.run(
        [sys.executable, str(VALIDATOR), str(chart)],
        capture_output=True,
        text=True,
    )
    prefixed = "".join(
        f"{arrival.VALIDATOR_LINE_PREFIX}{line}\n" for line in done.stdout.splitlines()
    )
    return done.returncode, prefixed


def _record_subprocess_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every argv this run hands to ``subprocess.run``, delegating to the real one."""
    seen: list[list[str]] = []
    real_run = subprocess.run

    def recording(*args: object, **kwargs: object) -> object:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list):
            seen.append([str(part) for part in argv])
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", recording)
    return seen


def _git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stdout, failing loudly on a non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, filename: str, text: str, message: str) -> str:
    (repo / filename).write_text(text, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, *COMMIT_IDENTITY, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _build_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trunk: str = "main",
    head: bool = True,
    work: str = "work",
) -> Path:
    """A repo one commit ahead of a bare ``origin`` whose only branch is ``trunk``.

    ``head`` points ``origin/HEAD`` at ``trunk``; without it no ``origin/HEAD`` exists,
    and ``followRemoteHEAD=never`` stops a git that would create one on fetch.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", f"--initial-branch={trunk}", str(bare))

    repo = tmp_path / work
    _git(tmp_path, "init", f"--initial-branch={trunk}", str(repo))
    _commit(repo, "base.txt", "base\n", "base commit")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", trunk)
    if head:
        _git(repo, "remote", "set-head", "origin", trunk)
    else:
        _git(repo, "config", "remote.origin.followRemoteHEAD", "never")

    _git(repo, "checkout", "-b", "story-branch")
    _commit(repo, "story.txt", "story\n", "story commit")

    monkeypatch.chdir(repo)
    return repo


@pytest.fixture()
def synthetic_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A worktree-shaped repo one commit ahead of a bare ``origin``'s ``main``."""
    return _build_repo(tmp_path, monkeypatch)


def test_refuses_outside_a_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rev-parse`` prints nothing on stdout there, and ``Path("").resolve()`` is the CWD.

    Left unchecked the script would report a toplevel that is not a checkout, look
    for a chart beside it and exit 0 — a pass from a run that measured nothing.

    Git's own stderr is printed alongside the exit code because the script cannot
    tell "not a checkout" from "git could not answer": the same branch is reached
    by a timeout, whose exit code is this script's own 124 and whose stderr says so.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: `git rev-parse --show-toplevel` exited 128" in out
    assert "not a git repository" in out
    assert "toplevel:" not in out


def test_prints_both_rev_list_directions_with_labels(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One count alone cannot tell unpushed work apart from a stale checkout."""
    assert main([]) == 0

    out = capsys.readouterr().out
    assert "git fetch origin: exit 0" in out
    assert "ahead of origin/HEAD (origin/HEAD..HEAD): 1" in out
    assert "behind origin/HEAD (HEAD..origin/HEAD): 0" in out


def test_prints_toplevel_branch_status_and_reflog(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (synthetic_repo / "dirty.txt").write_text("untracked\n", encoding="utf-8")

    assert main([]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {synthetic_repo.resolve()}\n" in out
    assert "branch: story-branch" in out
    assert "?? dirty.txt" in out
    assert "story commit" in out
    # `git log` also prints "story commit"; only the reflog shows HEAD@{N}, and the
    # reflog is the one signal that another session moved HEAD in this worktree.
    assert "HEAD@{0}" in out


def test_the_reflog_is_shown_exactly_five_entries_deep(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The depth is `-5`, and no other test has enough reflog to see it.

    `synthetic_repo` arrives with three entries, so every depth from `-3` upwards
    prints the same thing there. Five is the number every handoff's arrival block
    pastes; unpinned, a drift to `-1` or `--all` is invisible.
    """
    for n in range(5):
        _commit(synthetic_repo, f"extra{n}.txt", f"{n}\n", f"extra commit {n}")

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "HEAD@{4}" in out
    assert "HEAD@{5}" not in out


def test_expect_exits_0_when_origin_head_is_the_named_sha(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    on_origin_head = _git(synthetic_repo, "rev-parse", "origin/HEAD")

    assert main(["--expect", on_origin_head]) == 0
    assert f"origin/HEAD is {on_origin_head} or a descendant of it" in capsys.readouterr().out


def test_expect_exits_1_when_the_named_sha_is_not_an_ancestor_of_origin_head(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The story branch's own commit is unreachable from ``origin/HEAD``."""
    unpushed = _git(synthetic_repo, "rev-parse", "HEAD")

    assert main(["--expect", unpushed]) == 1

    out = capsys.readouterr().out
    assert f"FAIL: origin/HEAD is not {unpushed} nor a descendant of it" in out


def test_refuses_to_run_from_the_repo_venv(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repo ``.venv`` is an editable install pointing at the main checkout."""
    monkeypatch.setattr(sys, "prefix", str(synthetic_repo / ".venv"))

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "REFUSING: sys.prefix" in out
    assert str((synthetic_repo / ".venv").resolve()) in out
    assert "ahead of origin/HEAD" not in out


def test_refusal_resolves_the_prefix_before_comparing(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that only *resolves* into the repo must still be refused.

    ``git rev-parse --show-toplevel`` and ``sys.prefix`` can spell the same
    directory differently (``/tmp`` vs ``/private/tmp`` on macOS, or any symlink
    on the way in). A raw-string comparison lets the repo ``.venv`` through here.
    """
    link = synthetic_repo.parent / "link-to-work"
    link.symlink_to(synthetic_repo, target_is_directory=True)
    monkeypatch.setattr(sys, "prefix", str(link / ".venv"))

    assert main([]) == 1


def test_a_venv_outside_the_repo_is_not_refused(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(synthetic_repo.parent / "elsewhere" / ".venv"))

    assert main([]) == 0


def test_a_sibling_venv_directory_is_not_mistaken_for_the_repo_venv(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<toplevel>/.venv-gate`` is a CI-shaped gate venv, not the repo ``.venv``."""
    monkeypatch.setattr(sys, "prefix", str(synthetic_repo / ".venv-gate"))

    assert main([]) == 0


def test_refuses_the_main_checkouts_venv_from_a_linked_worktree(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Story work happens in linked worktrees, and the repo ``.venv`` is not in one.

    ``<toplevel>/.venv`` alone never matches from a worktree, so the refusal would
    be a no-op in exactly the place every lane runs — while the editable install
    still resolves ``agent`` to the MAIN checkout.
    """
    linked = synthetic_repo.parent / "linked"
    _git(synthetic_repo, "worktree", "add", "-b", "linked-branch", str(linked), "main")
    monkeypatch.chdir(linked)
    monkeypatch.setattr(sys, "prefix", str(synthetic_repo / ".venv"))

    assert main([]) == 1
    assert "REFUSING: sys.prefix" in capsys.readouterr().out


SYMMETRIC_CHART = {
    "stories": [
        {"id": "E01-S01", "status": "open", "mergeSerializedWith": ["E01-S02"]},
        {"id": "E01-S02", "status": "open", "mergeSerializedWith": ["E01-S01"]},
    ]
}


def _write_chart(repo: Path, raw: str) -> None:
    (repo / "open_stories.json").write_text(raw, encoding="utf-8")


def test_chart_steps_are_skipped_when_the_chart_is_absent(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI has no chart — it is gitignored per-machine — so this is the CI path."""
    assert not (synthetic_repo / "open_stories.json").exists()
    argv = _record_subprocess_argv(monkeypatch)

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "no regular file at" in out
    # Named ONCE: outside a linked worktree both candidate roots are the same
    # directory, and an undeduplicated message would print the path twice.
    assert out.count("open_stories.json") == 1
    assert "chart round-trip OK" not in out
    # That the validator did not RUN, asserted on the argv this run spawned rather
    # than on a sentence the validator prints — see `_validator_says`.
    assert argv, "no subprocess ran"
    assert not [spawned for spawned in argv if arrival.VALIDATOR_FILENAME in " ".join(spawned)]


def test_chart_round_trip_and_the_s27_validator_run_when_the_chart_exists(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    code, said = _validator_says(synthetic_repo / "open_stories.json")
    assert code == 0, "a symmetric chart must satisfy the validator, or this proves nothing"
    assert said.strip(), "the validator says nothing here — the passthrough assert would be vacuous"

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "chart round-trip OK" in out
    assert said in out


def test_a_reformatted_chart_fails_the_round_trip_assert(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A chart that does not re-serialise byte-identically was hand-edited."""
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=4) + "\n")

    assert main([]) == 1
    assert "FAIL: chart round-trip" in capsys.readouterr().out


def test_a_same_length_reformatting_of_the_chart_fails_the_round_trip_assert(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The comparison must be by BYTES, not by length.

    Every other round-trip test here feeds an ``indent=4`` chart or invalid JSON,
    both of which a ``len(rewritten) != len(raw)`` check still rejects — so that
    mutant survives all of them. Moving the space from after a key's colon to
    before it is valid JSON of IDENTICAL length: the same-length-mutant trap
    already recorded for E23-S27 in this repo.
    """
    canonical = json.dumps(SYMMETRIC_CHART, indent=2) + "\n"
    same_length = canonical.replace('"id": ', '"id" :')
    assert len(same_length) == len(canonical)
    assert same_length != canonical
    assert json.loads(same_length) == SYMMETRIC_CHART
    _write_chart(synthetic_repo, same_length)

    assert main([]) == 1
    assert "FAIL: chart round-trip" in capsys.readouterr().out


def test_a_chart_holding_unescaped_non_ascii_fails_the_round_trip_assert(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ensure_ascii=True`` is named in the FAIL message and in TESTING_STRATEGY.

    It is ``json.dumps``'s default, so nothing else here would notice it being
    turned off: every other chart in this file is pure ASCII, for which both
    settings emit identical bytes.
    """
    non_ascii = {"stories": [{"id": "E01-S01", "status": "open", "note": "café"}]}
    _write_chart(synthetic_repo, json.dumps(non_ascii, indent=2, ensure_ascii=False) + "\n")

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: chart round-trip" in out
    assert "ensure_ascii=True" in out


def test_a_chart_that_is_not_json_fails_the_round_trip_assert(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_chart(synthetic_repo, "{not json\n")

    assert main([]) == 1
    assert "FAIL: chart round-trip" in capsys.readouterr().out


def test_the_chart_is_found_at_the_main_checkout_root_from_a_linked_worktree(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chart exists only at the main checkout root, and lanes run in worktrees.

    Resolving it against the worktree toplevel alone finds nothing, so both chart
    steps would silently skip in every place they are actually wanted — the same
    no-op E23-S27's own ``parents[2]`` default shipped with.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    said = _validator_says(synthetic_repo / "open_stories.json")[1]
    linked = synthetic_repo.parent / "linked"
    _git(synthetic_repo, "worktree", "add", "-b", "linked-branch", str(linked), "main")
    monkeypatch.chdir(linked)

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "chart round-trip OK" in out
    assert said in out


def test_a_blocking_asymmetry_in_the_chart_fails_the_run(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The S27 validator's exit code must reach this script's exit code."""
    asymmetric = {
        "stories": [
            {"id": "E01-S01", "status": "open", "mergeSerializedWith": ["E01-S02"]},
            {"id": "E01-S02", "status": "open", "mergeSerializedWith": []},
        ]
    }
    _write_chart(synthetic_repo, json.dumps(asymmetric, indent=2) + "\n")
    code, said = _validator_says(synthetic_repo / "open_stories.json")
    assert code != 0, "this chart must fail the validator, or the run's exit 1 proves nothing"

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "chart round-trip OK" in out, "the failure must be the validator's, not the round-trip's"
    assert said in out


def test_refuses_a_venv_symlinked_into_a_linked_worktree(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worktree carries no ``.venv``, so symlinking one in is the natural workaround.

    Built in a linked worktree, not the main checkout, so the venv lives under the
    TOPLEVEL and nowhere near the main checkout root — the only case where the
    `toplevel` argument is the sole match. Re-measured: narrowing the call to
    `repo_venv_containing(prefix, main_root)` fails this test AND NO OTHER. No
    file-wide pass total is quoted here or anywhere below it: seven such totals
    were written against a version of this file collecting 80 items and were all
    stale by the next commit, while the named tests they were attached to were
    right every time.

    ⚠️ THE SYMLINK IS DANGLING ON PURPOSE, and that is the whole difference between
    this test and a vacuous one. `repo_venv_containing` resolves the CANDIDATE as
    well as the prefix, and both this file and that function's docstring said that
    resolve was the only thing standing between the refusal and failing open.
    Re-measured: with this test's symlink made LIVE, as it first shipped,
    dropping the candidate's `.resolve()` leaves the file green — `same_directory`
    grew a `samefile` arm in the next commit, and `samefile` follows the link, so the
    filesystem answered where the resolve used to. It cannot answer for a link whose
    target does not exist: `samefile` raises `FileNotFoundError` there, and the
    resolved candidate is the only thing left that matches. Measured with the symlink
    DANGLING as it is below, the same mutant fails this test and no other. That is
    where the unresolved candidate dies.

    A dangling `.venv` is also the realistic shape of the accident — a worktree kept
    after the venv it pointed at was rebuilt or deleted — and `sys.prefix` naming a
    directory that is not there is exactly what an operator in that state has.
    """
    linked = synthetic_repo.parent / "linked"
    _git(synthetic_repo, "worktree", "add", "-b", "linked-branch", str(linked), "main")
    dangling_target = synthetic_repo.parent / "outside-venv"
    (linked / ".venv").symlink_to(dangling_target, target_is_directory=True)
    assert (linked / ".venv").is_symlink()
    assert not (linked / ".venv").exists(), "the link must DANGLE, or `samefile` answers instead"
    assert not (synthetic_repo / ".venv").exists()
    monkeypatch.chdir(linked)
    monkeypatch.setattr(sys, "prefix", str(linked / ".venv"))

    assert main([]) == 1
    # Not a bare 1: without the refusal this run reaches the fetch and exits 0, so
    # naming the message is what stops a 1 from some later check standing in for it.
    assert "REFUSING: sys.prefix" in capsys.readouterr().out


def test_a_prefix_nested_inside_the_repo_venv_is_refused(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(synthetic_repo / ".venv" / "envs" / "child"))

    assert main([]) == 1


def test_a_failed_fetch_fails_the_run(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every count below the fetch, and `--expect`, are read off `origin/HEAD`.

    Left informational, an offline or broken fetch turns all of them into
    assertions about yesterday's ref that pass without measuring anything.
    """
    _git(synthetic_repo, "remote", "set-url", "origin", str(synthetic_repo.parent / "gone.git"))

    assert main([]) == 1
    assert "FAIL: git fetch origin exited" in capsys.readouterr().out


def test_a_fetch_failure_naming_a_credentialed_url_is_scrubbed_before_printing(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Git echoes the remote URL it failed on, and this repo rotates any printed token.

    Stubbed rather than driven through a real remote: git will not hand a password
    back on a filesystem remote, and a real credentialed URL in a test is the thing
    being prevented. What is pinned is that the fetch-failure path routes stderr
    through `scrub`, and the plain `user:password@` form of it.

    ⚠️ This test is NOT the only thing holding that path. It was when it was written;
    it is not now. Re-measured: printing `fetch.stderr` raw fails FOUR tests — this
    one and the three later ones that stub the same fetch path for the
    token-as-username, at-sign-in-password and `git@`-prefix cases. That is
    redundancy, not waste: each of the four pins a different property of the pattern,
    and the shared path is only how each of them reaches it.
    """
    real_git = arrival.git

    def leaky_fetch(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(
                list(args),
                128,
                "",
                "fatal: could not read from 'https://bob:ghp_SECRETVALUE@example.invalid/r.git'",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_fetch)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "FAIL: git fetch origin exited 128" in out
    assert "ghp_SECRETVALUE" not in out
    assert "bob" not in out
    assert "https://***@example.invalid/r.git" in out


def test_a_toplevel_failure_naming_a_credentialed_url_is_scrubbed_before_printing(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The OTHER path that prints git's stderr, and it was claimed but not measured.

    `rev-parse --show-toplevel` is the one place this script prints stderr that is
    not the fetch's, and it is where git reports a URL it could not resolve — so a
    credentialed remote surfaces on exactly this line. Replacing
    `scrub(where.stderr.strip())` with the bare `where.stderr.strip()`
    left the whole file green before this test existed, and on the shipped tree it
    now fails this test and no other — measured both times. Every other test that
    reaches this branch runs outside a checkout or times out, and git's stderr there
    is `not a git repository` or this script's own timeout message — neither carries
    userinfo at all, so neither can see the scrub go missing.
    """
    real_git = arrival.git
    leak = "fatal: could not read from 'https://bob:ghp_TOPLEVELSECRET@example.invalid/r.git'"

    def leaky_toplevel(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(list(args), 128, "", leak)
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_toplevel)

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: `git rev-parse --show-toplevel` exited 128" in out
    assert "ghp_TOPLEVELSECRET" not in out
    assert "bob" not in out
    assert "https://***@example.invalid/r.git" in out


def test_a_plain_remote_url_in_a_fetch_failure_is_left_readable(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scrub must not blank the URL the operator needs in order to act.

    `ssh://git@host` is a userinfo field that is not a secret, and the exemption for
    it is a literal `git@` — a rule about the VALUE, not about the shape, because
    `https://<token>@host` has the same shape and is a credential.

    Measured counterexample: replacing the pattern with a blanket `//.*@` (and its
    matching `"//***@"` replacement) fails this test — the greedy match runs from the
    FIRST `//` on the line to the LAST `@` on it, so the plain URL and the ssh URL
    collapse into the single string `fatal: 'https://***@example.invalid/r.git'`. Two
    URLs on one line is what makes that measurable; one URL would not show it.

    ⚠️ It is NOT the only test that mutant fails, and this docstring claimed it was
    ("and only this one", with the parenthesis asserting the residuals test still
    passed). Re-measured with the matching replacement named above, it fails this
    test and `test_scrub_leaves_exactly_its_stated_residuals_unmasked` and no
    other — a NAMED pair, not a count. (Left unmatched, the replacement is a
    second mutation and takes the tally to eleven; that is a different mutant.)
    The residuals test fails because the blanket form scrubs the secret out of
    every member of that test's derived class except the newline: `.` crosses
    each of the others, including the `/` the real userinfo run stops at. The two
    pin opposite halves: here that a legitimate userinfo survives, there that an
    illegitimate one is left exactly where the header says it is left.
    """
    real_git = arrival.git

    def failing_fetch(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(
                list(args),
                128,
                "",
                "fatal: 'https://example.invalid/r.git' and 'ssh://git@example.invalid/r.git'",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", failing_fetch)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "https://example.invalid/r.git" in out
    assert "ssh://git@example.invalid/r.git" in out


def test_a_fetch_failure_naming_a_token_as_username_url_is_scrubbed(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A personal access token is documented as the USERNAME, with no password at all.

    GitHub's own instruction for a PAT over HTTPS is
    `https://<token>@github.com/<owner>/<repo>.git` — one userinfo field, no colon
    in it. A scrub narrowed to `user:password@` leaves that URL, and therefore the
    token, verbatim on the fetch-failure path; measured against the earlier
    `(?<=//)[^/@\\s]*:[^/@\\s]*@`, the line came back unchanged.
    """
    real_git = arrival.git

    def leaky_fetch(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(
                list(args),
                128,
                "",
                "fatal: could not read from 'https://ghp_AbCdEf123456789@example.invalid/r.git'",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_fetch)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "ghp_AbCdEf123456789" not in out
    assert "https://***@example.invalid/r.git" in out


def test_a_password_containing_an_at_sign_is_scrubbed_whole(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A userinfo field can hold a literal `@`, and a short match leaks its tail.

    `https://bob:p@ssw0rd@host/r.git` has two `@` signs; a pattern that stops at the
    FIRST one prints `***:***@ssw0rd@host` — measured, and `ssw0rd` is most of the
    password. The match must run to the LAST `@` before the path.
    """
    real_git = arrival.git

    def leaky_fetch(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(
                list(args),
                128,
                "",
                "fatal: could not read from 'https://bob:p@ssw0rd@example.invalid/r.git'",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_fetch)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "ssw0rd" not in out
    assert "https://***@example.invalid/r.git" in out


@pytest.mark.parametrize(
    ("subcommand", "label"),
    [
        (("reflog", "-5"), "git reflog -5:"),
        (("status", "--porcelain"), "git status --porcelain:"),
    ],
)
def test_the_informational_git_output_is_scrubbed_too_not_only_the_fetch_failure(
    synthetic_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    subcommand: tuple[str, ...],
    label: str,
) -> None:
    """The reflog carries the URL of every clone and pull, verbatim and forever.

    Measured in a throwaway repo: after `git pull -q <url> main`, `git reflog -5`
    prints `HEAD@{0}: pull -q <url> main: Fast-forward`, and a fresh clone's
    `HEAD@{0}` is `clone: from <url>`. So a checkout where anyone once ran
    `git pull https://<user>:<token>@host` prints that token on EVERY run of this
    script — not on a failure path, on the ordinary one. Scrubbing only
    `fetch.stderr` passes every other test in this file, all of whose remotes are
    local paths with no userinfo at all.

    `status --porcelain` is parametrized alongside it because it is printed by the
    same unread-stdout shape two lines up: pinning one and not the other leaves the
    other free to regress.
    """
    real_git = arrival.git
    leak = "HEAD@{0}: pull -q https://bob:ghp_REFLOGSECRET@example.invalid/r.git main\n"

    def leaky(*args: str) -> subprocess.CompletedProcess[str]:
        if args[: len(subcommand)] == subcommand:
            return subprocess.CompletedProcess(list(args), 0, leak, "")
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert label in out
    assert "ghp_REFLOGSECRET" not in out
    assert "bob" not in out
    assert "https://***@example.invalid/r.git main" in out


def test_git_is_run_with_terminal_prompting_disabled_and_a_timeout(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git fetch` is a network call; a credential prompt on it would HANG the script.

    A hang returns no exit code at all, which is worse than a failure in a script
    whose only product is one. Asserted on the `subprocess.run` keywords because
    neither is observable in git's output on a remote that needs no auth — which
    is every remote in this file.

    ⚠️ `GIT_ASKPASS=""` IS ASSERTED HERE FOR THE "EVERY CALL" HALF ONLY. What it
    actually stops is measured end to end against a planted helper by
    `test_a_hostile_checkouts_core_askpass_is_not_executed`, which reaches exactly
    one call — the fetch. Prompting-disabled without it is not non-interactive:
    measured on git 2.53.0, an askpass helper is consulted BEFORE the prompt
    `GIT_TERMINAL_PROMPT=0` disables, so the audited checkout's own `core.askPass`
    ran twice per fetch with that variable set. Both are asserted per call, in the
    same loop, because the pair is the property and either alone is not it.
    """
    seen: list[dict[str, object]] = []
    real_run = subprocess.run

    def recording_run(*args: object, **kwargs: object) -> object:
        seen.append(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", recording_run)

    assert main(["--skip-chart"]) == 0

    assert seen, "no subprocess ran"
    for kwargs in seen:
        assert kwargs["timeout"] == arrival.SUBPROCESS_TIMEOUT_SECONDS
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == ""


def test_a_git_call_that_times_out_fails_the_run_instead_of_raising(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `TimeoutExpired` escaping `git()` is an unhandled traceback, not an exit code.

    Every `git()` call times out here, so the run stops at the FIRST one —
    `rev-parse --show-toplevel` — and never reaches the fetch. What this pins is
    that the timeout is converted at all: a non-zero returncode the caller's own
    check reads, rather than an exception crossing `main()`. The fetch path is
    measured separately, by the test below.
    """

    def always_times_out(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1)

    monkeypatch.setattr(arrival.subprocess, "run", always_times_out)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "FAIL: `git rev-parse --show-toplevel` exited" in out
    # The timeout arrives AS the returncode the caller reports, which is what
    # distinguishes this from `git` merely being absent, and git's own stderr —
    # here the message `git()` synthesised — says which it was.
    assert f"exited {arrival.TIMEOUT_RETURNCODE}" in out
    assert "timed out after" in out
    # The LITERALS, once each. Every other reference to either constant in this file
    # — including the one two lines up — interpolates it, so each is otherwise
    # compared only with itself and drifting its value survives the whole suite.
    # Re-measured: with the second line below deleted,
    # `SUBPROCESS_TIMEOUT_SECONDS` drifted to 86400 leaves the whole file
    # green, so the module header's "EVERY `git`
    # CALL IS BOUNDED" survives a bound that no longer bounds a hang. 124 is
    # `timeout(1)`'s own convention, and `test_refuses_outside_a_git_checkout` names
    # it in prose; 120 is two minutes, long enough for a slow fetch and short enough
    # that an operator waiting on this script notices.
    assert arrival.TIMEOUT_RETURNCODE == 124
    assert arrival.SUBPROCESS_TIMEOUT_SECONDS == 120


def test_a_fetch_that_times_out_reports_the_timeout_on_the_fetch_failure_path(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message `git()` synthesises is only ever PRINTED on the fetch path.

    Blanking `git()`'s timeout text to `""` does NOT survive this file, and it did
    not when this test was written: measured, it fails TWO tests — this one and the
    whole-run timeout above, which asserts "timed out after" against the same
    synthesised stderr. What this test adds on top of that one is the INTERPOLATION.
    Measured, narrowing the message's `' '.join(args)` to `args[0]` fails this test
    ALONE: the whole-run test never reaches the fetch, so only here is it visible
    that the message names the command that hung — `git fetch origin`, not `git`.

    `git fetch` is the call that can actually hang: it is the only network one, and
    `GIT_TERMINAL_PROMPT=0` does not reach an ssh key passphrase prompt.

    The subcommand is looked for ANYWHERE in the argv, not at a fixed offset: every
    call carries `HOSTILE_CONFIG_OVERRIDES` between `git` and the subcommand, and an
    `argv[:2] == ["git", "fetch"]` match stopped seeing the fetch the moment those
    were added — measured, this test then asserted 1 against a run that exited 0.
    """
    real_run = subprocess.run

    def times_out_on_the_fetch(*args: object, **kwargs: object) -> object:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv[:1] == ["git"] and "fetch" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", times_out_on_the_fetch)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert f"FAIL: git fetch origin exited {arrival.TIMEOUT_RETURNCODE}" in out
    assert f"`git fetch origin` timed out after {arrival.SUBPROCESS_TIMEOUT_SECONDS}s." in out
    # It reached the fetch, which is what separates this from the whole-run timeout.
    assert "FAIL: `git rev-parse --show-toplevel`" not in out


def test_a_timeout_in_the_three_informational_calls_is_marked_and_does_not_fail(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each of the three says it failed, and none of them fails the run.

    "On one line" is NOT this test's claim, because the stderr it feeds the marker is
    the single-line message `git()` synthesises for a timeout — the one input that
    cannot falsify it. `test_a_multi_line_git_stderr_is_marked_on_exactly_one_line`
    replays a real seven-line git stderr and pins that half.

    ⚠️ THIS TEST USED TO PIN THE OPPOSITE, and the thing it pinned was a defect. It
    asserted that a timed-out `status --porcelain` printed its label and then NOTHING
    — no blank line, no marker — which is byte-identical to what a CLEAN TREE prints,
    because a clean tree makes `status --porcelain` print zero bytes. `reflog -5` has
    the same shape. So a working-tree check that failed or hung rendered as a clean
    tree, in a tool whose entire product is letting an operator trust the tree, and
    this file asserted that as intended behaviour. `unavailable_marker` now prints one
    line naming the exit code and git's stderr, and this test requires it.

    Both halves are still pinned, and both were prose-only before this test existed at
    all. ⚠️ THE COMPOUND MUTANT IS NO LONGER THIS TEST'S ALONE, and the sentence here
    that said it was (`fails this test and no other`, with a file-wide pass total
    this one deliberately does not repeat) was falsified by the very commit that
    wrote it: it was measured before the sibling
    below landed, and never re-run after. Re-measured, all three runs against the same file and each
    naming the tests it fails rather than a total:

    ⚠️ NO COUNT IS WRITTEN FOR ANY OF THEM, and this list carried three. Every
    test that requires exit 0 from a run whose informational call fails is another
    test half (a) breaks, so the number grows whenever one is added — which it did,
    twice, in the commit that last re-measured this list. The tests are named:

    * half (a) alone — a non-zero exit from `status --porcelain` and `reflog -5` sets
      `ok = False` — fails this test,
      `test_a_multi_line_git_stderr_is_marked_on_exactly_one_line`,
      `test_the_marker_collapses_the_whitespace_the_rendering_leaves` and
      `test_the_unavailable_marker_scrubs_the_stderr_it_prints`, each of which
      requires exit 0 from a run whose `status` exits non-zero;
    * half (b) alone — `end=""` dropped from both marker prints — fails this test
      and `test_the_informational_marker_is_absent_when_the_three_calls_succeed`,
      whose tail assertion sees the extra newline that half prints on a SUCCESSFUL
      run;
    * the compound fails the union of the two.

    A mutant that makes the BRANCH LINE's non-zero exit set `ok = False` is still
    this test's alone. The exit contract is what the header's exit
    inventory relies on BY OMISSION, by not listing these three.

    All three are timed out in ONE run, which is what makes the tail assertable: a run
    where only one times out still has the other two printing real git output after
    it, so nothing about the structure of the tail is visible. Here the run ends with
    the two labels each followed by exactly their own marker — false under either half
    of the `end=""` mutant, in either printer — and still exits 0.
    """
    real_git = arrival.git
    informational = {
        ("rev-parse", "--abbrev-ref", "HEAD"),
        ("status", "--porcelain"),
        ("reflog", "-5"),
    }

    def times_out_on_the_informational_calls(*args: str) -> subprocess.CompletedProcess[str]:
        for subcommand in informational:
            if args[: len(subcommand)] == subcommand:
                return subprocess.CompletedProcess(
                    list(args),
                    arrival.TIMEOUT_RETURNCODE,
                    "",
                    f"`git {' '.join(args)}` timed out.",
                )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", times_out_on_the_informational_calls)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    marker = f"(unavailable: `git` exited {arrival.TIMEOUT_RETURNCODE} — "
    assert out.count(marker) == 3, out
    # The branch line is interpolated into its label, so its own timeout still prints
    # the bare label — but the line under it now says the call failed, and the line
    # after THAT is the next real one rather than a blank.
    assert f"branch: \n{marker}`git rev-parse --abbrev-ref HEAD` timed out.)\n" in out
    # `status --porcelain` and `reflog -5` print with `end=""`, so each label is
    # followed by exactly its own marker line and nothing else, and the run ends there.
    assert out.endswith(
        f"git status --porcelain:\n{marker}`git status --porcelain --untracked-files=normal` timed out.)\n"
        f"git reflog -5:\n{marker}`git reflog -5` timed out.)\n"
    ), repr(out[-240:])


def test_the_informational_marker_is_absent_when_the_three_calls_succeed(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other arm of `unavailable_marker`, and the one every other test rides on.

    Without it, a marker printed unconditionally — `unavailable_marker` returning its
    line for a returncode of 0 too — passes the timeout test above, because nothing
    there looks at the shape of a SUCCESSFUL informational line. Re-measured:
    dropping the `if done.returncode == 0: return ""` early return fails this test,
    `test_a_multi_line_git_stderr_is_marked_on_exactly_one_line`,
    `test_the_marker_collapses_the_whitespace_the_rendering_leaves` and
    `test_the_unavailable_marker_scrubs_the_stderr_it_prints` — every test that
    counts marker lines in a run where not every informational call failed. The
    tests are NAMED and not counted: this sentence has said "one" and then "TWO",
    and each was falsified by the next test added to this file.

    Asserted on the tail rather than on a substring, so that "the marker is absent"
    means absent from the report's structure and not merely from one line: the run
    ends with the LAST line of the reflog and nothing after it. That line is read from
    git here rather than written down, so this pins the report's shape and not the
    fixture's commit messages.
    """
    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    reflog = _git(synthetic_repo, "reflog", "-5")
    assert reflog, "the reflog is empty — this tail would prove nothing"
    assert "unavailable:" not in out
    assert out.endswith(reflog.splitlines()[-1] + "\n"), repr(out[-200:])


def test_a_validator_that_times_out_fails_the_run_instead_of_raising(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chart's own subprocess is bounded too, and it is not a `git()` call.

    `check_chart` shells out to the S27 validator directly, so the timeout added to
    `git()` does not cover it and no other test here reaches that `subprocess.run`.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    real_run = subprocess.run

    def times_out_on_the_validator(*args: object, **kwargs: object) -> object:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and any(arrival.VALIDATOR_FILENAME in str(part) for part in argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", times_out_on_the_validator)

    assert main([]) == 1
    assert "FAIL: the S27 validator timed out" in capsys.readouterr().out


def test_the_validator_subprocess_carries_the_timeout_and_no_env(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of what the module docstring claims for the one non-`git()` call.

    The test above monkeypatches `subprocess.run` to RAISE unconditionally and never
    looks at the kwargs it was handed, so it proves only that a `TimeoutExpired` is
    converted — not that anything asks for one. Deleting
    `timeout=SUBPROCESS_TIMEOUT_SECONDS` from the validator call left the whole file
    green before this test existed, and on the shipped tree it now fails this test
    and no other — measured both times. The git half of the same claim was already
    pinned by `test_git_is_run_with_terminal_prompting_disabled_and_a_timeout`.

    The `env` half is asserted alongside it because the docstring claims the two in
    one sentence — "the same `timeout=` and no `env=`". Every `git()` call passes an
    `env=` carrying `GIT_TERMINAL_PROMPT`; this subprocess runs no git and takes
    none, and asserting its ABSENCE is what stops that pair drifting apart.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    seen: list[tuple[list[str], dict[str, object]]] = []
    real_run = subprocess.run

    def recording_run(*args: object, **kwargs: object) -> object:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list):
            seen.append(([str(part) for part in argv], kwargs))
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", recording_run)

    assert main([]) == 0

    validator_calls = [
        kwargs for argv, kwargs in seen if any(arrival.VALIDATOR_FILENAME in part for part in argv)
    ]
    assert len(validator_calls) == 1, seen
    assert validator_calls[0]["timeout"] == arrival.SUBPROCESS_TIMEOUT_SECONDS
    assert "env" not in validator_calls[0], validator_calls[0]


def test_an_unlocatable_main_checkout_fails_the_run(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--git-common-dir` prints NOTHING on failure, and `Path("")` resolves to the cwd.

    Ignoring its exit code makes `main_checkout_root()` the PARENT of the current
    directory — a real path belonging to no checkout, which the `.venv` refusal and
    the chart lookup then both consult. The run must stop instead of measuring it.

    ⚠️ AND IT REPORTS WHAT GIT SAID. This branch used to print one bare sentence
    with neither an exit code nor stderr, and it is the one call whose ANSWER the
    audited checkout can steer — a planted `commondir` under `--repo` points it at
    a directory the operator never named. An operator whose target is tampered
    with, and one whose target is merely broken, got the same line.

    ⚠️ AND IT SCRUBS WHAT IT PRINTS. This is a printer of git's stderr inside a
    `FAIL:` sentence, and its `scrub` was pinned by nothing when it was added:
    measured, swapping it for `render_controls` left the whole file green. The
    stub's stderr therefore carries a token in a remote URL, and both halves are
    read below.

    Named breaks that kill this test, one per property:

    * **print the bare sentence** — `print("FAIL: could not locate the main
      checkout — `git rev-parse --git-common-dir` failed.")`. Observed, each
      assertion evaluated on its own under the break rather than inferred: THREE
      of the seven below turn red — the exit code, git's own stderr and the masked
      `***@` form are all absent from a line that prints no stderr. The other four
      do not discriminate: the run exits 1 either way, the FAIL line prints either
      way, the RAW token is absent either way — which is why the two token
      assertions fall on opposite sides of this break — and the run still stops
      before `toplevel:`.
    * **`render_controls` in place of `scrub`** on that line. Only the two token
      assertions turn red.

    ⚠️ FIXTURE NEUTRALISED, measured: a stub returning an empty stderr and an exit
    code of 0 does not reach this branch at all, so the stub's non-zero exit and
    its message are both load-bearing.
    """
    real_git = arrival.git

    def common_dir_fails(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("rev-parse", "--git-common-dir"):
            # The URL carries a token because this printer's `scrub` had no test.
            return subprocess.CompletedProcess(
                list(args),
                128,
                "",
                "fatal: not a git repository: https://bob:ghp_COMMONDIRSECRET@h/r.git",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", common_dir_fails)

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: could not locate the main checkout" in out, out
    assert "exited 128" in out, out
    assert "fatal: not a git repository" in out, out
    assert "ghp_COMMONDIRSECRET" not in out, out
    assert "***@h/r.git" in out, out
    assert "toplevel:" not in out


def test_the_refusal_says_IS_when_the_prefix_is_the_repo_venv_itself(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "`X` is inside `X`" reads as a bug in the check to whoever it stops.

    The nested case above keeps the "inside" wording honest, so a single message
    cannot be right for both.
    """
    monkeypatch.setattr(sys, "prefix", str(synthetic_repo / ".venv"))

    assert main([]) == 1

    out = capsys.readouterr().out
    assert f"REFUSING: sys.prefix IS the repo venv {(synthetic_repo / '.venv').resolve()}." in out
    assert "is inside the repo venv" not in out


def test_the_refusal_says_INSIDE_when_the_prefix_is_nested_under_the_repo_venv(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = synthetic_repo / ".venv" / "envs" / "child"
    monkeypatch.setattr(sys, "prefix", str(nested))

    assert main([]) == 1

    out = capsys.readouterr().out
    assert f"REFUSING: sys.prefix {nested.resolve()} is inside the repo venv" in out
    assert "sys.prefix IS the repo venv" not in out


def test_a_missing_origin_head_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rev-list` prints nothing when `origin/HEAD` does not exist, and exits 128."""
    _build_repo(tmp_path, monkeypatch, head=False)

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: could not count against origin/HEAD" in out


def test_the_main_checkout_chart_wins_over_a_worktree_local_one(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ordering candidates by path string lets a stray worktree copy shadow the real chart.

    An agent worktree under `<main>/.claude/worktrees/<x>` sorts BEFORE `<main>`
    itself, so the shadowing is deterministic, not a race.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    linked = synthetic_repo / ".claude" / "worktrees" / "wt"
    _git(synthetic_repo, "worktree", "add", "-b", "linked-branch", str(linked), "main")
    one_row = {"stories": [{"id": "E01-S01", "status": "open", "mergeSerializedWith": []}]}
    _write_chart(linked, json.dumps(one_row, indent=2) + "\n")
    monkeypatch.chdir(linked)
    # The two charts are told apart by what the validator itself says about each,
    # measured now — not by a sentence of the validator's copied into this file.
    canonical = _validator_says(synthetic_repo / "open_stories.json")[1]
    shadowing = _validator_says(linked / "open_stories.json")[1]
    assert canonical != shadowing, "the two charts read alike — this test could not tell them apart"

    assert main([]) == 0

    out = capsys.readouterr().out
    assert canonical in out
    assert shadowing not in out


def test_the_chart_is_read_from_where_it_exists_not_from_the_first_candidate(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chart read is `present[0]`, not `candidates[0]`.

    The main checkout root is the FIRST candidate and the one that normally holds
    the chart, so `candidates[0]` passes every other test here. With a chart in
    the worktree and none at the main root it names a path that does not exist and
    `read_text` raises FileNotFoundError, where this run must exit 0.
    """
    linked = synthetic_repo.parent / "linked"
    _git(synthetic_repo, "worktree", "add", "-b", "linked-branch", str(linked), "main")
    _write_chart(linked, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    assert not (synthetic_repo / "open_stories.json").exists()
    monkeypatch.chdir(linked)
    said = _validator_says(linked / "open_stories.json")[1]

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "chart round-trip OK" in out
    assert said in out


def test_the_validators_stderr_is_surfaced(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise a validator that cannot run exits the script 1 with no explanation.

    ⚠️ IT IS PRINTED AS LINES, and only its STDOUT sibling measured that. Both
    validator printers pass the same `keep_lines=True`; flipping it on the stdout
    one turns several tests red, while flipping it on this one left the whole file
    green — an exemption held by nothing on the half that carries a traceback.

    A real interpreter error here is ONE line ("can't open file ..."), which is
    why the missing-script fixture alone could never see this: the stub below
    supplies the multi-line stderr a failing validator actually produces, and the
    missing-script run above keeps the reachability claim honest.

    Named break that kills this test: **drop `keep_lines=True` from the
    validator's stderr printer**. Only the multi-line assertion turns red; the
    stderr is still surfaced and the run still exits 1.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    monkeypatch.setattr(arrival, "VALIDATOR_FILENAME", "no_such_validator.py")

    assert main([]) == 1
    assert capsys.readouterr().err.strip() != ""

    traceback_shaped = 'Traceback (most recent call last):\n  File "x", line 1\nValueError: boom\n'
    prefixed = "".join(
        f"{arrival.VALIDATOR_LINE_PREFIX}{line}\n" for line in traceback_shaped.splitlines()
    )
    real_run = subprocess.run

    def failing_validator(argv, **kwargs):  # type: ignore[no-untyped-def]
        if any(arrival.VALIDATOR_FILENAME in str(part) for part in argv):
            return subprocess.CompletedProcess(argv, 1, "", traceback_shaped)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_validator)

    assert main([]) == 1
    err = capsys.readouterr().err
    assert err == prefixed, err


def test_skip_chart_leaves_a_broken_chart_alone(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator whose errand is a diff has no business being blocked by the chart.

    The chart is gitignored, per-machine and hand-edited by several concurrent
    sessions, so coupling every arrival to its byte formatting is how a check
    becomes permanently red and then gets silenced.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=4) + "\n")
    argv = _record_subprocess_argv(monkeypatch)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "chart round-trip" not in out
    # As above: that the validator did not RUN, read off this run's own argv.
    assert argv, "no subprocess ran"
    assert not [spawned for spawned in argv if arrival.VALIDATOR_FILENAME in " ".join(spawned)]


def test_a_failed_fetch_does_not_suppress_the_chart_check(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both failures must be reported from one run, not the first one only."""
    _git(synthetic_repo, "remote", "set-url", "origin", str(synthetic_repo.parent / "gone.git"))
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=4) + "\n")

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: git fetch origin exited" in out
    assert "FAIL: chart round-trip" in out


@pytest.mark.parametrize("failing", ["origin/HEAD..HEAD", "HEAD..origin/HEAD"])
def test_one_failing_rev_list_direction_fails_the_run(
    synthetic_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    """Either count failing alone means the pair cannot be read against origin/HEAD.

    Parametrized because stubbing ONE direction measures one half of the `and`:
    with only `HEAD..origin/HEAD` stubbed, narrowing the guard to
    `if behind.returncode == 0:` survived this whole file.

    Neither direction can fail alone against real git — both name the same two
    refs — so that mutant was behaviourally equivalent in production, and this
    half is reachable only under the same monkeypatch the other half already
    needed. What is pinned is the guard's SHAPE: both counts, not one.

    ⚠️ THE LINE REPORTS, IT DOES NOT DIAGNOSE, and asserting its prefix alone let
    it diagnose for two commits. It used to end "no such ref in this checkout",
    which is one cause among several and the only one this branch cannot tell
    from the others: a timeout (`git()` synthesises exit 124), a corrupt object
    and an unborn HEAD all arrive here, and were all reported as a missing ref.
    The stub below therefore returns an exit code and a stderr that say something
    ELSE, and the assertions read both — a run that guesses cannot reproduce
    either.

    ⚠️ AND THE LINE SCRUBS WHAT IT PRINTS. It is the third printer that puts
    git's stderr inside a `FAIL:` sentence, and the two older ones each have a
    credentialed-URL test while this one shipped with none — the same unpinned
    printer this file corrected on `unavailable_marker` one commit earlier. The
    stub's stderr therefore carries a token in a remote URL as well as a timeout's
    exit code, and both properties are read below.

    Named break that kills this test: **go back to the diagnosis** —
    `print("FAIL: could not count against origin/HEAD — no such ref in this
    checkout.")`. Observed, each assertion evaluated on its own under the break
    rather than inferred: THREE of the six below turn red — the exit code, git's
    own stderr and the masked `***@` form are all absent from a line that prints
    no stderr at all. The other three do not discriminate: the run exits 1 either
    way, the FAIL line prints either way, and the RAW token is absent either way,
    which is the difference between the two token assertions and why they fall on
    opposite sides of this break. The break also takes
    `test_a_multi_line_git_stderr_cannot_split_the_line_it_is_printed_on`'s
    `rev-list` case red, which is the case the same commit added — named here
    rather than claimed as "this test and no other", which it was.

    Second named break, for the scrub half: **`render_controls` in place of
    `scrub`** on this line. Only the token assertion turns red.

    ⚠️ FIXTURE NEUTRALISED, measured: the exit code and message in the stub are
    NOT what give this test its teeth against the diagnosis — a stub carrying a
    genuinely-missing-ref exit and message, with the assertions retargeted, is
    red under the diagnosis just the same, because the diagnosis prints neither.
    What the timeout shape buys is the READER: it names a cause the line cannot
    have determined, so the test's subject is legible. The token, by contrast, IS
    load-bearing — remove it and the scrub half measures nothing.
    """
    real_git = arrival.git

    def one_direction_fails(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:3] == ("rev-list", "--count", failing):
            # NOT a missing ref: the exit code and the message are a timeout's,
            # so a line that names a missing ref cannot produce either. The URL
            # carries a token because this printer's `scrub` had no test.
            return subprocess.CompletedProcess(
                list(args),
                124,
                "",
                "git rev-list timed out after 8.0s: https://bob:ghp_REVLISTSECRET@h/r.git",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", one_direction_fails)

    assert main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL: could not count against origin/HEAD" in out, out
    assert "exited 124" in out, out
    assert "git rev-list timed out after 8.0s" in out, out
    assert "ghp_REVLISTSECRET" not in out, out
    assert "***@h/r.git" in out, out


def test_runs_from_a_subdirectory_of_the_checkout(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--git-common-dir` comes back RELATIVE to the cwd, not to the toplevel.

    Asserted on `main_checkout_root()` directly: through `main()` the toplevel
    candidate finds the same chart, so the whole run still exits 0 and prints exactly
    what a correct run prints, with `main_checkout_root()` returning `<repo>/sub` —
    the resolution this docstring is about goes unmeasured. (The sibling validator's
    own sentences are deliberately not quoted anywhere in this file; see
    `_validator_says` for why, and this docstring used to be the one place that
    still did.)
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    said = _validator_says(synthetic_repo / "open_stories.json")[1]
    sub = synthetic_repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    # The PROPERTY, not git's spelling of it: `--git-common-dir` comes back relative,
    # and relative to the CWD rather than to the toplevel. Pinning the literal
    # "../.git" fails on any git that renders the same fact differently, which would
    # read as a regression in this script.
    relative = _git(sub, "rev-parse", "--git-common-dir")
    assert not Path(relative).is_absolute()
    assert (sub / relative).resolve() == (synthetic_repo / ".git").resolve()
    # Returns the path BESIDE the git call, so the caller can report what git
    # said when it fails rather than a bare sentence.
    root, common = arrival.main_checkout_root()
    assert root == synthetic_repo.resolve()
    assert common.returncode == 0, common.stderr

    assert main([]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {synthetic_repo.resolve()}\n" in out
    assert said in out


def test_repo_runs_the_checks_in_the_named_directory(
    synthetic_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without `--repo` the script audits the cwd, which is how it passed on `main`.

    Run from the main checkout while the branch sat in a worktree, it printed that
    checkout's toplevel, branch and counts and exited 0 — a pass about the wrong
    tree, from an operator who believed they had measured the branch.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert main(["--repo", str(synthetic_repo)]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {synthetic_repo.resolve()}\n" in out
    assert "branch: story-branch" in out
    assert "FAIL: `git rev-parse --show-toplevel`" not in out


def test_repo_restores_the_working_directory_it_was_called_in(
    synthetic_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chdir is undone even on the failing paths, which run in-process here.

    Left in place, one `--repo` run silently relocates every later caller in the
    same process — including the rest of this suite, whose fixtures chdir once and
    then assert on a cwd nothing else touches.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    before = Path.cwd()

    assert main(["--repo", str(synthetic_repo)]) == 0
    assert Path.cwd() == before

    assert main(["--repo", str(synthetic_repo), "--expect", "0" * 40]) == 1
    assert Path.cwd() == before


def test_repo_restores_the_working_directory_when_the_run_raises(
    synthetic_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `finally` buys exactly one thing, and only a RAISE measures it.

    Every path the rest of this file exercises RETURNS, so
    `result = _run(args); os.chdir(original); return result` restores the directory
    on all of them and survives the whole suite — branch coverage included, since
    that mutant has no branch. Raising is still reachable in production, though the
    route this docstring first named is not: `check_chart`'s read is now inside its
    own `try`, so an unreadable chart RETURNS. What remains is every `git()` call —
    a `subprocess.run` that raises `FileNotFoundError` when `git` is not on PATH,
    before `git()`'s own `except TimeoutExpired` can see it. Unrestored, one
    `--repo` run parks the whole process in the audited tree.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    before = Path.cwd()

    def boom(_args: argparse.Namespace) -> int:
        raise RuntimeError("the chart could not be read")

    monkeypatch.setattr(arrival, "_run", boom)

    with pytest.raises(RuntimeError, match="the chart could not be read"):
        main(["--repo", str(synthetic_repo)])

    assert Path.cwd() == before


def test_repo_that_cannot_be_entered_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped path must not fall through to the cwd and audit it instead."""
    monkeypatch.chdir(tmp_path)

    assert main(["--repo", str(tmp_path / "no-such-worktree")]) == 1

    out = capsys.readouterr().out
    assert "FAIL: --repo" in out
    assert "toplevel:" not in out


def test_expect_a_revision_that_is_not_a_commit_here_says_so(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`merge-base --is-ancestor` exits 128 on an unknown object, not 1.

    Reported as an ancestry failure it tells the operator the wrong thing: the
    branch is not behind, the sha was mistyped or has been garbage-collected.

    ⚠️ THE LINE REPORTS WHAT GIT SAID, and used to name the cause. "is not a
    commit in this checkout" is one cause among several and cannot be told from a
    `cat-file -e` that timed out (`git()` synthesises 124) or was killed — the
    same defect the `rev-list` line above was corrected for, left standing one
    branch away because the correction was scoped by the phrase "every other FAIL
    branch". The assertions read git's exit code and git's own message.

    ⚠️ AND IT SCRUBS WHAT IT PRINTS. Like the two printers beside it this one puts
    git's stderr inside a `FAIL:` sentence, and its `scrub` was pinned by nothing
    when it was added: measured, swapping it for `render_controls` left the whole
    file green. The second run below stubs a stderr carrying a token so the
    masking half is read too.

    Named break that kills the second half: **`render_controls` in place of
    `scrub`** on that line. Only the two token assertions turn red.
    """
    absent = "0" * 40

    assert main(["--expect", absent]) == 1
    out = capsys.readouterr().out
    assert f"FAIL: --expect {absent}: `git cat-file -e` exited 128" in out, out
    assert "Not a valid object name" in out, out

    real_git = arrival.git

    def leaky_cat_file(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("cat-file", "-e"):
            return subprocess.CompletedProcess(
                list(args), 128, "", "fatal: https://bob:ghp_CATFILESECRET@h/r.git is unreadable"
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_cat_file)

    assert main(["--expect", absent]) == 1
    out = capsys.readouterr().out
    assert "ghp_CATFILESECRET" not in out, out
    assert "***@h/r.git" in out, out


def test_expect_a_tree_sha_is_reported_as_not_a_commit(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `^{commit}` peel is what makes `cat-file -e` a COMMIT check.

    A tree sha IS an object here, so an unpeeled `cat-file -e` exits 0 and the run
    falls through to `merge-base --is-ancestor`, which exits 128 on a non-commit —
    the same 128 branch that means "there is no origin/HEAD here". Exit 1 either
    way, and the operator is sent to look at the wrong thing.
    """
    tree = _git(synthetic_repo, "rev-parse", "HEAD^{tree}")

    assert main(["--expect", tree]) == 1

    out = capsys.readouterr().out
    assert f"FAIL: --expect {tree}: `git cat-file -e` exited 128" in out, out
    # git's own words for the distinction this test is about: a tree is an
    # object, and it is `^{commit}` that it fails to satisfy.
    assert "dereferences to tree type" in out, out
    assert "could not compare against origin/HEAD" not in out


def test_expect_says_so_when_origin_head_cannot_be_resolved_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`merge-base --is-ancestor` exits 128, not 1, when `origin/HEAD` is missing.

    The sha IS a commit here, so the not-a-commit branch above does not catch it,
    and a bare `!= 0` reads 128 as "not an ancestor" — telling the operator the
    branch is behind when the truth is that there is nothing to be behind.
    """
    repo = _build_repo(tmp_path, monkeypatch, head=False)
    local = _git(repo, "rev-parse", "HEAD")

    assert main(["--expect", local]) == 1

    out = capsys.readouterr().out
    assert f"FAIL: --expect {local}: could not compare against origin/HEAD" in out
    assert "nor a descendant of it" not in out


def test_expect_rejects_a_value_git_would_read_as_an_option(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--expect` reaches two git commands positionally, with no `--` separator.

    `git cat-file -e <rev>` and `git merge-base --is-ancestor <rev> origin/HEAD` both
    take it as a bare argument, so git parses a leading dash as an option. Measured
    on this git: `git cat-file -e '-oops^{commit}'` and
    `git merge-base --is-ancestor -oops origin/main` each exit 129 with
    "error: unknown switch `o'" — so today the value is contained by which switches
    those two commands happen not to have, not by anything this script does. Rejected
    up front, the containment stops depending on git's option table.
    """
    assert main(["--expect=-oops"]) == 1

    out = capsys.readouterr().out
    assert "FAIL: --expect -oops: a revision may not start with '-'." in out
    # It never reached `cat-file`, whose failure has a different message.
    assert "is not a commit in this checkout" not in out


def test_an_argparse_error_renders_the_argument_it_echoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse echoes an unrecognised argument into its error message as typed.

    Printed raw, an argument holding a newline put ``chart round-trip OK`` on a
    line of its own in a run that never opened a chart. The message is a string
    this script did not write, so it is rendered: the argument stays visible as
    text, with its newline escaped.

    Named break that kills this test: **build the parser as a plain
    ``argparse.ArgumentParser``** in ``main``. The forged line is back at column
    0 and the escaped form is absent.
    """
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["--skip-chart", "x\nchart round-trip OK"])

    err = capsys.readouterr().err
    assert excinfo.value.code == 2, err
    assert "unrecognized arguments: x\\nchart round-trip OK" in err, err
    assert "chart round-trip OK" not in err.split("\n"), err


def _planted_script(path: Path, marker: Path) -> Path:
    """An executable script that records that it ran, then fails."""
    path.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_hostile_checkouts_core_fsmonitor_is_not_executed(
    synthetic_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--repo` points at a checkout whose own `.git/config` has not been read yet.

    Measured against the earlier code: a checkout carrying
    `core.fsmonitor=<script>` in its own config ran that script during
    `git status --porcelain`, and the run still exited 0 — so the operator learned
    nothing had gone wrong. `git()` now prefixes every call with
    `-c core.fsmonitor=false`, and a command-line `-c` outranks every config file.
    """
    marker = tmp_path / "fsmonitor-ran"
    _git(
        synthetic_repo, "config", "core.fsmonitor", str(_planted_script(tmp_path / "fs.sh", marker))
    )
    monkeypatch.chdir(tmp_path)

    assert main(["--repo", str(synthetic_repo), "--skip-chart"]) == 0
    assert not marker.exists(), "the audited checkout's own config ran a command"


def test_a_hostile_checkouts_ext_transport_is_not_executed(
    synthetic_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audited checkout can raise `protocol.ext.allow` in its own config.

    Measured on git 2.53.0: `ext::` is refused by default even for a remote the
    user names directly — so an `ext::` URL alone is not the hole. The hole is that
    the SAME hostile config can carry `protocol.ext.allow=always`, and then
    `git fetch origin` runs the named command; measured, it did, and the earlier
    code ran it too. `-c protocol.ext.allow=never` outranks that config file.

    The run still exits 1 either way — the fetch fails — so the exit code cannot
    tell the two apart. Only the marker can.
    """
    marker = tmp_path / "ext-ran"
    planted = _planted_script(tmp_path / "ext.sh", marker)
    _git(synthetic_repo, "remote", "set-url", "origin", f"ext::{planted}")
    _git(synthetic_repo, "config", "protocol.ext.allow", "always")
    monkeypatch.chdir(tmp_path)

    assert main(["--repo", str(synthetic_repo), "--skip-chart"]) == 1
    assert not marker.exists(), "the audited checkout's own config ran a command"


def test_a_hostile_checkouts_own_hook_is_not_executed(
    synthetic_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config keys are not the only thing in a checkout that names a command.

    `--repo` enters a directory whose `.git/hooks` the operator has not read either,
    and `git fetch origin` updates a remote-tracking ref, which fires
    `reference-transaction`. Measured on git 2.53.0 with only the two config
    overrides in place: the planted hook RAN during the fetch and the run exited 0 —
    the same shape and the same silent outcome as `core.fsmonitor`. `git()` now also
    passes `-c core.hooksPath=/dev/null`; measured, that blocked it, and the control
    without the override ran it.

    Two things about the fixture, both measured rather than assumed:

    - The tracking ref is deleted BEFORE the hook is planted. `reference-transaction`
      fires on any ref update, so a `refs/remotes/origin/main` left in place makes
      the fetch a no-op that updates nothing and the hook would not run even without
      the override — a green test measuring nothing. Deleting it after planting is
      worse still: the delete is itself a transaction that fires the hook.
    - The hook exits 0, unlike `_planted_script`. A non-zero `reference-transaction`
      hook ABORTS the transaction, so the shape being reproduced — a command that
      runs while the run reports success — needs the hook to let the fetch through.
      Measured: with `exit 1`, `git update-ref -d` itself exits 128.
    """
    marker = tmp_path / "hook-ran"
    hooks = synthetic_repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    _git(synthetic_repo, "update-ref", "-d", "refs/remotes/origin/main")
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    assert not marker.exists()
    monkeypatch.chdir(tmp_path)

    assert main(["--repo", str(synthetic_repo), "--skip-chart"]) == 0
    assert not marker.exists(), "the audited checkout's own hook ran a command"

    # The control: the same fetch, in the same tree, without the override. If the
    # hook does not fire here at all, the assertion above proves nothing.
    _git(synthetic_repo, "update-ref", "-d", "refs/remotes/origin/main")
    marker.unlink(missing_ok=True)
    _git(synthetic_repo, "fetch", "origin")
    assert marker.exists(), "the hook does not fire here at all — the assertion above is vacuous"


def test_every_git_call_disables_the_config_keys_that_run_commands(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three settings on EVERY call, as literals, in order — `credential.helper` on none.

    The three tests above prove `core.fsmonitor`, the `ext::` transport and the
    checkout's own hooks end to end, one call at a time; this says the prefix is on
    all of them.

    The `credential.helper` half is a NEGATIVE assertion on purpose. That key is the
    same class and worse — `!<cmd>` runs a shell command, and can hand over the
    operator's stored HTTPS credentials — so clearing it reads like an obvious
    improvement, and this test exists to stop the next reader making it silently.
    Measured on this repository, whose `origin` is `https://github.com/...` behind
    `osxkeychain`: `git fetch origin` exits 0, and `git -c credential.helper= fetch
    origin` fails with `could not read Username ... terminal prompts disabled`. So
    clearing it makes the fetch — and `origin/HEAD`, both counts, and `--expect` —
    fail on the ordinary path for every operator. Re-adding it must turn this red.

    `core.sshCommand` is held out by the same kind of negative assertion, for the
    same kind of reason and with its own measurement: it DOES run a planted script
    during the fetch against an `ssh://` origin, but measured on git 2.53.0,
    `-c core.sshCommand=` is honoured as the command rather than read as "unset"
    (`error: cannot run : No such file or directory` / `fatal: unable to fork`,
    against a control that reached the real `ssh`), and `-c core.sshCommand=ssh`
    clobbers an operator's own `core.sshCommand` — measured, their script did not run
    and plain `ssh` did. Both spellings cost the ordinary path, which is the
    `credential.helper` trade again. The module records that; this keeps it.

    `filter.<driver>.clean` is held out too, and it is the MOST REACHABLE of the keys
    held out — named by key rather than by ordinal, here and in the module, because
    an ordinal is renumbered silently by the next key anyone adds or closes: it needs
    no remote, no network and no origin URL. Measured on git 2.53.0
    with all three overrides above already applied, against a checkout carrying
    `filter.evil.clean=<script>` in its own config and `* filter=evil` in its own
    `.gitattributes`, the script RAN during `git status --porcelain` — the local-only
    working-tree check — and the run exited 0. Two things are needed to reproduce it,
    both measured: the worktree file must differ from the index at the SAME SIZE
    (where the sizes differ git can call the file modified without hashing it, so the
    clean driver is never reached — with a size-changing edit the script did not run,
    in the control either), and the control matters, because plain `git status
    --porcelain` in the
    same tree ran it too, so it is not the overrides letting it through. It is held
    out because nothing here would close it: driver names are arbitrary, so
    `-c filter.<driver>.clean=` names one driver, and git has no blanket switch and no
    way to ignore an in-tree `.gitattributes` from the command line. Adding a
    `-c filter....` must turn this red, so the next reader has to read that before
    adding an override that closes nothing.

    Asserted against literal strings rather than against
    `arrival.HOSTILE_CONFIG_OVERRIDES`: comparing the constant with itself would
    pass whatever the constant happened to say.
    """
    argv = _record_subprocess_argv(monkeypatch)

    assert main(["--skip-chart"]) == 0

    git_calls = [spawned for spawned in argv if spawned[:1] == ["git"]]
    assert git_calls, "no git subprocess ran"
    for spawned in git_calls:
        assert spawned[1:7] == [
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "core.hooksPath=/dev/null",
        ], spawned
        held_out = ("credential.helper", "core.sshCommand", "filter.")
        assert not [part for part in spawned if part.startswith(held_out)], spawned


def test_a_userinfo_that_merely_begins_git_at_is_still_scrubbed(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `git@` exemption has to be a WHOLE userinfo, not a prefix of one.

    The lookahead runs once, at the position after `//`, while the match is greedy
    to the LAST `@`. So a bare `(?!git@)` exempted any userinfo that merely BEGAN
    `git@`, and an attacker-chosen — or simply unlucky — username of `git` in front
    of a real secret disabled the whole scrub: measured,
    `https://git@SECRET@host/x` came back verbatim.
    """
    real_git = arrival.git

    def leaky_fetch(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(
                list(args),
                128,
                "",
                "fatal: could not read from 'https://git@ghp_TRAILINGSECRET@example.invalid/r.git'",
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_fetch)

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "ghp_TRAILINGSECRET" not in out
    assert "https://***@example.invalid/r.git" in out


def _expected_rendering(char: str, spared: str) -> str:
    """What ``render_controls`` must return for one character, stated independently.

    The rule is written here rather than read from the module, and ``spared`` is
    passed in by the caller as a literal: read from ``RENDERED_VERBATIM`` instead,
    every assertion below would agree with any widening of it.
    """
    return char if char.isprintable() or char in spared else repr(char)[1:-1]


def test_render_controls_exempts_exactly_printable_plus_the_named_lines() -> None:
    r"""The exemption's CEILING, over EVERY codepoint rather than over a class.

    ``RENDERED_VERBATIM`` records a report that was forged while the exemption was
    a default, so its WIDTH is the property worth holding, and a widening can be
    spelled at the constant or at any use site. An earlier draft bounded this by
    the residual class, which only catches a widening to a character that class
    contains: measured there, a use-site widening to U+2067, to U+202D or to
    U+200B left every test in this file green. U+2067 RIGHT-TO-LEFT ISOLATE
    reopens the same CLASS of bidi forgery the module header measured with
    U+202E, though not that same rendering, which needs its own terminators; the
    other two are non-printing characters the exemption would equally pass
    through. They reach the transcript through a printer that passes
    ``keep_lines=True`` and adds no line prefix of its own.

    Driven one character at a time AND in two neighbourhoods, because neither
    form sees what the other does — each measured against a renderer that
    survived the other.
    """
    every = [chr(codepoint) for codepoint in range(sys.maxunicode + 1)]
    for keep_lines, spared in ((False, ""), (True, "\t\n")):
        # ONE AT A TIME, and the whole rendering, not just the absence of the raw
        # character. A renderer that short-circuits on length (`if len(text) < 2:
        # return text`) returns a lone character verbatim and is invisible to any
        # block form; one that DROPS what it will not print, or changes the escape
        # it writes, satisfies a mere absence check. Both were measured surviving
        # the block-only draft.
        wrong = [
            char
            for char in every
            if arrival.render_controls(char, keep_lines=keep_lines)
            != _expected_rendering(char, spared)
        ]
        assert not wrong, (keep_lines, [hex(ord(char)) for char in wrong[:20]])

        rest = [char for char in every if _expected_rendering(char, spared) != char]

        # AND AT THE SHORT LENGTHS BETWEEN. The loop above is length 1 and the
        # blocks below are the whole range, so a threshold that excludes only
        # some middle length sits between them unseen: measured, both `if
        # len(text) == 2` and `if len(text) == 40: return text` left the whole
        # file green when the widths driven here stopped short of them. The
        # larger widths are the lengths a real report line and a real stderr
        # block have.
        for width in (*range(2, 17), 40, 200, 4096):
            chunk = "".join(rest[:width])
            assert arrival.render_controls(chunk, keep_lines=keep_lines) == "".join(
                _expected_rendering(char, spared) for char in chunk
            ), (keep_lines, width)

        # AND IN CONTEXT. An exemption conditioned on its neighbours is invisible
        # to the loop above, where every character stands alone: measured, one
        # let a raw bidi isolate through while leaving the whole file green. Two
        # joiners because they present opposite neighbours — one non-printing,
        # one printable — so a condition keyed on either kind is driven.

        for joiner in ("", "x"):
            rendered = arrival.render_controls(joiner.join(rest), keep_lines=keep_lines)
            leaked = sorted(set(rendered) & set(rest))
            assert not leaked, (keep_lines, joiner, [hex(ord(char)) for char in leaked[:20]])
            assert rendered == joiner.join(_expected_rendering(char, spared) for char in rest), (
                keep_lines,
                joiner,
            )


def _in_field_defeating_characters() -> list[str]:
    """Every character that ends ``[^/\\s]+`` before the ``@``, derived not listed.

    The header states the in-field residual as a CLASS. This RESTATES the
    pattern's negated set rather than reading it, so on its own it tracks a
    widening of ``\\s`` and not a widening of the pattern. The caller closes that
    gap by measuring the real class and asserting this reproduces it.
    """
    return sorted({"/"} | {chr(c) for c in range(sys.maxunicode + 1) if re.match(r"\s", chr(c))})


def test_scrub_leaves_exactly_its_stated_residuals_unmasked() -> None:
    """The module header states the residuals; nothing else measures them.

    Each is true on the shipped tree, but prose that nothing runs is prose that a
    later widening of `URL_USERINFO` falsifies with the suite green — in the
    module docstring and in the `TESTING_STRATEGY.md` row that copies it. Most
    other tests reach `scrub` through a PRINTER, where a residual reads as "no
    leak" and is indistinguishable from a scrub, so this one calls it directly.

    The IN-FIELD class is derived rather than listed. The OUT-OF-FIELD members
    the header pins are listed, because they are positions rather than
    characters and no rule generates them.

    The residual asserted for every member is the SECRET SURVIVING, which is what
    the name says. Byte-identity holds for only some of them and is asserted per
    member by the rule below, not claimed for the class.

    The rendering half is asserted as the rule it follows rather than as a list
    of members: a defeating character comes back byte-identical when it is
    printable, and additionally under `keep_lines=True` when `RENDERED_VERBATIM`
    names it. That is a statement about THIS CLASS, and it bounds what it can
    catch to this class. The exemption's own width is a property of the renderer
    and is pinned over every codepoint by
    `test_render_controls_exempts_exactly_printable_plus_the_named_lines`.

    The positive control below guards the out-of-field loop: a scrub that
    returned its input unchanged would satisfy every assertion in it and measure
    nothing there.
    """
    residuals = [
        # A token in the QUERY STRING — not in the userinfo field at all.
        "https://host/r.git?access_token=ghp_QUERYSECRET",
        # A token in the PATH, for the same reason.
        "https://host/ghp_PATHSECRET/r.git",
        # scp-style: no `//`, so the pattern's lookbehind never fires.
        "user:ghp_SCPSECRET@host:org/repo.git",
    ]
    for leaky in residuals:
        assert arrival.scrub(leaky) == leaky, leaky

    # THE IN-FIELD CLASS, every member of it, in both modes. Malformed per RFC
    # 3986 — such a character in userinfo must be percent-encoded — and noted
    # rather than handled.
    for char in _in_field_defeating_characters():
        leaky = f"https://bob:pa{char}ssSECRET@host/r.git"
        # The residual itself. It is the SECRET surviving, which it does in both
        # modes; what the rendering half does to the character that let it
        # survive is the separate question below.
        assert "SECRET" in arrival.scrub(leaky), repr(char)
        assert "SECRET" in arrival.scrub(leaky, keep_lines=True), repr(char)
        # What the rendering then does, stated as the rule it follows rather
        # than per member. This is a claim about THIS CLASS, and it catches a
        # change to the exemption only for a character the class contains, which
        # is why the exemption's own width is pinned over every codepoint by
        # `test_render_controls_exempts_exactly_printable_plus_the_named_lines`
        # and is not inferred from here.
        assert (arrival.scrub(leaky) == leaky) == char.isprintable(), repr(char)
        assert (arrival.scrub(leaky, keep_lines=True) == leaky) == (
            char.isprintable() or char in "\t\n"
        ), repr(char)

    # The exemption's value, beside the rule above because it names the culprit
    # in the failure message rather than a bare member.
    assert arrival.RENDERED_VERBATIM == "\t\n", arrival.RENDERED_VERBATIM

    assert arrival.scrub("https://bob:ghp_ORDINARY@host/r.git") == "https://***@host/r.git", (
        "the scrub does not mask at all — the out-of-field loop above measured nothing"
    )
    # The mirror image the header also claims, and it had no test either: the
    # `git@` exemption is CASE-SENSITIVE, so the capitalised form is over-masked.
    assert arrival.scrub("https://git@host/x") == "https://git@host/x"
    assert arrival.scrub("https://Git@host/x") == "https://***@host/x"
    # The terminator `(?:[/\\s]|$)` has THREE branches and the cases above drive
    # only the slash. Both others are everyday shapes and each was measured
    # leaving the whole file green when dropped: a remote with no path ends the
    # field at end-of-string, and one named mid-sentence in git's stderr — or at
    # the end of a line inside a `reflog -5` block — ends it on whitespace.
    assert arrival.scrub("ssh://git@host") == "ssh://git@host"
    assert (
        arrival.scrub("fatal: could not read from ssh://git@host does not exist")
        == "fatal: could not read from ssh://git@host does not exist"
    )

    # THE FIELD-LENGTH AXIS. Every token in this file is a short placeholder, so
    # nothing drove a userinfo longer than a couple of dozen characters, and a
    # BOUNDED match — `[^/\\s]{1,64}@`, the shape a ReDoS-hardening edit takes —
    # was measured leaving the whole file green while printing a real-length
    # credential verbatim. Driven across the sizes the header's own examples have
    # and past them.
    for length in (1, 23, 40, 93, 256, 4096):
        assert arrival.scrub(f"https://bob:{'S' * length}@h/r") == "https://***@h/r", length

    # THE LOOKBEHIND'S CONTEXT. Every URL above reaches `//` through a scheme's
    # `://`, so narrowing the lookbehind to `(?<=://)` was measured green. A
    # remote named in an unread `.git/config` need not carry a scheme, and git
    # echoes it on failure.
    assert arrival.scrub("fatal: 'x//bob:ghp_LOOKBEHIND@h/r'") == "fatal: 'x//***@h/r'"
    # The module's own worked example of that width, stated in `scrub`'s docstring
    # and copied into the `TESTING_STRATEGY.md` row, and measured by neither.
    assert arrival.scrub("/tmp/a//b@c/work") == "/tmp/a//***@c/work"

    # THE EMPTY-FIELD BOUNDARY, on both quantifiers. Nothing drove a `git@` with
    # nothing after it or a userinfo with nothing before the `@`, so `*`->`+` in
    # the lookahead and `+`->`*` in the run each left the whole file green. Both
    # fail SAFE — they mask more, never less — which is why they belong beside
    # the `Git@` over-masking mirror rather than among the leaks.
    assert arrival.scrub("ssh://git@") == "ssh://git@"
    assert arrival.scrub("https://@host/x") == "https://@host/x"
    # And the LONG end of the same field, driven across the axis for the same
    # reason the userinfo run is: bounding the lookahead's own run survives every
    # assertion above while each `git@` host here is short. A bound above the
    # longest length driven would still survive — no finite fixture closes that —
    # so the largest is far past any host a person would write.
    for length in (1, 70, 256, 4096):
        exempt = f"https://git@{'h' * length}/x"
        assert arrival.scrub(exempt) == exempt, length

    # EVERY occurrence in a block, not a count of them. `status --porcelain` and
    # `reflog -5` are each scrubbed as ONE multi-line string, so a substitution
    # capped at a fixed count masks the leading credentials and prints the rest.
    # THE OCCURRENCE-COUNT AXIS, driven rather than sampled: when this drove two,
    # `count=1` died while `count=2` and `count=3` each left the whole file green,
    # leaking the later tokens out of a reflog block; at eight, `count=9` did the
    # same. A cap above the number driven here would still survive, which is why
    # the number is large rather than tidy — `status --porcelain` and the
    # validator's streams have no length bound at all.
    # The expected text is BUILT from the same range as the input, so the two
    # cannot drift.
    leaky_block = [f"L{index} https://u:S{index}@h/r" for index in range(64)]
    scrubbed_block = [f"L{index} https://***@h/r" for index in range(64)]
    assert arrival.scrub("\n".join(leaky_block), keep_lines=True) == "\n".join(scrubbed_block)
    # And more than one on a SINGLE line, so a per-line cap is driven as well.
    assert arrival.scrub("https://u:S1@h/r https://u:S2@h/r") == "https://***@h/r https://***@h/r"
    # The helper restates the pattern's negated set instead of reading it, so on
    # its own it tracks a widening of `\\s` and not a widening of the pattern.
    # This closes that: the class is MEASURED off the module and the helper has
    # to reproduce it, so widening the pattern's exclusion set by any character
    # turns this red rather than adding a residual that nothing drives (measured
    # — a `;` added to that set left all 95 green before this existed).
    assert {
        chr(codepoint)
        for codepoint in range(sys.maxunicode + 1)
        if "SECRET" in arrival.scrub(f"https://bob:pa{chr(codepoint)}ssSECRET@host/r.git")
    } == set(_in_field_defeating_characters())


def test_a_chart_over_the_size_bound_is_refused_rather_than_read(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The read grows with the file, and `MemoryError` was not what the `except` caught.

    Measured on the read as it stood: a 512 MiB file at the chart path was read to
    completion — 536870912 characters — at 1.01 GiB peak RSS. Nothing fails; it just
    scales, until a constrained machine gets a `MemoryError`, which is neither an
    `OSError` nor a `ValueError` and so printed the raw traceback the guarded read
    exists to prevent. `MemoryError` now joins that `except` — see
    `test_a_memory_error_from_the_parse_fails_the_run_instead_of_raising`, and see
    `MAX_CHART_BYTES` for why catching it is what this bound could honestly buy: the
    bound is on the BYTES READ, and the parse is a separate shape-dependent
    amplification it does not reach.

    `truncate` asks for a SPARSE file, so staging this costs neither time nor space
    where the filesystem provides one — measured on this machine's APFS, `st_blocks`
    is 0 for the 64 MiB fixture. Nothing here ASSERTS that: an earlier version of this
    test did, and it was an assertion about the RUNNER's filesystem rather than about
    the code, in a file CI collects offline. The fixture is all NUL bytes, which decode
    as UTF-8 and then fail `json.loads` with `Expecting value` — so the two outcomes
    are distinguishable in the message, and asserting the bound's message while
    asserting the parser's is absent is what proves the read did not happen.
    """
    chart = synthetic_repo / "open_stories.json"
    with chart.open("wb") as handle:
        handle.truncate(arrival.MAX_CHART_BYTES + 1)
    assert chart.stat().st_size == arrival.MAX_CHART_BYTES + 1

    assert main([]) == 1

    out = capsys.readouterr().out
    assert f"is over the {arrival.MAX_CHART_BYTES}-byte bound" in out
    assert "refusing to read it" in out
    assert "Expecting value" not in out, "the file was read anyway — the bound did not fire"
    assert "Traceback" not in out
    # The LITERAL, once, for the same reason `SUBPROCESS_TIMEOUT_SECONDS` gets one:
    # every other reference here interpolates the constant, so a drift to a value
    # that bounds nothing would survive the file. The measured chart was 923179 bytes
    # on 2026-09-06, so 64 MiB is 72.7x it; the bound's own comment carries the
    # derivation and the accretion rate that makes 4 MiB — this file's previous
    # value, and the one whose comment claimed "~40x" — a bound the real chart
    # reaches in ~39 days.
    assert arrival.MAX_CHART_BYTES == 64 * 1024 * 1024


def test_a_chart_exactly_on_the_size_bound_is_still_read(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The comparison is `>`, not `>=`, and an off-by-one there rejects a real chart.

    Staged with `truncate` at exactly the bound, like the over-bound test one above —
    64 MiB of JSON whitespace would be a real 64 MiB written into `tmp_path` on every
    CI run, and the bound is now large enough for that to matter. Unlike that test this
    one does READ the 64 MiB, which is the point; measured, that read and the parse
    that rejects it cost 0.008 s per run (0.014 s on the first, cold), because the NUL
    bytes take the ASCII fast path and `json.loads` fails on the first character.
    NUL decodes as UTF-8 and then fails `json.loads` with `Expecting value`, so the
    parser's own message is what proves the read HAPPENED: under a `>=` bound this
    file is refused unread and `Expecting value` never appears, while `is over the`
    does. Asserting both directions is what separates the two outcomes.
    """
    chart = synthetic_repo / "open_stories.json"
    with chart.open("wb") as handle:
        handle.truncate(arrival.MAX_CHART_BYTES)
    assert chart.stat().st_size == arrival.MAX_CHART_BYTES

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "is over the" not in out
    assert "FAIL: chart round-trip" in out
    assert "Expecting value" in out, "the bound fired on a chart exactly ON it"
    assert "Traceback" not in out


def test_a_prefix_differing_only_in_case_follows_this_filesystem(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.VENV` is the repo venv where the filesystem says it is, and not where it isn't.

    `resolve()` follows symlinks but preserves the case it was given, so on this
    project's own macOS APFS checkout `<root>/.VENV` and `<root>/.venv` are one
    directory with one inode that a string comparison called different — and the
    refusal silently passed, running against the editable install it exists to
    block. On a case-sensitive filesystem they really are two directories and
    refusing would be a false positive.

    So this test PROBES the filesystem it is running on rather than assuming
    either, and asserts the opposite outcome in each case. On a case-sensitive
    filesystem it is the `samefile` arm's negative control; the positive is
    measurable only where the platform provides it.

    ⚠️ IT ALSO PINS THE REFUSAL'S WORDING, which the same fact breaks a second way.
    The message chose between "IS the repo venv" and "is inside the repo venv" on a
    raw `prefix == refused`, so on this filesystem `<root>/.VENV` — one directory
    with one inode, as the paragraph above says the file's own `same_directory`
    measured — was reported as being INSIDE the directory it is. `_run` now asks
    `same_directory` for that choice too. Like the refusal itself, that arm is
    reachable only where the platform provides it: after `resolve()` the only way two
    different path strings still name one inode is case folding, so on a
    case-sensitive filesystem reverting to `==` kills nothing here and this test says
    so rather than implying otherwise.
    """
    (synthetic_repo / ".venv").mkdir()
    upper = synthetic_repo / ".VENV"
    one_directory = upper.exists()
    if not one_directory:
        upper.mkdir()
    monkeypatch.setattr(sys, "prefix", str(upper))

    assert main(["--skip-chart"]) == (1 if one_directory else 0)

    out = capsys.readouterr().out
    assert ("REFUSING: sys.prefix" in out) is one_directory
    if one_directory:
        assert (
            f"REFUSING: sys.prefix IS the repo venv {(synthetic_repo / '.venv').resolve()}." in out
        )
        assert "is inside the repo venv" not in out


def test_a_chart_that_is_not_utf8_fails_the_run_instead_of_raising(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chart read was the one unguarded operation left in the file.

    Measured against the earlier code, which read outside the `try`: this exited on
    a raw `UnicodeDecodeError` traceback rather than on a diagnosed failure. The
    chart is caller-influenced under `--repo` like everything else here, and
    `UnicodeDecodeError` is a `ValueError`, which is why the widened `except` sees
    it.
    """
    (synthetic_repo / "open_stories.json").write_bytes(b'{"stories": []}\n\xff')

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: chart round-trip" in out
    assert "utf-8" in out
    assert "Traceback" not in out


def test_a_directory_at_the_chart_path_is_not_a_chart(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`exists()` says yes to a directory, and `read_text` then raises IsADirectoryError.

    Measured against the earlier code: a raw traceback. `is_file()` is the guard —
    it is false for a directory, so this reads as the absent chart it effectively is.
    """
    (synthetic_repo / "open_stories.json").mkdir()

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "no regular file at" in out
    assert "chart round-trip OK" not in out
    assert "Traceback" not in out


def test_a_fifo_at_the_chart_path_does_not_block_the_run(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hang, which is the one failure a bounded script must not have.

    `exists()` is true for a FIFO and `read_text` then blocks for a writer that
    never comes: measured against the earlier code, still running past 8s with no
    timeout anywhere on that path. `is_file()` is false for a FIFO, so the read is
    never reached.

    Run on a thread with a join deadline because the failure being pinned is
    non-termination, which an ordinary call cannot report: without the deadline
    this test would hang the whole suite instead of failing it. The thread is a
    daemon so a hung one cannot hold the interpreter open either.
    """
    os.mkfifo(synthetic_repo / "open_stories.json")
    finished: list[int] = []
    worker = threading.Thread(target=lambda: finished.append(main([])), daemon=True)

    worker.start()
    worker.join(timeout=20)

    assert not worker.is_alive(), "the chart read blocked on a FIFO — the run is unbounded"
    assert finished == [0]
    assert "no regular file at" in capsys.readouterr().out


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a 0o000 file, so an unreadable chart cannot be staged"
)
def test_an_unreadable_chart_fails_the_run_instead_of_raising(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `OSError` half of the widened `except`, which `ValueError` does not cover.

    `is_file()` answers for the directory entry, not for whether this process may
    read it, so a permission failure — or the read losing a race with a chart being
    rewritten under it, which is the shape that cannot be staged — still reaches the
    read. Without `OSError` in the `except` that is a `PermissionError` traceback.
    """
    chart = synthetic_repo / "open_stories.json"
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    chart.chmod(0o000)

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: chart round-trip" in out
    assert "Traceback" not in out


def test_the_branch_line_is_scrubbed_too(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Git's output is scrubbed" is a rule, so it holds on every printer or on none.

    A branch name cannot realistically carry URL userinfo, so this is completeness
    against the module docstring's headline rather than a live leak — but a reader
    who has to check each `print` for themselves has no rule, only a list.
    """
    real_git = arrival.git

    def leaky_branch(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:3] == ("rev-parse", "--abbrev-ref", "HEAD"):
            return subprocess.CompletedProcess(
                list(args), 0, "https://bob:ghp_BRANCHSECRET@example.invalid/r.git\n", ""
            )
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", leaky_branch)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "ghp_BRANCHSECRET" not in out
    assert "branch: https://***@example.invalid/r.git" in out


def test_the_validators_own_output_is_scrubbed_too(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same rule, on the one printer whose text this script did not produce.

    The validator is handed one chart path and reports on it, so like the branch
    line this is completeness rather than a live leak. Both of its streams are
    routed, because scrubbing one and not the other is the split this story already
    shipped once for `fetch.stderr` and `reflog -5`.
    """
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")
    real_run = subprocess.run

    def leaky_validator(*args: object, **kwargs: object) -> object:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and any(arrival.VALIDATOR_FILENAME in str(p) for p in argv):
            return subprocess.CompletedProcess(
                argv,
                0,
                "audited https://bob:ghp_STDOUTSECRET@example.invalid/r.git\n",
                "note https://bob:ghp_STDERRSECRET@example.invalid/r.git\n",
            )
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", leaky_validator)

    assert main([]) == 0

    captured = capsys.readouterr()
    assert "ghp_STDOUTSECRET" not in captured.out
    assert "ghp_STDERRSECRET" not in captured.err
    assert "audited https://***@example.invalid/r.git" in captured.out
    assert "note https://***@example.invalid/r.git" in captured.err


def test_an_inherited_git_dir_does_not_redirect_the_repo_audit(
    synthetic_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A SILENT WRONG-TREE PASS, which is the one failure this script exists to prevent.

    `--repo` is an `os.chdir`, and git reads a repository's location from its
    ENVIRONMENT first — so `GIT_DIR` and `GIT_WORK_TREE` outrank it. Where they point
    at a valid sibling checkout, nothing fails: the fetch succeeds, both counts
    succeed, and the run exits 0 while every line describes a DIFFERENT tree.

    Measured against the earlier code, which passed `env={**os.environ, ...}`:
    `--repo <A> --skip-chart` printed B's toplevel and B's branch and exited 0, where
    the control with the two variables unset printed A's. `git()` now removes every
    name in `GIT_LOCATION_ENV_VARS` from the child's environment; there is no `-c` for
    this and no "ignore the environment" flag.

    An operator invoked from a git hook, `git bisect run` or `git rebase --exec` has
    these exported without having typed them.
    """
    elsewhere = tmp_path / "second"
    elsewhere.mkdir()
    second = _build_repo(elsewhere, monkeypatch)
    _git(second, "branch", "-m", "second-branch")
    assert second.resolve() != synthetic_repo.resolve()
    monkeypatch.setenv("GIT_DIR", str(second / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(second))

    assert main(["--repo", str(synthetic_repo), "--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {synthetic_repo.resolve()}\n" in out
    assert "branch: story-branch\n" in out
    # Named, not merely absent-by-implication: without the strip BOTH of these appear
    # and the run still exits 0, so the exit code cannot tell the two runs apart.
    assert str(second.resolve()) not in out
    assert "second-branch" not in out


def test_every_git_call_strips_the_location_environment_variables(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """All six names off EVERY call, the rest of the environment untouched.

    The end-to-end test above proves the pair that PASSES; this says the strip is on
    every call rather than on the two the pair happened to reach, and that it removes
    the names rather than blanking them — `GIT_DIR=""` is not the same as unset.

    Asserted against literal strings rather than against
    `arrival.GIT_LOCATION_ENV_VARS`, for the reason the config-override test gives:
    comparing the constant with itself passes whatever the constant happens to say.

    All six are planted at a path that does not exist, so the run also demonstrates
    the strip end to end — an unstripped `GIT_DIR=/nowhere/planted` is not a
    repository, and the run would fail at `rev-parse --show-toplevel` rather than
    exit 0.
    """
    planted = str(tmp_path_planted := synthetic_repo.parent / "nowhere-planted")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        monkeypatch.setenv(name, planted)
    monkeypatch.setenv("E23_S56_SENTINEL", "kept")
    assert not tmp_path_planted.exists()
    seen: list[dict[str, object]] = []
    real_run = subprocess.run

    def recording_run(*args: object, **kwargs: object) -> object:
        seen.append(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(arrival.subprocess, "run", recording_run)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {synthetic_repo.resolve()}\n" in out
    assert seen, "no subprocess ran"
    for kwargs in seen:
        env = kwargs["env"]
        assert isinstance(env, dict)
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            assert name not in env, (name, env.get(name))
        # The strip is surgical: everything else this process inherited is still
        # there, so a `env={"GIT_TERMINAL_PROMPT": "0"}` that dropped the whole
        # environment — which would take `PATH` and `HOME` with it — is not this.
        assert env["E23_S56_SENTINEL"] == "kept"
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_deeply_nested_chart_fails_the_run_instead_of_raising(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`RecursionError` is a `RuntimeError`, so neither widened `except` arm saw it.

    The chart is caller-influenced under `--repo`, and the guarded read's own argument
    — that `MemoryError` is neither an `OSError` nor a `ValueError`, so the handler
    would print the raw traceback it exists to prevent — applies unchanged to
    `json.loads` on deeply nested input. DEPTH is what reaches it, not size: measured
    against the two-exception `except`, this 120000-byte chart is ~0.13x the real one
    and far under `MAX_CHART_BYTES`, and it exited on a raw `RecursionError`
    traceback.

    Both assertions matter and they discriminate different things: the diagnosed FAIL
    line says the handler caught it, and `Traceback` says the interpreter did not
    print one over the top.
    """
    depth = 60_000
    _write_chart(synthetic_repo, "[" * depth + "]" * depth)
    chart = synthetic_repo / "open_stories.json"
    assert chart.stat().st_size < arrival.MAX_CHART_BYTES, "the size bound would fire instead"

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: chart round-trip" in out
    assert "maximum recursion depth exceeded" in out
    assert "Traceback" not in out


def test_the_no_chart_message_names_both_candidate_roots(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two-candidate form of that message was unasserted, so it was free to drop one.

    `test_chart_steps_are_skipped_when_the_chart_is_absent` asserts the DEDUPLICATED
    form, and it can only see that one: it runs in the main checkout, where the main
    root and the toplevel are the same directory and the two candidates collapse to a
    single path. Replacing the `" or ".join(...)` with `str(candidates[0])` killed no
    test in this file BEFORE this test existed — which is why it exists. ⚠️ The
    sentence here said that in the present tense, over the test it justifies, and so
    asserted its own vacuity: re-measured, that mutant now fails exactly this test
    and no other.

    From a linked worktree the two roots are genuinely different directories, and an
    operator who is told only one of them cannot tell "there is no chart anywhere"
    from "I looked in the wrong place" — which is the exact confusion E23-S27's own
    `parents[2]` default caused.
    """
    linked = synthetic_repo.parent / "linked"
    _git(synthetic_repo, "worktree", "add", "-b", "linked-branch", str(linked), "main")
    monkeypatch.chdir(linked)
    assert not (synthetic_repo / "open_stories.json").exists()
    assert not (linked / "open_stories.json").exists()

    assert main([]) == 0

    out = capsys.readouterr().out
    main_candidate = synthetic_repo.resolve() / "open_stories.json"
    worktree_candidate = linked.resolve() / "open_stories.json"
    assert main_candidate != worktree_candidate, "the two roots collapsed — this proves nothing"
    assert f"no regular file at {main_candidate} or {worktree_candidate} —" in out


def test_control_characters_in_a_commit_subject_are_rendered_not_printed(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reflog is the one printer carrying free text an author chose.

    This script's whole product is a report a human reads to decide whether the tree
    is right, and `reflog -5` prints commit subjects verbatim. Measured on git 2.53.0
    against a scrub that only masked URL userinfo: a subject of `benign` + ESC `[2K` +
    ESC `[1G` + `clean tree, nothing to see` reached the printed line byte for byte
    (checked with `od -c`), and on a terminal `ESC[2K` erases the line and `ESC[1G`
    returns the cursor to column 1 — so what the operator SEES is the attacker's
    sentence in place of the tool's.

    Driven through a real commit rather than a stub, because the fact being pinned is
    that git carries it: the other two printers of GIT'S text cannot, and this test
    asserts that too. ⚠️ It does not follow that the reflog is the only reachable
    printer, and this docstring's earlier framing ("the reflog is the reachable one")
    reasoned over a list of three that left one out — the `toplevel:` line prints the
    checkout's own directory name, which is chosen by whoever made the directory. That
    is `test_the_toplevel_line_renders_control_characters_in_the_checkout_path`. Tab
    and newline must survive — they are what the report is MADE of, and a class that
    swallowed them would pass every assertion above while collapsing the whole report
    onto one line.
    """
    subject = "benign\x1b[2K\x1b[1Gclean tree, nothing to see"
    _commit(synthetic_repo, "escaped.txt", "x\n", subject)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert "benign\\x1b[2K\\x1b[1Gclean tree, nothing to see" in out
    # The two exemptions, asserted on the function so a widened class cannot hide
    # behind the report's own structure — and asserted in BOTH directions, because
    # the exemption is now opt-in. `keep_lines=True` is what the four printers made
    # of lines pass; the DEFAULT renders tab and newline, which is what keeps a path
    # or an argv value from forging a line
    # (`test_a_newline_in_the_checkout_path_cannot_forge_a_report_line`). Measured on
    # this tree: flipping the default back to exempting them fails that test and
    # this line, among others.
    assert arrival.scrub("a\nb\tc", keep_lines=True) == "a\nb\tc"
    assert arrival.scrub("a\nb\tc") == "a\\nb\\tc"
    # The other two printers of author-chosen text, measured on this git rather
    # than assumed. ⚠️ AND THE REF HALF IS NARROWER THAN IT READS: what git
    # refuses below is an ASCII CONTROL character, not the class this file's rule
    # is about, which is PRINTABILITY. Measured on git 2.53.0,
    # `git check-ref-format 'refs/heads/rel<U+202E>ase'` exits 0 and `git branch`
    # creates it — and `'rel\u202ease'.isprintable()` is False — so a ref name
    # holds exactly the character the branch line renders, and the branch line's
    # rendering is load-bearing rather than belt-and-braces. `status --porcelain`
    # quotes a path holding either.
    refused = subprocess.run(
        ["git", "branch", "esc\x1b[2Kbranch"],
        cwd=synthetic_repo,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0, refused.stderr
    assert "not a valid branch name" in refused.stderr
    (synthetic_repo / "esc\x1b[2Kfile").write_text("x\n", encoding="utf-8")
    quoted = _git(synthetic_repo, "status", "--porcelain")
    assert "\x1b" not in quoted, quoted
    assert r'"esc\033[2Kfile"' in quoted, quoted


def test_a_unicode_direction_control_in_a_commit_subject_is_rendered_too(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A range list is only ever as wide as the ranges somebody thought of.

    The test above pins ESC, which the file's first spelling — the character class
    `[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f]` — did catch. This one pins the half that
    class did not: U+202E RIGHT-TO-LEFT OVERRIDE reverses the rendering of everything
    after it on any bidi-aware terminal, so `release fix<RLO>txt.exe<PDF> ready` is
    read as a subject about `exe.txt`. Measured against that class, `scrub` returned
    it byte for byte and the run exited 0. `str.isprintable()` is False for it, and
    for U+202D, U+202C, U+2066, U+200B, U+200D, U+00A0 and U+2028, all measured
    surviving the same way — so the fix is the sibling validator's printability test
    rather than eight more ranges.

    The third assertion is the one that stops the fix over-reaching: ordinary
    non-ASCII is printable and must arrive intact, so a mutant that escaped everything
    outside ASCII would satisfy the first two and mangle every accented author name in
    the reflog.
    """
    subject = "release fix‮txt.exe‬ ready — café"
    _commit(synthetic_repo, "rtl.txt", "x\n", subject)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "‮" not in out, "a raw right-to-left override reached the report"
    assert "release fix\\u202etxt.exe\\u202c ready" in out
    assert "café" in out, "printable non-ASCII must survive — this is not an ASCII filter"


def test_the_toplevel_line_renders_control_characters_in_the_checkout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `toplevel:` line prints a string somebody else chose, and it was unrendered.

    The module docstring used to reason about author-chosen text over a list of three
    printers — the reflog, the branch line and `status --porcelain` — and left this one
    out of the list. It is the same shape as the reflog case and worse placed: it is
    the exact line a reader uses to decide WHICH TREE they are in. Measured on the
    earlier code, a checkout directory named `wo` ESC `[2K` ESC `[1G` `clean tree,
    nothing to see` printed that line raw and the run exited 0.

    ⚠️ THE SYMLINK IS THE POINT OF THE SECOND HALF, not decoration. An operator who
    types the hostile name has at least seen it; `resolve()` means one who types a
    benign symlink has not, and gets the same line. Both are asserted, because the
    line is built from `Path(...).resolve()` and only the symlink run proves the
    rendering survives that resolve.

    `--skip-chart` throughout: the chart is irrelevant here and `tmp_path` has none.
    """
    hostile = tmp_path / "wo\x1b[2K\x1b[1Gclean tree, nothing to see"
    hostile.mkdir()
    repo = _build_repo(hostile, monkeypatch)
    rendered = f"toplevel: {str(repo.resolve()).replace(chr(27), chr(92) + 'x1b')}"
    # Without this the test is vacuous, because `rendered` is DERIVED from the
    # fixture's own path: give the checkout a benign name and `rendered` becomes
    # exactly the line an UNRENDERED print produces, so both halves below pass while
    # measuring nothing. Measured — neutralising the fixture to a plain directory name
    # turns this line red and nothing else in the test.
    assert "\\x1b" in rendered, "the checkout path holds no escape — this proves nothing"

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert rendered in out

    benign = tmp_path / "arrive"
    benign.symlink_to(repo, target_is_directory=True)
    monkeypatch.chdir(benign)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report through the symlink"
    assert rendered in out


def test_a_multi_line_git_stderr_is_marked_on_exactly_one_line(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The marker's contract says "on one line", and only a one-line stderr tested it.

    Every stderr this file previously fed the marker was the single-line message
    `git()` synthesises for a timeout, so the contract was asserted against the one
    input that cannot falsify it — in two places, `git()`'s docstring and
    `unavailable_marker`'s. Real git stderr is routinely several lines: measured on
    git 2.53.0, in a checkout whose `HEAD` commit is a corrupt loose object, both
    `git status --porcelain` and `git reflog -5` exit 128 with SEVEN lines, three
    `error: inflate:` / `error: unable to unpack` pairs and then the `fatal:`. That
    exact stderr is replayed here.

    Unjoined it rendered as seven lines with the marker's closing `)` stranded on the
    last, in the middle of a report whose next line is a label — so an operator reading
    for the label saw six lines that look like output from the command that did not
    run. Both assertions are needed and they discriminate different things: the first
    that the marker occupies one line, the second that no part of git's text was
    dropped to achieve it.
    """
    sha = "560e1a0354123b949a6459255b42384a80bab60a"
    seven_lines = (
        "error: inflate: data stream error (incorrect header check)\n"
        f"error: unable to unpack {sha} header\n"
        "error: inflate: data stream error (incorrect header check)\n"
        f"error: unable to unpack {sha} header\n"
        "error: inflate: data stream error (incorrect header check)\n"
        f"error: unable to unpack {sha} header\n"
        f"fatal: loose object {sha} (stored in .git/objects/56/{sha[2:]}) is corrupt\n"
    )
    assert len(seven_lines.strip().splitlines()) == 7
    real_git = arrival.git

    def corrupt_object(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("status", "--porcelain"):
            return subprocess.CompletedProcess(list(args), 128, "", seven_lines)
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", corrupt_object)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    marked = [line for line in out.splitlines() if line.startswith("(unavailable:")]
    assert len(marked) == 1, out
    assert marked[0].endswith(")"), marked[0]
    for fragment in seven_lines.split():
        assert fragment in marked[0], (fragment, marked[0])


def test_the_argv_values_are_echoed_as_typed_and_not_scrubbed(
    tmp_path: Path, synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--repo`` and ``--expect`` are echoed EXACTLY as typed, and that is why
    argv does not go through `scrub`.

    The rule that paths and argv take `render_controls` alone was justified on the
    `toplevel:` line, and measured there it makes no difference: that line prints a
    `Path.resolve()`-ed value and `resolve()` collapses a doubled separator, so the
    `//` that `URL_USERINFO`'s lookbehind needs can never reach it. Swapping `scrub`
    in at `toplevel:`, `chart:` and both `REFUSING:` lines was measured changing
    nothing at all.

    Where it does bite is the argv this line echoes, which is never resolved. A
    directory really can be named `a//b@c` — POSIX forbids only `/` in a component,
    and a doubled separator survives in a string the operator typed — and through
    `scrub` the `FAIL:` line would report a directory the operator did not name, in
    the one line that is supposed to tell them which value was refused.

    ⚠️ THE ASSERTIONS READ THE ``FAIL:`` LINE, NOT THE WHOLE REPORT, and reading the
    whole report is what a first draft of this test did. The same line goes on to
    print `render_controls(str(exc))`, and `OSError`'s message repeats the path — so
    "the value as typed is somewhere in the output" was satisfied by the exception
    text and passed under the break, measuring nothing. Only the echoed half is the
    subject here.

    ⚠️ BOTH ARGV VALUES, because the docs row said both were pinned and only one
    was: swapping `scrub` in at `--expect`'s renderer left the whole file green.
    They are echoed by different lines and each needs its own break.

    Named breaks that kill this test, one per value:

    * **scrub the `--repo` value** —
      `f"FAIL: --repo {scrub(args.repo)}: cannot enter that directory — ..."`.
    * **scrub the `--expect` value** — `expect = scrub(args.expect)` in `main`.

    Observed under the FIRST, each assertion evaluated on its own rather than
    inferred: TWO of the eight below turn red — the `--repo` value as typed is
    absent from the echoed half, and the masked `***@` form is present in it. The
    second break turns the two `--expect` assertions red the same way. The other
    four do not discriminate: both runs exit 1 either way and both `FAIL:` lines
    print either way.

    ⚠️ FIXTURE NEUTRALISED, measured: removing the doubled slash from the directory
    name leaves this test GREEN under the break as well. Without a `//` the two
    renderers agree on the value and there is nothing to tell apart — which is
    exactly the state the old justification was measured in, and why it could
    conclude that the distinction did not matter. The doubled slash is the whole
    mechanism.
    """
    typed = f"{tmp_path}/a//b@c/absent"

    assert main(["--repo", typed, "--skip-chart"]) == 1

    out = capsys.readouterr().out
    line = next((one for one in out.splitlines() if one.startswith("FAIL: --repo ")), "")
    echoed = line.partition(": cannot enter that directory")[0]
    assert line, out
    assert typed in echoed, echoed
    assert "***@" not in echoed, echoed

    # `--expect` is the other unresolved argv value, echoed by its own line.
    revision = "a//b@c-not-a-revision"

    assert main(["--expect", revision, "--skip-chart"]) == 1

    out = capsys.readouterr().out
    expect_line = next((one for one in out.splitlines() if one.startswith("FAIL: --expect ")), "")
    expect_echo = expect_line.partition(": `git cat-file -e`")[0]
    assert expect_line, out
    assert revision in expect_echo, expect_echo
    assert "***@" not in expect_echo, expect_echo


def test_the_marker_collapses_the_whitespace_the_rendering_leaves(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the marker's ``" ".join(...split())`` still buys, now that the
    rendering does the rest.

    The join was added when `keep_lines` defaulted to True and unjoined stderr
    really did print as several lines. The commit that made `keep_lines` opt-in
    made `scrub` escape a newline here and silently falsified that justification:
    measured BEFORE THIS TEST EXISTED, removing the join left the marker ONE line
    and the whole file green — which is what left the join unpinned, and is why
    the property everyone credited it with turned out to be delivered by the
    rendering as well. On the shipped tree that same mutant fails this test, so
    the sentence is DATED rather than left in a present tense its own named break
    contradicts four lines below.

    What it still does is the other half of `split()`: it collapses every run of
    whitespace. The stderr below is padded with tabs and runs of spaces, which
    `render_controls` keeps (a TAB is escaped, a space is left alone), and the
    marker is a sentence an operator reads rather than a column.

    Named break that kills this test: **drop the join** — `one_line =
    done.stderr` in `unavailable_marker`. Observed, each assertion evaluated on
    its own under the break rather than inferred: TWO of the five below turn red —
    a run of spaces survives into the marker, and so does an escaped tab. The
    other two do not discriminate: the marker is still one line and every word git
    said is still in it, both of which the rendering delivers on its own.

    ⚠️ FIXTURE NEUTRALISED, measured: replacing the padding with single spaces
    leaves this test GREEN under the break as well — there is then nothing for the
    join to collapse, so the rendering alone produces the same marker. That is the
    state the seven-line fixture next door is in, which is why it could not see
    this and why the join went unpinned for two commits.
    """
    padded = "error:  could not\tread    from remote\nfatal:\t\tunable  to fetch\n"
    real_git = arrival.git

    def padded_status(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("status", "--porcelain"):
            return subprocess.CompletedProcess(list(args), 128, "", padded)
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", padded_status)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    marked = [line for line in out.splitlines() if line.startswith("(unavailable:")]
    assert len(marked) == 1, out
    assert "  " not in marked[0], marked[0]
    assert "\\t" not in marked[0], marked[0]
    for word in padded.split():
        assert word in marked[0], (word, marked[0])


def test_the_unavailable_marker_scrubs_the_stderr_it_prints(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The marker is the report's ONLY printer of git's stderr that no test pinned.

    Measured on the shipped tree before this test: replacing `scrub(one_line)`
    with `one_line` in `unavailable_marker` left the whole file green. Nothing in
    the suite fed the marker a stderr that had anything to scrub — every earlier
    one was the timeout message `git()` synthesises, which holds no URL and no
    control character — so the file's headline rule, that every string in this
    report the script did not itself write is rendered, held on this printer by
    accident.

    Both halves of `scrub` are exercised in one stderr, because the marker calls
    it once and a test that pins one half leaves the other unpinned in exactly
    the way this test exists to correct:

    * a `scheme://` remote whose userinfo carries a token, which must come back
      as `***@`;
    * an ESC erase-and-rewrite pair, which must come back rendered — in a marker
      that a terminal would otherwise let git's text ERASE, which is worse here
      than in the lines below it: the marker's whole content is the reason the
      label beside it is empty.

    `git status --porcelain` is the call stubbed because its marker prints on the
    `git status --porcelain:` label, and the run still exits 0 — the three informational
    calls do not decide the exit code, so the forgery this pins is one a reader
    sees on a run that reports success.

    Named break that kills this test: **print the stderr unscrubbed** —
    `return f"(unavailable: `git` exited {done.returncode} — {one_line})\n"`.
    Observed, each assertion evaluated on its own under the break rather than
    inferred: THREE of the five below turn red — the token is in the output, a
    raw ESC is in the output, and the masked `***@` form is absent. The other two
    do not discriminate: the run still exits 0, and the marker is still one line,
    which is the property the neighbouring seven-line test already pins.

    ⚠️ FIXTURE NEUTRALISED, measured: replacing the token and the ESC in the
    stubbed stderr with ordinary text turns this test RED. With nothing to scrub
    there is nothing for the assertions to find, which is precisely the state the
    suite was in before this test.
    """
    token = "ghp_" + "A" * 12
    stderr = (
        f"fatal: could not read from https://bob:{token}@example.invalid/r.git\n"
        "\x1b[2K\x1b[1G clean tree, nothing to see\n"
    )
    real_git = arrival.git

    def failing_status(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("status", "--porcelain"):
            return subprocess.CompletedProcess(list(args), 128, "", stderr)
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", failing_status)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    marked = [line for line in out.splitlines() if line.startswith("(unavailable:")]
    assert len(marked) == 1, out
    assert token not in out, out
    assert "\x1b" not in out, out
    assert "***@example.invalid/r.git" in marked[0], marked[0]


def test_a_memory_error_from_the_parse_fails_the_run_instead_of_raising(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`MemoryError` is named in the guarded read's `except`, and it is none of the others.

    `MAX_CHART_BYTES` bounds the BYTES READ — measured, `read_text` on a 64 MiB file
    grows RSS by 128.0 MiB, 2.00x the file — and the PARSE is a separate amplification
    that depends on the file's shape rather than its size: measured at 64 MiB, between
    3.6x for repeated `null` and 26.8x for a flat array of empty arrays. So a chart
    UNDER the bound can still exhaust memory, and the two prescribed ways of bounding
    the parse were both measured and declined at `MAX_CHART_BYTES`. What is left is
    that the failure be DIAGNOSED rather than raw, which is the whole reason the read
    is inside a `try`.

    The first assertion is the exception hierarchy, pinned rather than described: the
    module says `MemoryError` is none of `OSError`, `ValueError` or `RecursionError`,
    and its MRO is `Exception` and nothing else, so the `except` sees it only because
    it is named.
    This test does not claim a real out-of-memory run is survivable — nothing here
    could measure that. It claims the handler names it.
    """
    assert not issubclass(MemoryError, (OSError, ValueError, RuntimeError))
    _write_chart(synthetic_repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")

    class ExhaustedJson:
        dumps = staticmethod(json.dumps)

        @staticmethod
        def loads(_raw: str) -> object:
            raise MemoryError("cannot allocate the parsed chart")

    monkeypatch.setattr(arrival, "json", ExhaustedJson)

    assert main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL: chart round-trip" in out
    assert "cannot allocate the parsed chart" in out
    assert "Traceback" not in out


HOSTILE_DIRNAME = "wo\x1b[2K\x1b[1Gclean tree, nothing to see"


def _hostile_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic repo whose own CHECKOUT PATH carries an ESC erase-and-rewrite pair.

    `test_the_toplevel_line_renders_control_characters_in_the_checkout_path` builds
    this by hand for the one line it pins; the tests below need the same shape for
    the printers it left out, so it is a helper rather than another copy.
    """
    hostile = tmp_path / HOSTILE_DIRNAME
    hostile.mkdir()
    return _build_repo(hostile, monkeypatch)


def _escaped(text: str) -> str:
    """What a rendered `text` must look like, spelled WITHOUT calling the script.

    Deriving the expectation from `arrival.render_controls` would move both sides of
    every assertion below together, so a mutant that returned its argument unchanged
    would still be green.
    """
    return text.replace("\x1b", "\\x1b")


def test_the_chart_printers_render_the_checkout_path_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fix above closed ONE printer of the checkout path. There were five more.

    `test_the_toplevel_line_renders_control_characters_in_the_checkout_path` renders
    the `toplevel:` line, and the paragraph that justified it diagnosed its
    predecessor for "reasoning over a list that left one out" — and then reasoned
    over a new list that left five out. Three of those five printers are here — the
    `chart:` line and both `FAIL: chart round-trip` lines, which all print a path
    built from the same `resolve()`d root; the other two are the `REFUSING:` lines,
    and they are the test below. Five is a measurement of the version this commit
    replaces, not a property of the file: what holds going forward is the rule, not
    the number.

    ⚠️ THE SUITE COULD NOT SEE THEM BECAUSE IT PASSED `--skip-chart` EVERYWHERE THE
    PATH WAS HOSTILE. That flag skips the sibling printer two lines below the one
    being asserted. Every run here is on the DEFAULT path, which is also where the
    worst of the three lives: measured on the earlier code, the `chart:` line printed
    the erase-and-rewrite pair raw, as the report's LAST line, on a run that EXITED
    0 — so the operator's terminal ended on the attacker's sentence and nothing said
    anything was wrong. The two `FAIL:` lines at least come with an exit 1.

    The rule this holds is the module header's: every string in the report that this
    script did not write is rendered before it is printed. It is stated as a rule
    because the two sentences before it were counts, and each was falsified by the
    commit that wrote it.
    """
    repo = _hostile_checkout(tmp_path, monkeypatch)
    rendered = _escaped(str(repo.resolve() / "open_stories.json"))
    # Without this the test is vacuous: `rendered` is DERIVED from the fixture's own
    # path, so a benign checkout name makes it exactly the line an UNRENDERED print
    # produces and every assertion below passes while measuring nothing. Measured —
    # neutralising `HOSTILE_DIRNAME` to a plain name turns this line red and nothing
    # else in this test.
    assert "\\x1b" in rendered, "the checkout path holds no escape — this proves nothing"

    # (a) No chart: the DEFAULT path's LAST line, on a run that exits 0.
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert out.splitlines()[-1] == (
        f"chart: no regular file at {rendered} — round-trip and S27 validator skipped."
    )

    # (b) A chart that parses and does not round-trip.
    _write_chart(repo, '{"stories": []}')
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"FAIL: chart round-trip — {rendered} is not " in out

    # (c) A chart that does not parse at all — the arm that also prints the exception.
    _write_chart(repo, "not json")
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"FAIL: chart round-trip — {rendered} could not be read: " in out


def test_the_refusal_lines_render_the_venv_paths_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other two printers of a path, and the ONLY output a refused operator gets.

    A refusal exits 1 and prints two lines and nothing else — no toplevel, no branch,
    no counts. So an erase-and-rewrite here is not one lie among a report of true
    lines; it IS the report. Both wordings are covered because they are separate
    printers with separate interpolations: the `IS` form names one path, the
    `is inside` form names two, and rendering one and not the other would leave the
    second free.

    `--skip-chart` is honest here, unlike in the test above: the refusal returns
    before the chart is ever looked for, so the flag changes nothing on this path.
    """
    repo = _hostile_checkout(tmp_path, monkeypatch)
    venv = (repo.resolve() / ".venv").resolve()
    rendered_venv = _escaped(str(venv))
    assert "\\x1b" in rendered_venv, "the checkout path holds no escape — this proves nothing"

    monkeypatch.setattr(sys, "prefix", str(venv))

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"REFUSING: sys.prefix IS the repo venv {rendered_venv}." in out

    nested = venv / "envs" / "child"
    rendered_nested = _escaped(str(nested.resolve()))
    monkeypatch.setattr(sys, "prefix", str(nested))

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"REFUSING: sys.prefix {rendered_nested} is inside the repo venv {rendered_venv}." in out


def test_the_exception_texts_the_str_exc_renderers_print_hold_no_tab_or_newline(
    tmp_path: Path,
) -> None:
    """The record behind the ``render_controls(str(exc))`` sites in the chart
    read's ``except`` clause and in the ``--repo`` ``os.chdir`` failure.

    ``keep_lines=True`` spares tab and newline and nothing else, so it can change
    what those sites print only through an exception text holding one. Each
    exception below is raised by the call the script makes, on a path or input
    that holds both. The texts escape them: ``OSError`` prints its filename with
    ``repr``, and the decoders print positions, not the input. The size
    ``ValueError`` is left out because the script composes its text from numbers.
    These are samples of the exception paths, not a proof about every message.

    No production break kills this test. It turns red if an exception text
    starts to carry a raw tab or newline, and then those sites are no longer
    equivalent under ``keep_lines`` and need a test of their own.
    """
    hostile = tmp_path / "tab\there\nnewline"
    hostile.mkdir()
    (hostile / "a-directory").mkdir()
    (hostile / "not-utf8.json").write_bytes(b"\xff\n\t")
    texts: dict[str, str] = {}
    calls = {
        "stat of a missing chart": lambda: (hostile / "open_stories.json").stat(),
        "read_text of a directory": lambda: (hostile / "a-directory").read_text(encoding="utf-8"),
        "read_text of bytes that are not UTF-8": lambda: (hostile / "not-utf8.json").read_text(
            encoding="utf-8"
        ),
        "json.loads of a broken document": lambda: json.loads("{\n\t\n"),
        "json.loads nested past the recursion limit": lambda: json.loads("[" * 200_000),
        "os.chdir into a missing directory": lambda: os.chdir(hostile / "missing"),
    }
    for label, call in calls.items():
        with pytest.raises((OSError, ValueError, RecursionError)) as excinfo:
            call()
        texts[label] = str(excinfo.value)

    for label, text in texts.items():
        assert text, label
        assert "\t" not in text and "\n" not in text, (label, text)
    # Without this the test proves nothing: the path really reached the OSError texts.
    assert "\\t" in texts["stat of a missing chart"], texts
    assert "\\n" in texts["os.chdir into a missing directory"], texts


def _lf_escaped(text: str) -> str:
    """``text`` with its tabs and newlines spelled as escapes, written out here
    and not by calling the script, for the same reason as ``_escaped``."""
    return text.replace("\t", "\\t").replace("\n", "\\n")


def _lf_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic repo whose checkout path holds a newline."""
    base = tmp_path / "lf\ncheckout"
    base.mkdir()
    repo = _build_repo(base, monkeypatch)
    assert "\n" in str(repo.resolve()), "the checkout path holds no newline — this proves nothing"
    return repo


def test_the_chart_printers_keep_a_newline_path_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chart printers that name the chart's path print it on one line
    when the checkout path holds a newline. They pass no ``keep_lines``, and the
    suite stayed green when each was given ``keep_lines=True`` (E23-S61 step 2),
    because every newline checkout above ran under ``--skip-chart``.

    Named breaks that kill this test, each applied alone: **pass
    ``keep_lines=True``** to the ``render_controls`` call that prints the path in
    the ``chart: no regular file`` line, in the ``could not be read`` line, and in
    the ``is not`` line.
    """
    repo = _lf_checkout(tmp_path, monkeypatch)
    rendered = _lf_escaped(str(repo.resolve() / "open_stories.json"))

    # (a) No chart.
    assert main([]) == 0
    lines = capsys.readouterr().out.split("\n")
    assert (
        f"chart: no regular file at {rendered} — round-trip and S27 validator skipped." in lines
    ), lines

    # (b) A chart that does not parse.
    _write_chart(repo, "not json")
    assert main([]) == 1
    lines = capsys.readouterr().out.split("\n")
    prefix = f"FAIL: chart round-trip — {rendered} could not be read: "
    assert any(line.startswith(prefix) for line in lines), lines

    # (c) A chart that parses and does not round-trip.
    _write_chart(repo, '{"stories": []}')
    assert main([]) == 1
    lines = capsys.readouterr().out.split("\n")
    prefix = f"FAIL: chart round-trip — {rendered} is not "
    assert any(line.startswith(prefix) for line in lines), lines


def test_the_refusal_lines_keep_a_newline_venv_path_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both ``REFUSING:`` wordings print each venv path on one line when the
    checkout path holds a newline. The nested wording names the prefix and the
    repo venv, and the asserted sentence holds each, so a split in either breaks
    it.

    Named breaks that kill this test, each applied alone: **pass
    ``keep_lines=True``** to the ``render_controls`` call for the path in the
    ``IS the repo venv`` line, for ``sys.prefix`` in the ``is inside`` line, and
    for the repo venv in the ``is inside`` line.
    """
    repo = _lf_checkout(tmp_path, monkeypatch)
    venv = (repo.resolve() / ".venv").resolve()
    nested = venv / "envs" / "child"

    monkeypatch.setattr(sys, "prefix", str(venv))
    assert main(["--skip-chart"]) == 1
    lines = capsys.readouterr().out.split("\n")
    assert f"REFUSING: sys.prefix IS the repo venv {_lf_escaped(str(venv))}." in lines, lines

    monkeypatch.setattr(sys, "prefix", str(nested))
    assert main(["--skip-chart"]) == 1
    lines = capsys.readouterr().out.split("\n")
    assert (
        f"REFUSING: sys.prefix {_lf_escaped(str(nested.resolve()))} is inside the repo venv "
        f"{_lf_escaped(str(venv))}." in lines
    ), lines


def test_the_expect_lines_keep_a_newline_value_on_one_line(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ``--expect`` value holding a newline is printed on one line by the arm
    that reports an unknown revision and by the arm that rejects a leading dash.
    The dash case is one argument, ``--expect=-…``: as two arguments argparse
    reads ``-…`` as an option and never reaches the check.

    Named breaks that kill this test, each applied alone: **pass
    ``keep_lines=True``** to the ``render_controls`` call that renders
    ``args.expect`` once for the git checks, and to the one in the leading-dash
    rejection.
    """
    value = "a\nb"

    assert main(["--skip-chart", "--expect", value]) == 1
    lines = capsys.readouterr().out.split("\n")
    prefix = f"FAIL: --expect {_lf_escaped(value)}: `git cat-file -e` exited "
    assert any(line.startswith(prefix) for line in lines), lines

    assert main([f"--expect=-{value}"]) == 1
    lines = capsys.readouterr().out.split("\n")
    assert f"FAIL: --expect -{_lf_escaped(value)}: a revision may not start with '-'." in lines, (
        lines
    )


def test_the_repo_line_keeps_a_newline_path_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--repo`` path holding a newline that cannot be entered is printed on
    one line.

    Named break that kills this test: **pass ``keep_lines=True``** to the
    ``render_controls`` call for ``args.repo`` in the ``cannot enter that
    directory`` line.
    """
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "no\nsuch"

    assert main(["--repo", str(missing)]) == 1
    lines = capsys.readouterr().out.split("\n")
    prefix = f"FAIL: --repo {_lf_escaped(str(missing))}: cannot enter that directory — "
    assert any(line.startswith(prefix) for line in lines), lines


def test_the_printers_that_echo_argv_and_the_counts_render_them_too(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rule is about every string this script did not write, not only about paths.

    Three more sources reach the report and none of them is git's stdout or a
    filesystem path: the `--expect` value, echoed by EVERY arm that reports on it —
    the leading-dash rejection, the unknown revision, the ancestry result and both
    of its failures; the `--repo` value and the `OSError` naming it; and both
    `rev-list --count` results. The operator typed the first two, which makes them
    the mildest case in the class — and exactly the ones a later "these are ours,
    they're fine" would except. They are not this script's words, and holding the
    rule costs one call.

    The counts are stubbed because real `rev-list --count` prints digits and cannot
    falsify anything; the stub is the only way to reach that interpolation with a
    string somebody else chose.
    """
    hostile = "0\x1b[2K\x1b[1G9999"
    rendered = _escaped(hostile)
    assert "\\x1b" in rendered, "the staged value holds no escape — this proves nothing"

    # `--expect`, on the arm that reports an unknown revision.
    assert main(["--skip-chart", "--expect", hostile]) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"FAIL: --expect {rendered}: `git cat-file -e` exited " in out, out

    # `--expect`, on the arm that rejects a leading dash before git ever sees it.
    assert main([f"--expect=-{hostile}"]) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"FAIL: --expect -{rendered}: a revision may not start with '-'." in out

    # `--repo`, and the `OSError` message, which names the same value again.
    missing = synthetic_repo.parent / hostile
    assert main(["--repo", str(missing)]) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"FAIL: --repo {_escaped(str(missing))}: cannot enter that directory — " in out

    # Both counts, which are git's stdout on the ordinary path and unread otherwise.
    real_git = arrival.git

    def hostile_counts(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("rev-list", "--count"):
            return subprocess.CompletedProcess(list(args), 0, f"{hostile}\n", "")
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", hostile_counts)

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw escape reached the report"
    assert f"ahead of origin/HEAD (origin/HEAD..HEAD): {rendered}" in out
    assert f"behind origin/HEAD (HEAD..origin/HEAD): {rendered}" in out


# One `/`-free run per path component: POSIX forbids `/` in a name and forbids
# nothing else, so a newline inside a component is legal and a `/` is not. The
# components below are built from these lines rather than written out, so the fixture
# cannot drift from what it claims to forge.
#
# ⚠️ EVERY ONE OF THESE IS CHOSEN NOT TO COLLIDE WITH A GENUINE LINE OF THIS RUN, and
# two of them nearly did. `_build_repo` leaves the checkout on `story-branch` one
# commit ahead, so the genuine lines are `branch: story-branch`, `ahead ...: 1` and
# `behind ...: 0` — the first draft forged `behind ...: 0`, which the run prints
# honestly, and the assertion below counted the REAL line and passed for the wrong
# reason. The counts are `7` for that reason and for no other.
FORGED_LINES = (
    "branch: main",
    "ahead of origin/HEAD (origin/HEAD..HEAD): 7",
    "behind origin/HEAD (HEAD..origin/HEAD): 7",
    "chart round-trip OK",
    "PASS: nothing is asymmetric",
)


def test_a_newline_in_the_checkout_path_cannot_forge_a_report_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exemption that made the report readable defeated the rendering on a path.

    Tab and newline were exempted from `render_controls` BY DEFAULT, which is right
    for `status --porcelain`, `reflog -5` and the validator's two streams — they are
    made of lines — and wrong for every printer whose output is one line. Measured
    end to end on the earlier code, `--repo` under `--skip-chart` at a checkout whose
    directory-name components embed newlines: the `toplevel:` line printed them
    verbatim and the report carried `behind origin/main (HEAD..origin/main): 0`,
    `chart round-trip OK` and a validator-shaped `PASS:` line as lines of their own,
    byte-identical to genuine output, in a run that never opened a chart — exit 0.
    `chart round-trip OK` under `--skip-chart` is the decisive one: no chart was read,
    so no honest run could have printed it.

    The exemption is now opt-in and those four printers ask for it. The LAST FOUR
    assertions are what stops the fix over-reaching into them: `reflog -5` and
    `status --porcelain` are still printed as lines, not as one line of `\\n`
    escapes, which is the collapse a blanket strict rendering would cause.
    ⚠️ The two untracked files exist FOR the `status --porcelain` half, and are
    load-bearing: every other tree in this file is CLEAN, so that printer prints zero
    bytes and dropping its `keep_lines=True` was measured leaving that printer's
    exemption unheld, with every other item in this file green
    — a printer whose exemption nothing held.
    """
    base = tmp_path
    for component in (
        f"wo\n{FORGED_LINES[0]}",
        f"{FORGED_LINES[1].split('/')[0]}",
        f"{FORGED_LINES[1].split('/')[1]}",
        f"{FORGED_LINES[1].split('/')[2]}\n{FORGED_LINES[2].split('/')[0]}",
        f"{FORGED_LINES[2].split('/')[1]}",
        f"{FORGED_LINES[2].split('/')[2]}\n{FORGED_LINES[3]}\n{FORGED_LINES[4]}\narrive",
    ):
        base = base / component
        base.mkdir()
    repo = _build_repo(base, monkeypatch)
    assert "\n" in str(repo), "the checkout path holds no newline — this proves nothing"
    (repo / "untracked-one.txt").write_text("x\n", encoding="utf-8")
    (repo / "untracked-two.txt").write_text("x\n", encoding="utf-8")

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    lines = out.splitlines()
    for forged in FORGED_LINES:
        # Present as TEXT — otherwise the fixture never reached the report and the
        # assertion below proves nothing — but never as a LINE of its own. None of
        # the five is a line this run prints honestly; see FORGED_LINES.
        assert forged in out, f"{forged!r} never reached the report — this proves nothing"
        assert forged not in lines, (forged, lines)
    assert len([line for line in lines if line.startswith("toplevel: ")]) == 1, lines
    assert "\\n" in out, "the newlines were dropped rather than rendered"

    # The over-reach guard: the printers that ARE made of lines still are. Both of
    # the local ones, because each carries its own `keep_lines=True` and pinning one
    # leaves the other free.
    assert "git reflog -5:" in lines
    assert lines[lines.index("git reflog -5:") + 1].endswith("commit: story commit"), lines
    assert "git status --porcelain:" in lines
    assert lines[lines.index("git status --porcelain:") + 1 :][:2] == [
        "?? untracked-one.txt",
        "?? untracked-two.txt",
    ], lines


def test_the_validators_own_output_cannot_forge_a_report_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same forgery, through the ONE printer the sibling test above could not
    reach: the E23-S27 validator's streams, on the DEFAULT path.

    ⚠️ THE SIBLING PASSES `--skip-chart`, so it never opens a chart and never runs
    the validator — and the forgery survived there for two commits because of it.
    A review reproduced it on the shipped code: the validator prints the chart PATH
    raw (`Audited N rows in <path>.`), this script prints the validator's streams
    with `keep_lines=True` because they really are made of lines, and a checkout
    named with a newline therefore put an attacker's sentence at column 0 as a line
    of its own, byte-identical to genuine output, at exit 0. The `toplevel:` line
    two lines above rendered the same newlines correctly, which is what made it
    look closed.

    The cure is a RULE about the whole subprocess rather than a guard on the field
    that leaked: every line of the validator's output wears
    `VALIDATOR_LINE_PREFIX`, so no line of it can be read as a line of this report,
    whatever it prints and whatever it interpolates. Escaping the newline instead
    would collapse the validator's real multi-line report into one line, and fixing
    the validator's own interpolation would close one field and not the next one
    anybody adds to a program this script does not own.

    Named break that kills this test: **print the validator's streams unprefixed** —
    `print(scrub(audit.stdout, keep_lines=True), end="")` and its stderr sibling, as
    `check_chart` did. Observed, each assertion evaluated on its own under the break
    rather than inferred: THREE of the six below turn red — the forged sentence
    stands as a line of its own, and both prefix assertions fail. The other three
    do not discriminate: the fixture's own newline precondition holds either way,
    the run exits 0 either way, and the forged text is present as TEXT either way.
    The break also reddens every test that compares this script's
    output against `_validator_says`, which reproduces the prefix from the module's
    own constant — that is the prefix being well pinned, not an over-reach.

    ⚠️ FIXTURE NEUTRALISED, measured, and it neutralises ONE HALF: with an ordinary
    directory name, the two prefix assertions still fail under the break while the
    forgery assertion passes. So the newline in the path is what makes the FORGERY
    half real, and the prefix half stands on its own — which is why both are here.
    Neutralising the fixture alone, with the fix in place, leaves the test green,
    and that is the state the sibling `--skip-chart` test was permanently in.
    """
    # No `/` in it: the forged text is a PATH COMPONENT, and a slash would make
    # it two directories instead of one line. A `REFUSING:` line is also one this
    # run cannot print honestly — it is not refused — so its presence as a line
    # could only be the forgery.
    forged = "REFUSING: sys.prefix IS the repo venv nowhere"
    base = tmp_path / f"tree\n{forged}\narrive"
    base.mkdir()
    repo = _build_repo(base, monkeypatch)
    assert "\n" in str(repo), "the checkout path holds no newline — this proves nothing"
    _write_chart(repo, json.dumps(SYMMETRIC_CHART, indent=2) + "\n")

    assert main([]) == 0

    out = capsys.readouterr().out
    lines = out.splitlines()

    # It reached the report — otherwise everything below proves nothing.
    assert forged in out, out
    # And never as a line of its own, which is the forgery.
    assert forged not in lines, lines
    # The validator really ran, and every line it produced wears the prefix.
    validator_lines = [line for line in lines if line.startswith(arrival.VALIDATOR_LINE_PREFIX)]
    assert validator_lines, out
    assert any("Audited" in line for line in validator_lines), out


class _Unauthorized(http.server.BaseHTTPRequestHandler):
    """The smallest server that makes git ask for a credential: a bare 401."""

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="git"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the handler's stderr logging, which pytest would capture per request."""


@pytest.fixture()
def unauthorized_origin() -> Iterator[str]:
    """A local HTTP origin URL that answers every request with 401.

    A credential prompt is not reachable against any other remote in this file: every
    one of them is a local path, which needs no auth, and that is exactly why the
    `GIT_TERMINAL_PROMPT` assertion elsewhere had to be made on `subprocess.run`'s
    keywords rather than on behaviour. Bound to 127.0.0.1 on an ephemeral port, so it
    needs no network and cannot collide.
    """
    server = http.server.HTTPServer(("127.0.0.1", 0), _Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/r.git"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_hostile_checkouts_core_askpass_is_not_executed(
    synthetic_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unauthorized_origin: str,
) -> None:
    """`GIT_TERMINAL_PROMPT=0` was credited with a property it did not have.

    The module said prompting-disabled turns a credential prompt into a failure. It
    does not on its own: git consults an askpass helper BEFORE that prompt, so a
    `core.askPass` in the audited checkout's own `.git/config` — the file `--repo`
    has not read — runs anyway. Measured on git 2.53.0 with all three `-c` overrides
    applied AND `GIT_TERMINAL_PROMPT=0` set: the planted helper ran TWICE during one
    `git fetch origin`, once for the username and once for the password. With the
    helper replaced by `sleep 600` the fetch BLOCKED past 8s — the hang, not the
    failure, that the paragraph existed to prevent.

    `GIT_ASKPASS=""` in the child environment closes it, and an EMPTY value is read
    as "no askpass" rather than exec'd — the opposite of `core.sshCommand=`, which
    git tries to run (`cannot run :`) and which is why that key is declined rather
    than cleared. Measured, the helper then ran ZERO times and the fetch failed in
    0.04s with `could not read Username ...: terminal prompts disabled`.

    ⚠️ IT IS TAKEN, WHERE THE OTHER KEYS OF THIS CLASS ARE DECLINED, BECAUSE ITS
    CLOSURE IS FREE. Measured on this machine against an ordinary origin needing no
    credentials: `git fetch origin`, `git status --porcelain` and `git reflog -5` all
    still exit 0 with no extra output under it. The keys held out are held out
    because clearing them costs the ORDINARY path — `credential.helper` most of all,
    since the helper is what ANSWERS there. Askpass is only reached when no helper
    answers, and what it does then is interact; removing it removes no answer.

    The control is the point of the last two lines. If the helper does not fire in
    this fixture at all, the assertion above is vacuous — which is the shape the hook
    test records for the same reason.
    """
    marker = tmp_path / "askpass-ran"
    helper = tmp_path / "askpass.sh"
    helper.write_text(f"#!/bin/sh\necho ran >> {marker}\necho hunter2\n", encoding="utf-8")
    helper.chmod(0o755)
    _git(synthetic_repo, "remote", "set-url", "origin", unauthorized_origin)
    _git(synthetic_repo, "config", "core.askPass", str(helper))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.chdir(tmp_path)

    # The fetch fails either way — the origin answers 401 — so the exit code cannot
    # tell the two apart. Only the marker can.
    assert main(["--repo", str(synthetic_repo), "--skip-chart"]) == 1
    assert not marker.exists(), "the audited checkout's own config ran a command"

    control = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=synthetic_repo,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=arrival.SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert control.returncode != 0, control.stderr
    assert marker.exists(), "the helper never fires here — the assertion above is vacuous"


def test_a_symlink_loop_at_the_venv_path_is_diagnosed_rather_than_raised(
    synthetic_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--repo` walks a caller-named path with `resolve()`, and `resolve()` raises.

    The `.venv` refusal resolves `<root>/.venv` before comparing, and under `--repo`
    that root belongs to whoever the operator pointed at. Measured on CPython
    3.11.15, `Path.resolve()` on a symlink loop raises `RuntimeError("Symlink loop
    from ...")` — NOT an `OSError`, so neither `same_directory`'s handler nor
    anything else in this file caught it; the same 3.11 behaviour E31-S04 already hit
    in this repo. Measured end to end against the earlier code, `--repo` at a
    checkout whose `.venv` is half of a two-link cycle exited 1 on a RAW TRACEBACK
    with NO arrival information printed at all — the failure the chart and git
    handlers exist to prevent, in the one path that had no handler.

    The run now COMPLETES: a root whose candidate cannot be resolved has no answer to
    give, and this process's own `sys.prefix` is a directory it is running out of, so
    it cannot be inside a `.venv` the filesystem will not walk. The second assertion
    is what distinguishes "diagnosed" from "swallowed": the report has to be there.

    The `pytest.raises(RuntimeError)` below is the anti-vacuity check, and this
    docstring used to point at the last two lines instead — which is worse than
    imprecise, because those two lines PASS on a neutralised fixture. Measured:
    replacing the loop with `loop.mkdir()` and deleting the `pytest.raises` line
    leaves this test green; keeping the `pytest.raises` and neutralising only the
    symlinks turns it RED there, which is where the guard is. Without the loop the
    resolve never raises, so every assertion about the run passes on a plain
    checkout and measures nothing — on the SHIPPED code and on the pre-fix code
    alike. The whole discipline of this file is that a reader finds each guard by
    its docstring; this one pointed away from it.
    """
    loop = synthetic_repo / ".venv"
    other = synthetic_repo / "loop-other"
    loop.symlink_to(other)
    other.symlink_to(loop)
    with pytest.raises(RuntimeError):
        loop.resolve()

    monkeypatch.chdir(synthetic_repo.parent)

    assert main(["--repo", str(synthetic_repo), "--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {synthetic_repo.resolve()}" in out
    assert "branch: story-branch" in out
    assert "REFUSING" not in out


@pytest.mark.parametrize(
    ("failing", "prefix"),
    [
        (("fetch",), "FAIL: git fetch origin exited 128: "),
        (("rev-parse", "--show-toplevel"), "FAIL: `git rev-parse --show-toplevel` exited 128 "),
        (
            ("rev-list", "--count"),
            "FAIL: could not count against origin/HEAD — `git rev-list --count` exited 128: ",
        ),
        (
            ("rev-parse", "--git-common-dir"),
            "FAIL: could not locate the main checkout — `git rev-parse --git-common-dir` "
            "exited 128: ",
        ),
        (("cat-file", "-e"), "FAIL: --expect "),
    ],
)
def test_a_multi_line_git_stderr_cannot_split_the_line_it_is_printed_on(
    synthetic_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: tuple[str, ...],
    prefix: str,
) -> None:
    """Every printer that puts git's stderr INSIDE a sentence, and git's stderr is
    often several lines.

    `unavailable_marker` already joins its stderr onto one line, and
    `test_a_multi_line_git_stderr_is_marked_on_exactly_one_line` holds that. These
    do not join: each interpolates `stderr.strip()` into an f-string that opens
    with `FAIL:`. A newline inside it therefore ENDS the `FAIL:` line and starts new
    ones the operator reads as report output — the same forgery the checkout path
    was measured performing, from git's side rather than the filesystem's, and
    reachable on the fetch path from whatever a remote writes to stderr.

    They are covered together because they are every printer that interpolates
    git's STDERR into a `FAIL:` sentence, and because a fix applied to one of them
    reads as complete. This list is parametrized rather than described so that
    adding a printer means adding a case — and it has been wrong twice for exactly
    that reason. It held two; the commit that made the `rev-list` failure report
    git's own stderr instead of guessing a cause added a third and updated this
    list; the commit that did the same for `--git-common-dir` and for `--expect`'s
    `cat-file -e` added two more and did NOT, so this docstring claimed "every"
    over three of five while the two it missed were the two that commit wrote.
    THE LIST IS THE CLAIM: a printer that is not a case here is not covered by the
    sentence above it.

    Each is stubbed: the fetch failures elsewhere in this file are real and
    single-line, so none of them can see this. ⚠️ The `--expect` case is stubbed on
    `cat-file -e` and therefore needs `--expect` on the argv, which the run below
    passes unconditionally — a revision that never resolves, so the branch is
    reached on every parametrization and decides nothing for the other four.
    """
    forged = "PASS: nothing is asymmetric"
    stderr = f"error: could not read from remote\n{forged}\nfatal: unable to fetch\n"
    assert forged in stderr.splitlines(), "the staged stderr forges no line — this proves nothing"
    real_git = arrival.git

    def multi_line(*args: str) -> subprocess.CompletedProcess[str]:
        if args[: len(failing)] == failing:
            return subprocess.CompletedProcess(list(args), 128, "", stderr)
        return real_git(*args)

    monkeypatch.setattr(arrival, "git", multi_line)

    # `--expect` is passed on every parametrization so the `cat-file -e` printer is
    # reachable at all; for the other four the revision simply does not resolve and
    # the run was already failing.
    assert main(["--skip-chart", "--expect", "0" * 40]) == 1

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert forged in out, "git's stderr never reached the report — this proves nothing"
    assert forged not in lines, lines
    assert len([line for line in lines if line.startswith(prefix)]) == 1, lines
    # Rendered rather than dropped: the operator still gets everything git said.
    for fragment in stderr.split():
        assert fragment in out, (fragment, out)


# ---- E23-S91: git output decoded without a raise, cut at git's own newline,
# and read under a byte cap. -------------------------------------------------


def _stub_git(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes,
    stderr: bytes = b"",
    code: int = 0,
) -> None:
    """Put first on ``PATH`` a ``git`` that prints these bytes and exits ``code``.

    The stub copies the bytes from files with ``cat``, so no escape is typed
    into a tool call. It does not read stdin.
    """
    directory.mkdir()
    (directory / "stdout").write_bytes(stdout)
    (directory / "stderr").write_bytes(stderr)
    stub = directory / "git"
    stub.write_text(
        "#!/bin/sh\n"
        f"cat {shlex.quote(str(directory / 'stdout'))}\n"
        f"cat {shlex.quote(str(directory / 'stderr'))} >&2\n"
        f"exit {code}\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def test_a_reflog_subject_that_is_not_utf8_reaches_the_verdict_rendered(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A strict decode in ``git()`` raised ``UnicodeDecodeError`` on a reflog
    subject that holds byte 0xFF, and the run ended in a traceback after
    ``git reflog -5:`` (E23-S91 problem 3). The byte now prints as its escape.
    """
    # kills: scripts/verify/arrival.py:726 `stdout.decode("utf-8", "surrogateescape")` -> `stdout.decode("utf-8")`
    message = synthetic_repo.parent / "message"
    message.write_bytes(b"subject " + bytes([0xFF]) + b" latin\n")
    (synthetic_repo / "latin.txt").write_text("latin\n", encoding="utf-8")
    _git(synthetic_repo, "add", "latin.txt")
    _git(synthetic_repo, *COMMIT_IDENTITY, "commit", "-q", "-F", str(message))

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    newest = next(line for line in out.splitlines() if "HEAD@{0}:" in line)
    assert newest.endswith(" HEAD@{0}: commit: subject \\udcff latin"), out


def test_the_toplevel_line_keeps_a_non_ascii_space_that_ends_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``str.strip()`` removed a non-ASCII space at the end of the toplevel, so
    the line named a directory that does not exist.
    """
    # kills: scripts/verify/arrival.py:1013 `where.stdout.removesuffix("\n")` -> `where.stdout.strip()`
    repo = _build_repo(tmp_path, monkeypatch, work=f"work{chr(0xA0)}")

    assert main(["--skip-chart"]) == 0

    out = capsys.readouterr().out
    assert f"toplevel: {repo.parent.resolve()}/work\\xa0\n" in out, out


def test_the_main_checkout_root_keeps_a_non_ascii_space_that_ends_the_common_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--git-common-dir`` names a git directory whose name ends in a non-ASCII
    space. A sibling with the same name less that character is a symlink to
    another directory, so a cut name resolves to a different main root.
    """
    # kills: scripts/verify/arrival.py:820 `common.stdout.removesuffix("\n")` -> `common.stdout.strip()`
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    holder = tmp_path / "holder"
    holder.mkdir()
    common = holder / f"gd{chr(0xA0)}"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", f"--separate-git-dir={common}", str(work))
    elsewhere = tmp_path / "elsewhere" / "inner"
    elsewhere.mkdir(parents=True)
    (holder / "gd").symlink_to(elsewhere)
    monkeypatch.chdir(work)

    root, done = arrival.main_checkout_root()

    # Control: git printed the name with its last character.
    assert done.stdout == f"{common.resolve()}\n", repr(done.stdout)
    assert root == holder.resolve()


def test_the_branch_line_keeps_a_non_ascii_space_that_ends_the_name(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``git check-ref-format --branch`` accepts a name that ends in a non-ASCII
    space, and ``str.strip()`` removed it from the printed branch.
    """
    # kills: scripts/verify/arrival.py:1061 `branch.stdout.removesuffix("\n")` -> `branch.stdout.strip()`
    _git(synthetic_repo, "checkout", "-q", "-b", f"story{chr(0xA0)}")

    assert main(["--skip-chart"]) == 0

    assert "\nbranch: story\\xa0\n" in capsys.readouterr().out


def test_git_reads_stdout_up_to_its_cap_and_refuses_one_byte_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git()`` reads stdout under ``GIT_STDOUT_CAP``."""
    cap = arrival.GIT_STDOUT_CAP

    _stub_git(tmp_path / "at-cap", monkeypatch, stdout=b"a" * cap)
    done = arrival.git("reflog", "-5")
    assert (done.returncode, done.stdout) == (0, "a" * cap)

    _stub_git(tmp_path / "over-cap", monkeypatch, stdout=b"a" * (cap + 1))
    with pytest.raises(arrival.GitOutputOverCap) as excinfo:
        arrival.git("reflog", "-5")
    assert str(excinfo.value) == (
        f"`git reflog -5` printed more than GIT_STDOUT_CAP ({cap} bytes) on stdout"
    )
    assert cap == 16 * 1024 * 1024


def test_git_reads_stderr_up_to_its_cap_and_refuses_one_byte_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git()`` reads stderr under ``GIT_STDERR_CAP``."""
    cap = arrival.GIT_STDERR_CAP

    _stub_git(tmp_path / "at-cap", monkeypatch, stdout=b"", stderr=b"e" * cap, code=1)
    done = arrival.git("fetch", "origin")
    assert (done.returncode, done.stderr) == (1, "e" * cap)

    _stub_git(tmp_path / "over-cap", monkeypatch, stdout=b"", stderr=b"e" * (cap + 1), code=1)
    with pytest.raises(arrival.GitOutputOverCap) as excinfo:
        arrival.git("fetch", "origin")
    assert str(excinfo.value) == (
        f"`git fetch origin` printed more than GIT_STDERR_CAP ({cap} bytes) on stderr"
    )
    assert cap == 1024 * 1024


def test_an_over_cap_reflog_fails_the_run_and_names_the_command(
    synthetic_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An over-cap read is a failed run, never a pass. Every other call goes to
    the real git; ``reflog`` prints one byte more than the cap.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "reflog").write_bytes(b"a" * (arrival.GIT_STDOUT_CAP + 1))
    stub = stub_dir / "git"
    stub.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = reflog ]; then\n'
        f"    cat {shlex.quote(str(stub_dir / 'reflog'))}\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    assert main(["--skip-chart"]) == 1

    out = capsys.readouterr().out
    assert out.endswith(
        "git reflog -5:\n"
        f"FAIL: `git reflog -5` printed more than GIT_STDOUT_CAP "
        f"({arrival.GIT_STDOUT_CAP} bytes) on stdout\n"
    ), out[-500:]


def test_the_status_listing_shows_an_untracked_file_when_the_repository_config_hides_them(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository's own ``status.showUntrackedFiles=no`` hid an untracked
    file from the printed ``git status --porcelain``. The call now asks git for
    untracked files whatever the config says.
    """
    # kills: scripts/verify/arrival.py:1092 `git("status", "--porcelain", "--untracked-files=normal")` -> `git("status", "--porcelain")`
    _git(synthetic_repo, "config", "status.showUntrackedFiles", "no")
    (synthetic_repo / "stray.txt").write_text("stray\n", encoding="utf-8")
    # Control: the config hides the file from a plain listing.
    assert _git(synthetic_repo, "status", "--porcelain") == ""

    assert main(["--skip-chart"]) == 0

    assert "git status --porcelain:\n?? stray.txt\ngit reflog -5:\n" in capsys.readouterr().out
