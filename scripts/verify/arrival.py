#!/usr/bin/env python3
"""E23-S56 — the arrival ritual, as one command.

Every handoff prompt in this project opens with the same block: fetch, count both
directions against ``origin/HEAD``, print the toplevel and branch, show the
working tree and the reflog, and — where the wave chart exists — round-trip it
and run the E23-S27 symmetry validator. Re-pasted per handoff, that block loses a
line or gets run in the wrong tree. This script is that block.

BOTH ``rev-list --count`` DIRECTIONS ARE PRINTED, AND LABELLED. The count is
asymmetric and the two hazards are different: ``origin/HEAD..HEAD`` non-zero is
work this checkout has not pushed, ``HEAD..origin/HEAD`` non-zero is a checkout
that has fallen behind. One number cannot distinguish them.

THE ``.venv`` REFUSAL IS AN INHERITED GUARD ON THE INTERPRETER, AND IT KEYS ON A
RESOLVED PATH, NEVER ON A SKIP COUNT. The repo's own ``.venv`` is an editable
install pointing at the MAIN checkout, so a PYTEST SUITE run from it measures the
wrong tree; on 2026-09-05 that produced a gate with zero skips. ⚠️ THIS SCRIPT RUNS
NO SUITE. Nothing it prints would be wrong under that interpreter, so the refusal
is not protecting this script's own output — it is a guard on the INTERPRETER,
inherited from the gate context this file was written for and kept because the
story requires it to fire. Its cost is real, and is stated here rather than left
for the operator to discover: the commonest operator, the one with the project
virtualenv active, gets exit 1 and NO arrival information at all, and has to
re-run from a CI-shaped venv built outside the repo. "Zero skips" is the symptom,
not the rule — a skip count drifts as the suite grows, so a guard keyed on one
would go stale on its own, and this one is keyed on the interpreter instead. It
compares ``Path(sys.prefix).resolve()`` against a RESOLVED
``<git toplevel>/.venv`` AND ``<main checkout root>/.venv`` (from
``git rev-parse --git-common-dir``), so a symlink on either side is still caught
and a sibling venv such as ``.venv-gate`` is not a false positive.
Both roots are checked because story work happens in linked worktrees, which
contain no ``.venv`` of their own: against the toplevel alone the refusal would
never fire where it is needed, and against the main root alone it would miss a
``.venv`` symlinked into a worktree — that same absence's natural workaround, and
the one case where the toplevel is the sole match.

THE CHART IS LOOKED FOR AT BOTH ROOTS TOO, main checkout root FIRST, for the same
reason and with the same precedent: ``open_stories.json`` lives only at the main
checkout root, so E23-S27's own validator shipped with a ``parents[2]`` default
that resolved to the WORKTREE root and silently reported "chart not present" in
the common case. The order is the main root's by intent, not by sorting, and the objects that
would be sorted are the CANDIDATE PATHS rather than the roots. The roots sort the
right way round on their own — a prefix sorts first, so ``<main>`` precedes
``<main>/.claude/worktrees/<x>`` — and stating it of the roots, as this did, is
an argument a maintainer can check, find false, and then "simplify" by sorting.
It is the candidates that invert: ``<main>/.claude/worktrees/<x>/open_stories.json``
sorts before ``<main>/open_stories.json``, because ``.`` precedes ``o`` after the
shared prefix. So a stray chart copied into an agent worktree would shadow the
canonical one every time.

⚠️ WHAT THIS DOES AND DOES NOT GUARANTEE. Like every sibling in
``scripts/verify/``, this is a MANUAL harness. **Nothing runs it automatically:**
no command, no gate, no hook and no CI job invokes it. It is typed by whoever
arrives in a checkout, and it therefore *prevents* nothing — it reports, once, for
the operator who asked. It does not decide whether the toplevel, the branch,
the working tree or the reflog is the RIGHT one — it prints them so a reader can.
These decide its exit code: a ``--repo`` it cannot enter, a git call whose output
is over ``GIT_STDOUT_CAP`` or ``GIT_STDERR_CAP``, a working directory that
is not a git checkout, a ``rev-parse --git-common-dir`` that fails, the ``.venv``
refusal, an ``--expect`` that begins with ``-``, a non-zero ``git fetch origin``, a
``rev-list --count`` that fails FOR ANY REASON — the single cause this used to
name is the one the line itself stopped naming, since a timeout, a corrupt object
and an unborn HEAD all arrive there — ``--expect``'s
``cat-file -e`` and its ``merge-base --is-ancestor`` on either failing exit —
named rather than counted, since the second call has two failing branches and a
test for each, and — unless ``--skip-chart`` is passed — a chart that cannot be read
(unreadable, not UTF-8, not JSON, nested past the interpreter's recursion limit, over
``MAX_CHART_BYTES``, or exhausting memory in the parse), a
chart that does not round-trip, an E23-S27 validator subprocess that times
out (``check_chart``'s own early return, not an exit the validator ever produced),
and that validator's own exit. Git's EXIT CODES are what those read, never its text:
``rev-parse --show-toplevel`` exits 128 outside a work tree while printing nothing
on stdout, and a stale ``origin/HEAD`` would otherwise let both counts and
``--expect`` assert against yesterday's ref. Git's TEXT is either printed or RESOLVED TO A PATH, and never parsed for a
verdict. Two calls are resolved: ``rev-parse --show-toplevel`` and ``rev-parse
--git-common-dir``, and both are text the AUDITED CHECKOUT controls — a planted
``commondir`` file in ``.git/worktrees/<wt>/`` steers ``main_checkout_root``
at a directory the operator never named, and the chart lookup and the ``.venv``
refusal both read that root. That is a property of pointing ``--repo`` at a
checkout you have not read, which the ``--repo`` paragraph below says is not a
sandbox, and it is named here because "only ever printed" read as a bound and was
not one. The three informational calls are the other exception, and only to the
EXIT-CODE half: theirs is PRINTED as a marker and decides nothing; see
``unavailable_marker``.
⚠️ AND THE SCRUB BELOW IS NOT A GUARANTEE THAT NO SECRET IS PRINTED. It masks the
USERINFO FIELD of a ``scheme://`` URL and nothing else, so a token carried anywhere
else in git's text still reaches the transcript. The residuals below are each measured on
this file's own ``scrub``, and each one named below as PINNED is held by a unit
test in the suite so that widening the pattern cannot falsify that member
silently. The UNBOUNDED class's further examples are measured and NOT pinned —
nothing could pin a class with no boundary, and saying "each residual is pinned"
over it was a blanket the tests do not cover.
The first class is the token sitting somewhere the field is not, and it is
UNBOUNDED — measured, ``remote: Invalid token <token>``, an ``Authorization:
Basic <token>`` header echo and a ``machine host login x password <token>`` line
all come back unmasked, and nothing here could enumerate the rest. The members
pinned by a test are ``https://host/r.git?access_token=<token>`` (query string),
``https://host/<token>/r.git`` (path), and the scp-style
``user:<token>@host:org/repo.git``, which has no ``//`` at all, so the pattern's
lookbehind never fires.

THE SECOND CLASS IS INSIDE THE FIELD, and it too is a CLASS rather than the single
character this once named. The userinfo half matches one or more characters that
are neither a slash nor whitespace, followed by ``@`` — so a SLASH OR ANY
WHITESPACE before that ``@`` ends the run early and defeats the match entirely.
Measured, the token comes back unmasked through
``https://bob:pa/ss<token>@host/r.git`` and equally through an ASCII SPACE, a
TAB, U+00A0, U+2028 or U+3000 in the same position. Writing the residual as "an
unencoded slash" was the shape this file keeps finding: the instance someone
happened to try, standing in for the class it belongs to. All of these are
malformed per RFC 3986, which requires such a character in userinfo to be
percent-encoded, and they are noted rather than handled. What the rendering half
still does to them is separate and is why the residual is stated as the TOKEN
surviving rather than the string coming back byte-identical: under the DEFAULT
rendering a TAB or a U+00A0 in that position is itself rendered as its escape.
``keep_lines=True`` spares tab and newline BY NAME, so a string defeated by one
of those two comes back byte-identical, token and all, through ``status
--porcelain`` and ``reflog -5``, which pass it and add no prefix of their own.
The two PRINTABLE members, the slash and the ASCII space, come back byte-identical
in either mode; every remaining member is escaped in both.

The mirror image is over-masking and is cosmetic: the ``git@`` exemption is
case-sensitive, so ``https://Git@host/x`` is masked to ``***@`` while
``https://git@host/x`` is left alone. This repo's rule that a printed token must
be rotated still applies to what this script prints.

EVERY ``git`` CALL IS BOUNDED, NON-INTERACTIVE, RUN IN THE DIRECTORY THIS PROCESS IS
IN RATHER THAN THE ONE THE ENVIRONMENT NAMES, AND RUN WITH COMMAND-RUNNING CONFIG
KEYS AND THE AUDITED CHECKOUT'S HOOKS DISABLED — properties set by the ``git()``
helper and only there. Each is named with the thing that sets it, never numbered and
never counted: this sentence carried ordinals until a property was inserted ABOVE
them and renumbered every one, and it then carried "four" until a fifth was added, so
a number here is a claim that a later edit falsifies silently. BOUNDED by
``timeout=``; NON-INTERACTIVE by ``GIT_TERMINAL_PROMPT=0`` AND ``GIT_ASKPASS=""``;
RUN WHERE THIS PROCESS IS by removing every variable in ``GIT_LOCATION_ENV_VARS``
from the child's environment; CONFIG KEYS AND HOOKS by the ``-c`` settings in
``HOSTILE_CONFIG_OVERRIDES``. ``git fetch origin`` is a
network call, and git's credential machinery
blocks on an interactive prompt when a remote needs auth — a hang, not a failure, in
a script whose whole product is an exit code. ⚠️ ``GIT_TERMINAL_PROMPT=0`` DOES NOT
CLOSE THAT ON ITS OWN, and this paragraph credited it with doing so: git consults an
askpass helper FIRST, so a ``core.askPass`` in the audited checkout's own config runs
with prompting disabled — measured on git 2.53.0, twice per fetch, and a helper that
merely slept blocked the fetch past 8s. ``GIT_ASKPASS=""`` is the half that closes
that; see ``git()`` for the measurement and for why an empty value is read as "no
askpass" rather than exec'd. The pair turns the interactive path into a failure **for
HTTPS remotes only**: neither reaches ssh, which reads ``SSH_ASKPASS``, so an ssh
origin whose key needs a passphrase still blocks and there ``timeout=`` is the only
bound. The one non-``git()`` subprocess, the S27 validator in ``check_chart``, gets
the same ``timeout=`` and no ``env=``; it is this repo's own file and takes no network.

**EVERY STRING IN THIS REPORT THAT THIS SCRIPT DID NOT ITSELF WRITE IS RENDERED
BEFORE IT IS PRINTED.** That is a RULE, and it is a rule on purpose. The two
sentences it replaces were COUNTS — "at EIGHT call sites", then "APPLIED AT NINE
SITES, NOT EIGHT" — and each was true when it was measured and false by the end of
the commit that wrote it, because a count is falsified by adding a printer and a
rule is not. The second one was written directly above a fix for the first, which is
how a reader should read this paragraph: not as a list to check off, but as the one
sentence that stays true. Its scope is every string that arrives from git, from the
filesystem, or from this process's own ``argv`` — git's stdout and stderr wherever
they are printed, the checkout's resolved path, the chart's path and the exception
naming it, the ``.venv`` paths the refusal names, both ``rev-list --count`` results,
and the ``--expect`` and ``--repo`` values echoed back. Its complement is this file's
own literals, which nobody but this file can choose. There is no count here to check
it against, deliberately; ``grep`` for ``render_controls`` and ``scrub`` if you want
the sites, and if you add a printer of somebody else's string, route it.

THE RENDERING HAS TWO SPELLINGS, DIFFERING ONLY IN WHAT THEY EXEMPT, AND THE
EXEMPTION IS OPT-IN. The printers that are MADE of lines — ``status --porcelain``,
``reflog -5``, and the S27 validator's stdout and stderr — pass ``keep_lines=True``,
because tab and newline are their structure. EVERY OTHER PRINTER EXEMPTS NOTHING,
newline included, because each of those lines is one claim and a newline inside it
forges another; ``RENDERED_VERBATIM`` records the report that was forged while the
exemption was a default.

THE USERINFO HALF IS NARROWER THAN THE RENDERING HALF, and only git's TEXT goes
through it (``scrub``), because git's text is where a remote URL appears: git echoes
the remote URL it failed on, and records the clone and pull URL verbatim in the
reflog, so a checkout where anyone ran ``git pull https://<token>@host`` prints that
token on every run — and this repo's rule is that a token printed to a transcript
must be rotated. Some of git's text cannot realistically carry userinfo at all — a
branch name, a count, a validator handed one chart path — and is routed through
``scrub`` anyway, so that a reader has a rule to check rather than a list. Paths and
``argv`` go through ``render_controls`` ALONE and never through ``scrub``: measured,
``scrub("/tmp/a//b@c/work")`` returns ``/tmp/a//***@c/work``, so the userinfo half
would eat part of a legitimate checkout path on the exact line a reader uses to
decide which tree they are in.

WHAT THE RENDERING IS FOR, measured on git 2.53.0 and on this tree:

* a COMMIT SUBJECT, which ``reflog -5`` prints. A subject of ``benign`` followed by
  ESC ``[2K`` ESC ``[1G`` and then ``clean tree, nothing to see`` arrived unchanged
  (checked with ``od -c``); on a terminal that pair erases the line and rewrites it
  from column 1. So does a subject of ``release fix`` U+202E ``txt.exe`` U+202C
  ``ready``, which no range list caught — see ``RENDERED_VERBATIM``.
* the CHECKOUT'S OWN PATH, which reaches the ``chart:`` line, both
  ``FAIL: chart round-trip`` lines and both ``REFUSING:`` lines as well as
  ``toplevel:`` — every one of them unrendered until this commit, which is the
  measurement behind the rule above rather than a count to maintain. Measured on the
  earlier code, in a checkout directory named
  ``wo`` ESC ``[2K`` ESC ``[1G`` ``clean tree, nothing to see``: the ``toplevel:``
  line rendered correctly and two lines below it the ``chart:`` line printed the same
  string raw and performed the same erase-and-rewrite — on the DEFAULT path, exit 0,
  as the report's LAST line. It is reachable through a SYMLINK, so an operator who
  never types the name still gets it: ``resolve()`` replaces the link they typed with
  the name they did not.
* the ``--expect`` and ``--repo`` values, and the ``OSError`` message naming the
  latter. The operator typed these, so they are the mildest case — but they are not
  this script's words either, and the rule is cheaper to hold than to except from.

``--repo <dir>`` RUNS THE CHECKS IN THAT DIRECTORY, restoring the original one
afterwards. Reading the current directory only, this script run from the main
checkout audits ``main`` — toplevel, branch and both counts — and still exits 0, a
pass that says nothing about the branch the operator meant to look at.
⚠️ THE CHDIR IS NOT ENOUGH ON ITS OWN, because git reads a repository's location from
its ENVIRONMENT first — see ``GIT_LOCATION_ENV_VARS``, which ``git()`` strips, and
which was measured turning ``--repo <A>`` into a silent exit-0 audit of a different
checkout entirely.
⚠️ IT IS NOT A SANDBOX. WHAT IS CLOSED IS NAMED BY KEY, NEVER BY ORDINAL OR COUNT —
``core.fsmonitor``, ``protocol.ext.allow``, ``core.hooksPath`` and ``core.askPass``,
and that is all. (An ordinal here is a claim a later edit falsifies silently: this
paragraph and the one below it said "three" until ``core.askPass`` joined them, and
the ``git()`` docstring records the same lesson about the properties it sets.) It
chdirs into a caller-named directory and runs git there, so that checkout's own
configuration is otherwise honoured, and git config and git's hooks can each name a
command to run. Measured on this machine (git 2.53.0), against a checkout carrying
them in its OWN ``.git/config`` or its own ``.git/hooks``:

* ``core.fsmonitor=<script>`` ran that script during ``git status --porcelain``
  and this script still exited 0;
* ``protocol.ext.allow=always`` beside ``remote.origin.url=ext::<script>`` ran a
  second script during ``git fetch origin``;
* an executable ``.git/hooks/reference-transaction`` ran during ``git fetch
  origin`` — it fires on the remote-tracking ref update — and the run exited 0,
  the same shape and outcome as the first;
* ``core.askPass=<script>`` ran that script TWICE during one ``git fetch origin``,
  with the other three overrides applied AND ``GIT_TERMINAL_PROMPT=0`` set, because
  git consults an askpass helper BEFORE the prompt that variable disables. See
  ``git()``, which closes it with ``GIT_ASKPASS=""`` in the child environment rather
  than with a ``-c``.

``git()`` now passes ``-c core.fsmonitor=false -c protocol.ext.allow=never
-c core.hooksPath=/dev/null``, which stopped the first three, because a command-line
``-c`` outranks every config file. Measured, ``core.hooksPath=/dev/null`` costs an
ordinary run nothing: fetch, ``status --porcelain`` and ``reflog -5`` each exit 0
under it with no extra output.

⚠️ THAT IS NOT AN AUDIT OF GIT'S CONFIG SURFACE. THE CLASS IS LARGER THAN WHAT THIS
FILE CLOSES, and the keys held out are named by key rather than numbered, for the
reason above: ``core.sshCommand``, ``remote.<name>.uploadpack``, ``core.gitProxy``,
``credential.helper`` and ``filter.<driver>.clean``. Measured with the three ``-c``
overrides above already applied, each against a checkout carrying the key in its own
``.git/config``: ``core.sshCommand=<script>`` ran that script during
``git fetch origin`` against an ``ssh://`` origin; ``remote.origin.uploadpack=<script>``
ran its script against a local-path origin and the run exited 0; ``core.gitProxy=<script>``
ran its script against a ``git://`` origin; ``credential.helper`` is the worst of
them, being able to hand over the operator's stored credentials; and
``filter.<driver>.clean`` is the most reachable, needing no remote, no network and no
origin URL — it ran during the LOCAL-ONLY ``status --porcelain`` with the run exiting
0. NONE of those is cleared here, and each absence is a deliberate decision recorded
at ``HOSTILE_CONFIG_OVERRIDES`` with the measurement behind it — every one of them
costs the ORDINARY path, which is what separates them from ``core.askPass``, whose
closure was measured costing it nothing.
``--repo`` is still only as trustworthy as the directory the operator names, so
point it at your own worktrees.

``--skip-chart`` exists because the chart is gitignored, per-machine and hand-edited
by several concurrent sessions: an operator whose errand is a diff and not the chart
must not be stopped by someone else's half-written chart, and passes it. E23-S27's
own validator records why that matters — "a permanently red gate gets silenced or
deleted, which is how this defect class survives".

Not collected by pytest (``[tool.pytest.ini_options] testpaths = ["tests"]``) and
not measured by coverage: ``[tool.coverage.run] source = ["agent"]`` bounds the
report to ``agent/``, and measured, this file is imported by the suite below and
still does not appear in ``coverage report``. It adds no ``omit`` entry.
``scripts/verify/e23_s27_merge_symmetry.py`` — same directory, same shape, the file
this one is modelled on — deliberately carries none and records why, that a needless
``omit`` is a trap for whoever later moves the file into ``agent/``. The
``# pragma: no cover`` on the ``__main__`` guard below is inert for the
same reason and is kept against the same widening. Its behaviour is proved by
``tests/e23/test_e23_s56_arrival.py``, which builds a synthetic repo with a bare
``origin`` rather than reading this one.

Run:  python3 scripts/verify/arrival.py [--repo <dir>] [--expect <sha>] [--skip-chart]
Exit codes:
  0   arrival checks ran and nothing this script decides on is wrong
  1   not a git checkout, refused from the repo venv, or any check above failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO, NoReturn

REPO_VENV_DIRNAME = ".venv"
CHART_FILENAME = "open_stories.json"
VALIDATOR_FILENAME = "e23_s27_merge_symmetry.py"
SUBPROCESS_TIMEOUT_SECONDS = 120
TIMEOUT_RETURNCODE = 124

# Byte caps on each git call's output (E23-S91). `git()` reads each stream to its
# cap plus one byte and raises `GitOutputOverCap` above the cap, and `main` then
# fails the run.
GIT_STDOUT_CAP = 16 * 1024 * 1024
GIT_STDERR_CAP = 1024 * 1024

# ⚠️ THIS BOUNDS THE BYTES READ. IT DOES NOT BOUND THE PARSE, and the sentence that
# said "it still bounds" generalised one shape into a property — the same defect this
# story keeps producing, here in the fix that closed the previous instance.
#
# What the byte bound really buys, measured on CPython 3.11.15 with
# `resource.getrusage(RUSAGE_SELF).ru_maxrss`, each run in a fresh interpreter against
# a 13.9 MiB baseline: `read_text` on a 64 MiB file grows RSS by 128.0 MiB, 2.00x the
# file, for a record-shaped chart and for a file of NUL bytes alike. That is the
# property, and it is linear in this constant.
#
# `json.loads` on the same bytes is a SEPARATE amplification that this constant does
# not control, and it depends on the SHAPE of the file, not its size. Measured at
# 64 MiB, growth over the same baseline, nine rows:
#
#   `[],[],[]...`        22369621 containers   1671.5 / 1718.4 MiB (two runs)  26.1-26.8x
#   `{},{},{}...`        22369621 containers   1671.5 MiB                      26.1x
#   `1.5,1.5,...`               1 container     772.0 MiB                      12.1x
#   `12345,12345,...`           1 container     558.1 MiB                       8.7x
#   distinct strings            1 container     495.7 MiB                       7.7x
#   `1,1,1,...`                 1 container     386.1 MiB                       6.0x
#   real chart rows repeated  139946 containers 294.4 MiB                       4.6x
#   `"a","a",...`               1 container     258.1 MiB                       4.0x
#   `true`/`null` repeated      1 container     232.5 MiB                       3.6x
#
# So an adversarial 64 MiB chart — AT this bound, which the `>` comparison admits —
# peaks around 1.7 GiB, ABOVE the
# 1.01 GiB that the unbounded 512 MiB read below is cited for. Measured against that
# earlier read: a 512 MiB file at the chart path was read to completion — 536870912
# characters — at 1.01 GiB peak RSS, 2.03x the file, growing with the file and never
# stopping.
#
# BOTH PRESCRIBED FIXES WERE MEASURED AND BOTH ARE DECLINED, each recorded here with
# the output rather than left for the next reader to re-derive:
#
# * REFUSING ON AN IMPLAUSIBLE BRACKET COUNT does not bound the object graph, it
#   bounds one shape of it. Measured above: `1.5,1.5,...` at 64 MiB holds exactly ONE
#   container and still costs 772.0 MiB, 6.0x what the read alone costs, and the five
#   other single-container rows cost between 232.5 and 558.1 MiB. A bracket bound passes
#   every one of them untouched. Adopting it would ALSO defang a test that measures
#   something real: `test_a_deeply_nested_chart_fails_the_run_instead_of_raising`
#   stages 60000 nested arrays in 120000 bytes, which any bracket bound tight enough
#   to catch the 26.1x shape rejects before `json.loads` is reached.
# * RE-DERIVING THIS NUMBER FROM THE ADVERSARIAL SHAPE does not buy the property
#   either, at any value the real chart can live with. Measured on the 26.1x shape:
#   32 MiB peaks at 836.8 MiB — still 0.81x the 1.01 GiB ceiling it would be derived
#   against — while costing the chart 401 days of headroom (791 down to 390 at the
#   measured accretion rate below). 16 MiB peaks at 419.5 MiB and leaves 189 days,
#   which is the permanently-red gate the `--skip-chart` note quotes E23-S27 about,
#   the same trap that made this commit raise the bound in the first place.
#
# WHAT IS TAKEN INSTEAD is the one fix with no cost on the ordinary path:
# `MemoryError` JOINS THE `except` BELOW. It is not a subclass of `OSError` or of
# `ValueError` (measured: `issubclass` is False both ways) and not of `RuntimeError`
# either, so the handler did not see it — the same argument this file already records
# for `RecursionError`, on the one failure a shape-dependent parse actually ends at.
# That converts the harm this paragraph is about, a raw traceback out of `json.loads`
# on a constrained machine, into the diagnosed FAIL line the guarded read exists to
# print. It does NOT make the parse bounded, and nothing below claims it does.
#
# THE SIZE OF THE REAL CHART IS MEASURED, NOT ESTIMATED, and this bound is set from
# the measurement. `open_stories.json` at the main checkout root was 923179 bytes
# (0.880 MiB) across 153 `stories` rows on 2026-09-06. It ACCRETES: its own backup
# series at that root runs 695456 bytes on 2026-09-03 08:15 to 923179 on 2026-09-06
# 01:34 — 227723 bytes in 65.3 hours, ~84 KB/day. This file previously bounded the
# read at 4 MiB and called that "~40x headroom"; measured, 4 MiB is 4.54x, and at
# ~84 KB/day the REAL chart reaches it in ~39 days. A bound the real chart trips is
# the permanently-red gate the `--skip-chart` note below quotes E23-S27 about, so it
# is raised rather than re-described: 64 MiB is 72.7x the measured chart and ~791
# days of accretion at the measured rate, which is long enough that it needs no
# calendar revisit. What it still bounds is the READ, at the 2.00x measured at the
# top of this comment, and nothing else; the earlier sentence here claimed "a 64.0 MiB
# growth" for that read, which is neither the measurement (128.0 MiB) nor the
# subtraction it was written as (143.6 - 79.5 = 64.1). Re-derive the multiple and the
# days from the chart before changing this number; the ~40x sentence was true of no
# chart this repo ever had.
MAX_CHART_BYTES = 64 * 1024 * 1024

# `//` then everything up to the LAST `@` before the next `/` or space — the whole
# userinfo field of a `scheme://` URL. It is not narrowed to the `user:password@`
# form: GitHub documents a personal access token as the USERNAME
# (`https://<token>@github.com/o/r.git`), which carries no colon at all, and a
# password containing a literal `@` splits the field so that a shorter match leaks
# the tail. The one exemption is a COMPLETE userinfo of `git@`, the conventional
# ssh user, which is not a secret and is the half of `ssh://git@host/path` an
# operator needs in order to act. The lookahead has to run to the end of the field
# rather than stop after four characters: it is evaluated once, at the position
# after `//`, while the match itself is greedy to the LAST `@`, so a bare `(?!git@)`
# exempted every userinfo that merely BEGINS `git@` — measured,
# `https://git@SECRET@host/x` came back verbatim. A `scheme://` URL with no userinfo
# has no `@` before its path, so `https://host/path` never matches. ⚠️ scp-style
# remotes (`deploy@host:org/repo.git`, the form the reference log records verbatim)
# have no `//` and are NOT considered here at all.
URL_USERINFO = re.compile(r"(?<=//)(?!git@[^/\s@]*(?:[/\s]|$))[^/\s]+@")

# The test is PRINTABILITY, not a list of ranges, and it is the sibling's:
# `e23_s27_merge_symmetry.py`'s `_safe` renders `c if c.isprintable() else
# repr(c)[1:-1]` for exactly this reason. This file first shipped the range list
# `[\x00-\x08\x0b-\x1f\x7f-\x9f]` — C0 except tab and newline, DEL, and the C1 block —
# and a range list is only ever as wide as the ranges someone thought of. Measured on
# CPython 3.11.15 against that list, every one of these came back from `scrub`
# BYTE FOR BYTE: U+202E RIGHT-TO-LEFT OVERRIDE, U+202D, U+202C, U+2066, U+200B,
# U+200D, U+00A0 and U+2028. And end to end, measured: a commit subject of
# `release fix<U+202E>txt.exe<U+202C> ready` reached the printed reflog line unchanged
# (`'... commit: release fix‮txt.exe‬ ready'`) and the run exited 0 — on any
# bidi-aware terminal that renders the filename reversed, which is the same class of
# lie as the ESC sequence the header records. `str.isprintable()` is False for all of
# them, and False for the whole range list too, so the wider test is a superset of the
# narrower one and no case the range list caught is lost.
#
# TAB AND NEWLINE ARE THE EXEMPTION, AND THE EXEMPTION IS OPT-IN. They are what the
# printers that are MADE of lines are made of: `reflog -5`, `status --porcelain` and
# the S27 validator's two streams are multi-line, and a tab inside one of their lines
# moves the cursor forward only. `isprintable()` is False for both, so they are named
# here rather than inherited, and rendered rather than dropped where they are not
# exempt, so a line that contained one still says so.
#
# ⚠️ IT IS OPT-IN — `keep_lines=True`, passed by the printers named above and by
# nothing else —
# because it was a DEFAULT, and as a default it defeated the rendering on the very line
# that rendering was added to protect. Measured on this tree, end to end, `--repo`
# under `--skip-chart` at a checkout whose directory-name components embed newlines
# (POSIX forbids `/` in a name and nothing else): the `toplevel:` line printed those
# newlines verbatim, and the report an operator read carried `behind origin/main
# (HEAD..origin/main): 0`, `chart round-trip OK` and a validator-shaped
# `PASS: nothing is asymmetric` as five lines of their own, byte-identical to genuine
# output, in a run that never opened a chart at all — exit 0. A path is one claim on
# one line; a newline inside it is a second claim this script did not make.
RENDERED_VERBATIM = "\t\n"


# Git reads a repository's location from its ENVIRONMENT before it reads the current
# directory, so each of these outranks the `os.chdir` that `--repo` does, and an
# operator invoked from a git hook, `git bisect run` or `git rebase --exec` has them
# exported already. There is no `-c` for this and no "ignore the environment" flag:
# the only fix is to leave them out of the child's environment.
#
# Measured on git 2.53.0, each variable alone, `--repo <checkout A>` with the variable
# pointed at a second checkout B, against a control run with it unset:
#
# * `GIT_DIR` — the branch line reported B's branch under A's toplevel;
# * `GIT_WORK_TREE` — the toplevel line reported B while the branch stayed A's;
# * `GIT_COMMON_DIR` — `main_checkout_root()` became B's root, so the chart lookup
#   named B's chart (and the `.venv` refusal reads that same root);
# * `GIT_INDEX_FILE` — `status --porcelain` reported `D f.txt` in a tree where
#   nothing had been deleted;
# * `GIT_OBJECT_DIRECTORY` — pointed at an empty directory, `cat-file -e` exited 128,
#   so `--expect <a commit that IS in this checkout>` was reported as "not a commit in
#   this checkout";
# * `GIT_ALTERNATE_OBJECT_DIRECTORIES` — pointed at B's object store, `--expect <a
#   commit that is NOT in this checkout>` stopped saying "not a commit in this
#   checkout" and said "not ... nor a descendant of it" instead.
#
# ⚠️ THE ONE THAT MATTERS IS THE PAIR, because it PASSES. With `GIT_DIR` and
# `GIT_WORK_TREE` both set to a valid sibling checkout, the fetch and both counts
# succeed: measured, `--repo <A> --skip-chart` printed B's toplevel and B's branch and
# EXITED 0. A silent wrong-tree pass is the single failure this script exists to
# prevent, and it is the one an operator cannot see.
GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


# Settings that make `git` run a command of the audited checkout's choosing. A
# command-line `-c` outranks every config file, including the audited checkout's own
# `.git/config` — which is exactly the file `--repo` has not read yet. `core.fsmonitor`,
# the `ext::` transport and a `.git/hooks/reference-transaction` script were each
# measured executing a planted script here (see the `--repo` note above).
#
# ⚠️ "NO COST" IS NARROWER THAN IT READS, and the earlier wording here — "each is
# disabled at no cost to any ordinary run" — was measured against ONE shape of
# ordinary run. What is measured is that against an `https` origin, with no fsmonitor
# and no ref hooks configured, `git fetch origin`, `git status --porcelain` and
# `git reflog -5` all still exit 0 under all three overrides with no extra output. A
# checkout that legitimately uses any of the three features does pay:
# `protocol.ext.allow=never` refuses the `ext` transport OUTRIGHT — measured,
# `git -c protocol.ext.allow=never fetch origin` against an `ext::` remote fails with
# `fatal: transport 'ext' not allowed` without reaching the helper at all, where the
# control without the override went on to run the named command — so an operator whose
# origin legitimately IS an `ext::` remote gets exit 1 on every run.
# `core.hooksPath=/dev/null`
# suppresses a legitimate `reference-transaction` hook, which the hook test's control
# shows firing without the override, and `core.fsmonitor=false` declines whatever
# speedup a configured fsmonitor would have given. That is the same "cost on the
# ordinary path" shape used to decline the keys below; the difference is one of
# degree — these cost a checkout that opted into an unusual feature, while
# clearing `credential.helper` costs THIS repo's own everyday fetch.
#
# ⚠️ `core.askPass` IS ALSO CLOSED, AND IT IS NOT CLOSED HERE — named by key rather
# than counted in with these three, because a `-c` is the wrong instrument for it: `git()` sets
# `GIT_ASKPASS=""` in the child's environment instead, which outranks both this
# tuple's reach and the audited checkout's `core.askPass`. It is listed by name in
# the header alongside these three rather than being counted with them. See `git()`
# for why it is taken where the keys below are declined: measured, its closure costs
# the ordinary path nothing.
#
# ⚠️ MORE KEYS OF THE SAME CLASS ARE DELIBERATELY NOT HERE — `core.sshCommand`,
# `remote.<name>.uploadpack`, `core.gitProxy`, `credential.helper` and
# `filter.<driver>.clean`, named by key rather than numbered because a later edit
# renumbers an ordinal silently. Each absence is a
# decision with a measurement behind it, not an oversight, and together they are why
# the header says `--repo` is not a sandbox.
#
# `credential.helper=` is the worst of them — `credential.helper=!<cmd>` runs a shell
# command, and can hand the operator's stored HTTPS credentials to a host the audited
# checkout names. But clearing it costs this script its whole reason to exist.
# Measured on this repository, whose `origin` is `https://github.com/...` and whose
# operator authenticates through `osxkeychain`: `git fetch origin` exits 0, and
# `git -c credential.helper= fetch origin` fails with `could not read Username for
# 'https://github.com': terminal prompts disabled`. So clearing it turns the fetch —
# and with it `origin/HEAD`, both counts and `--expect` — into a hard exit 1 on the
# ORDINARY path, for every operator, to close a hole that needs `--repo` aimed at a
# hostile checkout AND a server that actually asks for credentials.
# ⚠️ THAT IS ALSO WHY `GIT_ASKPASS=""` IS FREE AND THIS IS NOT, and the two are easy
# to confuse: the credential HELPER is what answers on the ordinary path, so clearing
# it removes an answer this repo's own fetch depends on. Askpass is only reached when
# NO helper answers — and what it does there is prompt, which is the hang this script
# is already choosing to fail on. Clearing it removes no answer, only an interaction.
#
# `core.sshCommand` was measured running a planted script during `git fetch origin`
# against an `ssh://` origin, with the three overrides above already applied. It is
# left alone because NEITHER spelling of the fix is free, both measured on git 2.53.0:
#   - `-c core.sshCommand=` breaks EVERY ssh remote. An empty value is honoured as the
#     command rather than read as "unset", so git tries to exec it:
#     `error: cannot run : No such file or directory` / `fatal: unable to fork`. The
#     control, the same fetch with no override, reached the real `ssh`.
#   - `-c core.sshCommand=ssh` restores the default binary but CLOBBERS an operator's
#     own `core.sshCommand`. Measured with `core.sshCommand=<script>` in the global
#     config: under the override that script did not run and plain `ssh` did — so an
#     operator whose ssh remotes need `ssh -i <key>` loses their key on every fetch.
# Both are the `credential.helper` shape again: a cost on the ORDINARY path, for every
# operator, against a hole that needs `--repo` aimed at a hostile checkout.
#
# `remote.<name>.uploadpack` was measured running a planted script during
# `git fetch origin` against a local-path origin, with the run exiting 0. It is not
# cleared because the only override that would do it names ONE remote
# (`-c remote.origin.uploadpack=...`), which is both narrower than the hole — the
# audited checkout chooses its own remote names — and a live cost to any operator
# whose server keeps `git-upload-pack` off the default path.
#
# `core.gitProxy` was measured running a planted script during `git fetch origin`
# against a `git://` origin. It is left alone for the same reason as the two above:
# clearing it would break an operator who legitimately fetches through a proxy.
#
# `filter.<driver>.clean` is the MOST REACHABLE of the keys held out, not the least:
# it needs no remote, no network and no origin URL. A `.gitattributes` in
# the audited worktree names the driver and the checkout's own `.git/config` supplies
# the command. Measured on git 2.53.0 with all three overrides above applied, against
# a checkout carrying `filter.evil.clean=<script>` and `* filter=evil`: the script ran
# during `git status --porcelain` — the LOCAL-ONLY working-tree check — and the run
# exited 0. Two things about that measurement, both needed to reproduce it: the
# worktree file must differ from the index at the SAME SIZE, because where the sizes
# differ git can call the file modified without hashing it and the clean driver is
# never reached (measured, with a size-changing edit the script did not run, in the
# control either); and the
# control matters — plain `git status --porcelain` in the same tree ran it too, so it
# is not the overrides letting it through. It is declined because there is no override
# that closes it: driver names are arbitrary, `-c filter.<driver>.clean=` names one
# driver, and git has no blanket switch and no way to ignore an in-tree
# `.gitattributes` from the command line.
HOSTILE_CONFIG_OVERRIDES = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "core.hooksPath=/dev/null",
)


def render_controls(text: str, *, keep_lines: bool = False) -> str:
    """Render every non-printable character as its escape; ``keep_lines`` spares tab and newline.

    This half is about the REPORT rather than about secrets, which is why it is a
    function of its own rather than only the tail of ``scrub``. The header states the
    rule it serves: every string in this report that this script did not itself write
    is rendered before it is printed. It is a function rather than a habit so that
    ``grep`` can find the call sites, and it is applied at the PRINTER rather than
    where a value enters, because a value that is rendered early would then be handed
    to ``git`` or to ``Path`` in its escaped form.

    ⚠️ ``keep_lines`` DEFAULTS TO FALSE, which is the safe direction: a new printer
    that reaches for this function gets the strict rendering without asking, and the
    printers that are MADE of lines have to say so. They are named where they are
    listed and not counted here: a count is a claim the next line-printer
    falsifies silently, which is this file's own rule. It was the other way round
    once — see ``RENDERED_VERBATIM`` for the measured report that a newline in a
    checkout path forged, under an exemption that was on by default.

    ``repr(char)[1:-1]`` is the sibling validator's spelling and gives the escape
    Python itself would print — ``\\x1b`` for ESC, ``\\u202e`` for the right-to-left
    override — so a line that held one still says so.
    """
    verbatim = RENDERED_VERBATIM if keep_lines else ""
    return "".join(
        char if char.isprintable() or char in verbatim else repr(char)[1:-1] for char in text
    )


#: Every line of the E23-S27 validator's output wears this. It is what keeps a
#: subprocess this script does not own from writing a line of this script's
#: report — see ``check_chart`` for the measured forgery that made it necessary.
VALIDATOR_LINE_PREFIX = "s27: "


def scrub(text: str, *, keep_lines: bool = False) -> str:
    """Make GIT'S TEXT safe to print: blank URL userinfo, then render control characters.

    ⚠️ THIS IS FOR GIT'S TEXT AND NOT FOR EVERY EXTERNAL STRING. The rendering half
    applies to all of them; the userinfo half applies here because git's text is where
    a remote URL appears. A PATH OR AN ARGV VALUE must not come through here:
    measured, ``scrub("/tmp/a//b@c/work")`` returns ``/tmp/a//***@c/work``, because
    ``URL_USERINFO`` reads ``//b@`` as a userinfo field and blanks a real directory
    name out of the line a reader uses to decide which tree they are in.

    ⚠️ WHERE THAT BITES IS THE UNRESOLVED ARGV, not the resolved paths this used to
    name. The ``toplevel:``, ``chart:`` and ``REFUSING:`` lines all print a
    ``Path.resolve()``-ed value, and ``resolve()`` collapses a doubled separator, so
    ``//`` cannot survive to reach them and swapping ``scrub`` in at any of them was
    measured changing nothing. ``--repo`` and ``--expect`` are echoed back EXACTLY as
    typed, by EVERY line that reports them — not only the ``FAIL:`` ones, since
    ``origin/HEAD is <expect> or a descendant of it`` is a SUCCESS line that echoes
    it too — and those are the values that can hold ``//``: measured,
    ``render_controls("/tmp/a//b@c/work")`` returns it intact where ``scrub``
    returns ``/tmp/a//***@c/work``. Paths and argv go through ``render_controls``
    alone for that reason, and EACH of the two values is pinned by its own break,
    so the distinction is not prose alone.

    ``scheme://`` and nothing wider for the userinfo half: the pattern's lookbehind is
    a literal ``//``, so
    a scp-style remote is not a URL as far as this is concerned, and a token outside
    the userinfo field is not touched at all. The header's residual paragraph states one
    UNBOUNDED class outside the field and one class inside it. It used to say "the
    four measured cases", a count over a class the header itself declares has no
    boundary.
    """
    return render_controls(URL_USERINFO.sub("***@", text), keep_lines=keep_lines)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in this process's working directory, never raising on a non-zero exit.

    Every call is prefixed with ``HOSTILE_CONFIG_OVERRIDES``, because ``--repo``
    points at a checkout whose own ``.git/config`` the operator has not read yet, and
    every variable in ``GIT_LOCATION_ENV_VARS`` is REMOVED from the child's
    environment, because those outrank the ``os.chdir`` that ``--repo`` does.

    ``GIT_ASKPASS=""`` IS THE OTHER HALF OF "NON-INTERACTIVE", and without it
    ``GIT_TERMINAL_PROMPT=0`` did not deliver the property this file credits it with.
    Git consults an askpass helper BEFORE the terminal prompt, so the audited
    checkout's own ``core.askPass`` runs even with prompting disabled and every
    override applied. Measured on git 2.53.0, ``--repo`` at a checkout carrying
    ``core.askPass=<script>`` in its own ``.git/config``, fetching an HTTPS origin
    that answers 401: the script ran TWICE during one ``git fetch origin`` (once for
    the username, once for the password), and with the helper replaced by
    ``sleep 600`` the fetch BLOCKED past 8s — the hang, not the failure, that this
    docstring's next paragraph says prompting-disabled prevents. With
    ``GIT_ASKPASS=""`` in the child environment the script ran ZERO times and the
    fetch failed in 0.04s with ``could not read Username ...: terminal prompts
    disabled`` — the message ``GIT_TERMINAL_PROMPT=0`` was set to produce, which the
    askpass helper had been intercepting. An EMPTY value is read as "no askpass"
    rather than exec'd, which is the
    opposite of ``core.sshCommand=`` (measured, git tries to run that one and reports
    ``cannot run :``), and it is why this is set here rather than declined the way
    that key is: measured, ``git fetch origin``, ``git status --porcelain`` and
    ``git reflog -5`` against an ordinary origin needing no credentials all still
    exit 0 with no extra output under it. It does not reach ssh — ssh reads
    ``SSH_ASKPASS``, not this — so an ssh key needing a passphrase is still bounded
    only by ``timeout=``.

    A timeout is reported as ``TIMEOUT_RETURNCODE``, with the message on ``stderr``,
    rather than raised — a ``TimeoutExpired`` escaping here would be a traceback
    instead of an exit code. It reaches the callers that READ the return code, which
    is every call this script decides on. The three informational calls do not decide
    the exit code — the exit inventory in the module docstring lists them nowhere, and
    this is why — but they do not swallow a failure either: each prints
    ``unavailable_marker``, which names the exit code and git's stderr on one line and
    changes nothing else. Those three lines are offered to a reader, not judged.
    """
    env = {name: value for name, value in os.environ.items() if name not in GIT_LOCATION_ENV_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    command = f"`git {' '.join(args)}`"
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            done = subprocess.run(
                ["git", *HOSTILE_CONFIG_OVERRIDES, *args],
                stdout=out,
                stderr=err,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                ["git", *args],
                TIMEOUT_RETURNCODE,
                "",
                f"{command} timed out after {SUBPROCESS_TIMEOUT_SECONDS}s.",
            )
        stdout = _read_capped(out, GIT_STDOUT_CAP, "GIT_STDOUT_CAP", command, "stdout")
        stderr = _read_capped(err, GIT_STDERR_CAP, "GIT_STDERR_CAP", command, "stderr")
    # No strict decode (E23-S91): a byte that is not UTF-8, in a reflog subject or
    # a path, raised `UnicodeDecodeError`. The printers render the surrogate.
    return subprocess.CompletedProcess(
        done.args,
        done.returncode,
        stdout.decode("utf-8", "surrogateescape"),
        stderr.decode("utf-8", "surrogateescape"),
    )


class GitOutputOverCap(Exception):
    """A git call printed more than its byte cap on one stream; ``main`` fails the run."""


def _read_capped(stream: IO[bytes], cap: int, label: str, command: str, name: str) -> bytes:
    """``stream`` from its start, refused above ``cap`` bytes."""
    stream.seek(0)
    data = stream.read(cap + 1)
    if len(data) > cap:
        raise GitOutputOverCap(f"{command} printed more than {label} ({cap} bytes) on {name}")
    return data


def unavailable_marker(done: subprocess.CompletedProcess[str]) -> str:
    """One line naming a failed informational call's exit code, or ``""`` if it worked.

    ⚠️ THE THREE CALLS NOBODY'S EXIT CODE IS READ FOR STILL HAVE TO SAY WHEN THEY
    FAILED, because two of them print NOTHING on success. A clean tree makes
    ``status --porcelain`` print zero bytes, so a ``status`` that failed or timed out
    printed its label and then nothing — byte-identical to a clean tree, in a tool
    whose entire product is letting an operator trust the tree. ``reflog -5`` has the
    same shape; the branch line has a weaker version of it, printing a bare
    ``branch: ``. This file shipped that ambiguity once with a test asserting it as
    intended behaviour.

    It does NOT set the run to fail. Making these three decide the exit code would put
    them in the header's exit inventory and would fail an arrival on a checkout whose
    reflog is merely unreadable — a different change, and not this one.

    ⚠️ ONE LINE IS MADE, NOT ASSUMED, and it was assumed once. A real ``fatal:`` is
    routinely preceded by ``error:`` lines, and the only stderr the suite exercised
    was the SINGLE-line message ``git()`` synthesises for a timeout — so "on one
    line" was written against the one case that could not falsify it. Measured on git
    2.53.0, in a checkout whose ``HEAD`` commit is a corrupt loose object: ``git
    status --porcelain`` and ``git reflog -5`` each exit 128 with SEVEN lines of
    stderr (three repeats of an ``error: inflate:`` / ``error: unable to unpack``
    pair, then the ``fatal: loose object ... is corrupt``).

    ⚠️ TWO THINGS NOW MAKE IT ONE LINE, AND EITHER WOULD DO. The join landed while
    ``keep_lines`` still defaulted to True, when unjoined stderr really did print as
    seven lines with the marker's ``)`` stranded on the last. The commit that made
    ``keep_lines`` opt-in made ``scrub`` escape a newline here too, so the property
    became doubly held and the justification standing two lines above it became
    false — it credited the join alone. Measured, each with the other left in
    place: removing the join leaves the marker one line, and passing
    ``keep_lines=True`` to the scrub leaves it one line as well.

    What only the join does is the other half of ``split()``: it collapses every
    run of whitespace, which the rendering does not, so a stderr padded with tabs
    and runs of spaces reads as a sentence rather than as a column. That is what
    the suite pins, and it is why "removing the join leaves the whole suite green"
    — true when it was written — is no longer true either.
    """
    if done.returncode == 0:
        return ""
    one_line = " ".join(done.stderr.split())
    return f"(unavailable: `git` exited {done.returncode} — {scrub(one_line)})\n"


def main_checkout_root() -> tuple[Path | None, subprocess.CompletedProcess[str]]:
    """The MAIN checkout's root — where the repo ``.venv`` and the chart live — or ``None``.

    Returns the git call BESIDE the answer, so the caller can report git's exit
    code and stderr on failure rather than a bare sentence. It used to return the
    path alone, and the caller printed neither.

    ``--git-common-dir`` is the main checkout's ``.git`` even from a linked
    worktree. It comes back relative to the CURRENT DIRECTORY otherwise — ``.git``
    at the root, ``../.git`` one level down — which ``resolve()`` anchors correctly
    because it too is relative to the current directory.

    Its exit code is checked because it prints NOTHING on failure, and
    ``Path("").resolve().parent`` is then the parent of the current directory: a
    plausible path, belonging to no checkout, that the ``.venv`` refusal and the
    chart lookup would both go on to use.

    ⚠️ THIS ``resolve()`` IS NOT GUARDED, unlike the ``.venv`` refusal's, which
    catches ``OSError`` and ``RuntimeError`` because a symlink loop raises the
    latter on 3.11. It is left that way because nobody has reached it: a review
    tried, pointing a planted ``commondir`` through a two-link cycle, and git
    itself exited 128 (``More than 32 nested symlinks``) so this line never saw
    the path. Written down rather than guarded, because a guard with no reachable
    input is a branch no test can honestly cover.
    """
    common = git("rev-parse", "--git-common-dir")
    if common.returncode != 0:
        return None, common
    # Only git's newline (E23-S91): `str.strip()` also removed a non-ASCII space
    # that ends the path, and a cut name can resolve somewhere else.
    return Path(common.stdout.removesuffix("\n")).resolve().parent, common


def same_directory(left: Path, right: Path) -> bool:
    """Whether two already-resolved paths name one directory, spelling aside.

    ``resolve()`` follows symlinks but PRESERVES the case it was handed, and this
    project's own filesystem is case-insensitive: measured on macOS APFS,
    ``<root>/.VENV`` and ``<root>/.venv`` are one directory with one inode, and
    comparing the two strings says they are not. So a ``sys.prefix`` of
    ``<root>/.VENV`` walked straight past the ``.venv`` refusal and ran against the
    editable install the refusal exists to block.

    ⚠️ ``os.path.normcase`` does NOT close that, though it is the obvious reach:
    measured, it is the identity function on this platform — it folds case on
    Windows only — so it would have left the defect exactly where it was while
    reading like a fix. The second arm asks the filesystem instead, which is right
    on a case-insensitive and a case-sensitive filesystem alike: on Linux
    ``<root>/.VENV`` really is a different directory, and ``samefile`` says so.
    It can only answer for paths that EXIST; a ``sys.prefix`` naming a directory
    that is not there stays a string comparison and nothing more.
    """
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def repo_venv_containing(prefix: Path, *roots: Path) -> Path | None:
    """The ``<root>/.venv`` that holds the already-resolved ``prefix``, or ``None``.

    The candidate is RESOLVED before comparing, like ``prefix``. A worktree carries
    no ``.venv``, so symlinking one in is the natural workaround, and comparing an
    unresolved candidate against a resolved prefix makes the resolve DEFEAT the
    match. ⚠️ ``same_directory``'s ``samefile`` arm rescues that for a symlink whose
    TARGET EXISTS — it asks the filesystem, which follows the link — so the resolve
    here is load-bearing only where ``samefile`` cannot answer: a DANGLING ``.venv``
    symlink, where ``samefile`` raises and only the resolved candidate still matches.
    That is the case the suite stages, and it is what keeps the unresolved candidate
    from surviving the file. The comparison is by whole path segment, so a sibling
    such as ``.venv-gate`` is not a match.

    ⚠️ THE RESOLVE IS THE ONE THAT RAISES, and under ``--repo`` the path it walks is
    the caller's. Measured on CPython 3.11.15, ``Path.resolve()`` on a symlink loop
    raises ``RuntimeError("Symlink loop from ...")`` — NOT an ``OSError``, so neither
    ``same_directory``'s handler nor anything else in this file saw it. Measured end
    to end against the earlier code, ``--repo`` at a checkout whose ``.venv`` is half
    of a two-link cycle exited on a raw traceback out of this line with NO arrival
    information printed at all, which is the failure the chart and git handlers exist
    to prevent, and the same 3.11 behaviour E31-S04 already hit here once. A root
    whose candidate cannot be resolved is SKIPPED rather than failed: this process's
    own ``sys.prefix`` is a directory it is running out of, so it cannot be inside a
    ``.venv`` the filesystem will not walk, and a root that cannot answer the question
    has no answer to give. ``OSError`` is named alongside ``RuntimeError`` because
    this is a filesystem call on a caller-named path and that is the pair
    ``same_directory`` already catches for the same reason.
    """
    for root in roots:
        try:
            candidate = (root / REPO_VENV_DIRNAME).resolve()
        except (OSError, RuntimeError):
            continue
        if same_directory(prefix, candidate) or any(
            same_directory(parent, candidate) for parent in prefix.parents
        ):
            return candidate
    return None


def check_chart(*roots: Path) -> bool:
    """Round-trip the wave chart and run the E23-S27 validator, if the chart is here.

    The chart is gitignored per-machine, so CI and a fresh clone never have one and
    both steps are skipped there — that is the common path, not an error.

    ⚠️ THE READ IS GUARDED AND BOUNDED, because under ``--repo`` the chart path is
    caller-influenced like everything else here. It is taken only after
    ``is_file()``, and inside the ``try``. Measured against the earlier code, which
    read an ``exists()`` path outside the ``try``: a non-UTF-8 chart exited on a raw
    ``UnicodeDecodeError`` traceback, a DIRECTORY at that path on an
    ``IsADirectoryError``, and a FIFO there blocked past 8s with no timeout — an
    unbounded hang in a file whose docstring calls boundedness a property. The
    ``except`` is widened to ``OSError`` for the read and ``ValueError`` for the
    decode and the parse; ``UnicodeDecodeError`` is a ``ValueError``.

    ⚠️ ``RecursionError`` IS NAMED IN THE ``except`` TOO, AND IT IS NEITHER ``OSError``
    NOR ``ValueError``. It is a
    ``RuntimeError`` — measured, ``issubclass(RecursionError, ValueError)`` and
    ``issubclass(RecursionError, OSError)`` are both False — which is the same
    argument ``MAX_CHART_BYTES`` records for ``MemoryError``, on an input a bound
    cannot catch. Measured against the two-exception ``except``: a 120000-byte chart
    of nested empty arrays, ~0.13x the real chart and far under any bound, exited on a
    raw ``RecursionError`` traceback out of ``json.loads``. Depth, not size, is what
    reaches it.

    ⚠️ ``MemoryError`` IS NAMED THERE AS WELL, and it is none of ``OSError``,
    ``ValueError`` or ``RecursionError``: measured,
    ``issubclass(MemoryError, OSError)``, ``(MemoryError, ValueError)`` and
    ``(MemoryError, RuntimeError)`` are all False — its MRO is ``Exception`` and
    nothing else. It is here because the SIZE bound does not reach it.
    ``MAX_CHART_BYTES`` bounds the BYTES READ, at a measured 2.00x, and the parse is a
    separate shape-dependent amplification measured between 3.6x and 26.8x at 64 MiB —
    so a chart under the bound can still exhaust memory, and both prescribed ways of
    bounding the parse were measured and declined at ``MAX_CHART_BYTES``. Catching it
    does not make the parse bounded. It makes the one failure a constrained machine
    actually gets a diagnosed FAIL line instead of the raw traceback this handler
    exists to prevent — which is all the earlier "the size is bounded too" sentence was
    ever entitled to claim. The size bound is checked before the read, from
    ``stat()``, so an oversize chart costs one syscall.
    """
    candidates: list[Path] = []
    for root in roots:
        candidate = root / CHART_FILENAME
        if candidate not in candidates:
            candidates.append(candidate)
    present = [path for path in candidates if path.is_file()]
    if not present:
        listed = " or ".join(render_controls(str(path)) for path in candidates)
        print(f"chart: no regular file at {listed} — round-trip and S27 validator skipped.")
        return True
    chart = present[0]

    try:
        size = chart.stat().st_size
        if size > MAX_CHART_BYTES:
            raise ValueError(
                f"{size} bytes is over the {MAX_CHART_BYTES}-byte bound "
                "(the real chart measured 923179 bytes on 2026-09-06, accreting "
                "~84 KB/day); refusing to read it"
            )
        raw = chart.read_text(encoding="utf-8")
        rewritten = json.dumps(json.loads(raw), indent=2) + "\n"
    except (OSError, ValueError, RecursionError, MemoryError) as exc:
        # `str(exc)` without `keep_lines`, measured EQUIVALENT with it (E23-S61
        # step 2): the exception texts this read raises hold no tab or newline.
        # `test_the_exception_texts_the_str_exc_renderers_print_hold_no_tab_or_newline`
        # in tests/e23/test_e23_s56_arrival.py samples them.
        print(
            f"FAIL: chart round-trip — {render_controls(str(chart))} could not be read: "
            f"{render_controls(str(exc))}"
        )
        return False
    if rewritten != raw:
        print(
            f"FAIL: chart round-trip — {render_controls(str(chart))} is not "
            "`json.dumps(..., indent=2, ensure_ascii=True)` + newline."
        )
        return False
    print("chart round-trip OK")

    validator = Path(__file__).resolve().parent / VALIDATOR_FILENAME
    try:
        audit = subprocess.run(
            [sys.executable, str(validator), str(chart)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"FAIL: the S27 validator timed out after {SUBPROCESS_TIMEOUT_SECONDS}s.")
        return False
    # EVERY VALIDATOR LINE IS PREFIXED, and that is a rule about the class rather
    # than a guard on one field. The validator is a separate program this script
    # does not own: it interpolates the chart PATH raw (`Audited N rows in
    # <path>.`), and a checkout reached through a symlink can be named with a
    # newline in it. Rendered with `keep_lines=True` — which these two streams
    # need, because they really are made of lines — that put an attacker's
    # sentence at column 0 as a line of its own, byte-identical to this script's
    # own output, on the DEFAULT path at exit 0 (measured). Escaping the newline
    # instead would mangle the validator's real multi-line report; fixing the
    # validator would fix one interpolation and not the next one anybody adds.
    #
    # With the prefix, NO line of that subprocess's output can be read as a line
    # of this report, whatever it prints and whatever it interpolates.
    for line in scrub(audit.stdout, keep_lines=True).splitlines():
        print(f"{VALIDATOR_LINE_PREFIX}{line}")
    for line in scrub(audit.stderr, keep_lines=True).splitlines():
        print(f"{VALIDATOR_LINE_PREFIX}{line}", file=sys.stderr)
    return audit.returncode == 0


def _run(args: argparse.Namespace) -> int:
    """The arrival checks themselves, in whatever directory the caller has entered."""
    where = git("rev-parse", "--show-toplevel")
    if where.returncode != 0:
        print(
            f"FAIL: `git rev-parse --show-toplevel` exited {where.returncode} — not a git "
            f"checkout, or git could not answer here: "
            f"{scrub(where.stderr.strip())}"
        )
        return 1
    toplevel = Path(where.stdout.removesuffix("\n")).resolve()
    main_root, common = main_checkout_root()
    if main_root is None:
        # git's exit code and stderr, like every other decisive failure. This
        # branch used to print neither, and it is the one call whose ANSWER the
        # audited checkout can steer with a planted `commondir` — so an operator
        # whose `--repo` target is tampered with or merely broken got one bare
        # sentence with nothing to tell the two apart.
        print(
            f"FAIL: could not locate the main checkout — `git rev-parse --git-common-dir` "
            f"exited {common.returncode}: {scrub(common.stderr.strip())}"
        )
        return 1

    prefix = Path(sys.prefix).resolve()
    refused = repo_venv_containing(prefix, toplevel, main_root)
    if refused is not None:
        # `same_directory`, not `==`, for the reason `same_directory` itself exists:
        # measured on this project's macOS APFS checkout, `<root>/.VENV` and
        # `<root>/.venv` are ONE directory with one inode that a string comparison
        # calls different. `==` therefore told an operator whose `sys.prefix` was
        # `<root>/.VENV` that it was "inside" a directory it IS, which reads as a bug
        # in the check to the person it stops. The two sibling tests below keep the
        # two wordings apart.
        if same_directory(prefix, refused):
            print(f"REFUSING: sys.prefix IS the repo venv {render_controls(str(refused))}.")
        else:
            print(
                f"REFUSING: sys.prefix {render_controls(str(prefix))} is inside the repo venv "
                f"{render_controls(str(refused))}."
            )
        print("Re-run from a CI-shaped venv built outside it; this one is an editable install.")
        return 1

    ok = True

    fetch = git("fetch", "origin")
    if fetch.returncode == 0:
        print("git fetch origin: exit 0")
    else:
        print(f"FAIL: git fetch origin exited {fetch.returncode}: {scrub(fetch.stderr.strip())}")
        ok = False

    # `render_controls`, not `scrub`: see `scrub`'s own docstring on why a PATH must
    # not go through the userinfo half.
    print(f"toplevel: {render_controls(str(toplevel))}")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    # Only git's newline: a branch name can end in a non-ASCII space.
    branch_name = branch.stdout.removesuffix("\n")
    print(f"branch: {scrub(branch_name)}")
    print(unavailable_marker(branch), end="")
    ahead = git("rev-list", "--count", "origin/HEAD..HEAD")
    behind = git("rev-list", "--count", "HEAD..origin/HEAD")
    if ahead.returncode == 0 and behind.returncode == 0:
        print(f"ahead of origin/HEAD (origin/HEAD..HEAD): {scrub(ahead.stdout.strip())}")
        print(f"behind origin/HEAD (HEAD..origin/HEAD): {scrub(behind.stdout.strip())}")
    else:
        # The exit code and git's own stderr, NOT a diagnosis. This line used to
        # read "no such ref in this checkout", which is one of several causes and
        # the only one it cannot tell apart from the others: a timeout (`git()`
        # synthesises 124), a corrupt object and an unborn HEAD all arrive here
        # and were all reported as a missing ref.
        #
        # ⚠️ AND IT IS NOT THE LAST BRANCH OF THIS SHAPE. The correction here
        # first read "every other FAIL branch prints what git said; this one is
        # the only one that guessed", which is false twice over: the
        # `main_checkout_root` failure below prints neither an exit code nor
        # stderr, and `--expect`'s `cat-file -e` still names a cause — a timeout
        # there reports "is not a commit in this checkout". Both now report what
        # git said, and stating the sweep as "every other" is what would have let
        # the next one stand.
        failed = ahead if ahead.returncode != 0 else behind
        print(
            f"FAIL: could not count against origin/HEAD — `git rev-list --count` "
            f"exited {failed.returncode}: {scrub(failed.stderr.strip())}"
        )
        ok = False

    print("git status --porcelain:")
    status = git("status", "--porcelain", "--untracked-files=normal")
    print(scrub(status.stdout, keep_lines=True), end="")
    print(unavailable_marker(status), end="")
    print("git reflog -5:")
    reflog = git("reflog", "-5")
    print(scrub(reflog.stdout, keep_lines=True), end="")
    print(unavailable_marker(reflog), end="")

    if not args.skip_chart:
        ok = check_chart(main_root, toplevel) and ok

    if args.expect is not None:
        # Rendered at the printers, never where it enters: the value handed to `git`
        # below has to stay the one the operator typed.
        expect = render_controls(args.expect)
        # Bound, so this branch can report what git said. Naming the cause here
        # is the same defect the `rev-list` line above was corrected for: a
        # `cat-file -e` that times out (`git()` synthesises 124) or is killed is
        # not a missing commit, and reporting it as one is a diagnosis this
        # branch never made.
        commit = git("cat-file", "-e", f"{args.expect}^{{commit}}")
        if commit.returncode != 0:
            print(
                f"FAIL: --expect {expect}: `git cat-file -e` exited "
                f"{commit.returncode}: {scrub(commit.stderr.strip())}"
            )
            ok = False
        else:
            # `--is-ancestor` answers with 0/1 and reserves everything else for a
            # question it could not ask — 128 when `origin/HEAD` is not a ref here.
            # NOT folded into the 1 branch that reads as "the branch is behind": the
            # `else` below is a separate branch with its own message, and
            # `test_expect_says_so_when_origin_head_cannot_be_resolved_at_all` fails
            # if the two are merged.
            ancestry = git("merge-base", "--is-ancestor", args.expect, "origin/HEAD")
            if ancestry.returncode == 0:
                print(f"origin/HEAD is {expect} or a descendant of it.")
            elif ancestry.returncode == 1:
                print(f"FAIL: origin/HEAD is not {expect} nor a descendant of it.")
                ok = False
            else:
                print(
                    f"FAIL: --expect {expect}: could not compare against origin/HEAD "
                    f"— `git merge-base --is-ancestor` exited {ancestry.returncode}."
                )
                ok = False

    return 0 if ok else 1


def _run_capped(args: argparse.Namespace) -> int:
    """``_run``, with an over-cap git read reported as a failed run (E23-S91)."""
    try:
        return _run(args)
    except GitOutputOverCap as exc:
        print(f"FAIL: {render_controls(str(exc))}")
        return 1


class _RenderedParser(argparse.ArgumentParser):
    """``argparse`` with its error message rendered.

    ``argparse`` echoes an argument into that message as typed, so it reached
    stderr raw. The message is a string this script did not write, so it goes
    through ``render_controls``.
    """

    def error(self, message: str) -> NoReturn:
        super().error(render_controls(message))


def main(argv: list[str] | None = None) -> int:
    parser = _RenderedParser(description="Run the arrival ritual for this checkout.")
    parser.add_argument(
        "--repo",
        metavar="DIR",
        help=(
            "run the checks in DIR instead of the current directory — git runs "
            "under DIR's own config, which can execute code it names and can hand "
            "over your stored git credentials, with --skip-chart and no network "
            "no protection; point this only at checkouts you trust"
        ),
    )
    parser.add_argument(
        "--expect",
        metavar="SHA",
        help="fail unless origin/HEAD is this commit or a descendant of it",
    )
    parser.add_argument(
        "--skip-chart",
        action="store_true",
        help="do not round-trip the wave chart or run the E23-S27 validator",
    )
    args = parser.parse_args(argv)

    # `merge-base --is-ancestor` and `cat-file -e` both take this value positionally
    # with no `--` separator, so a leading dash would be read as an option rather
    # than as a revision. Rejected here instead of relying on which options those two
    # commands happen not to have.
    if args.expect is not None and args.expect.startswith("-"):
        print(f"FAIL: --expect {render_controls(args.expect)}: a revision may not start with '-'.")
        return 1

    if args.repo is None:
        return _run_capped(args)

    # Restored in `finally` so a caller that runs this in-process — the suite does —
    # is not left in another directory by a run that failed anywhere below.
    original = os.getcwd()
    try:
        os.chdir(args.repo)
    except OSError as exc:
        # `str(exc)` without `keep_lines`, measured EQUIVALENT with it (E23-S61
        # step 2): `os.chdir`'s `OSError` text prints the path with `repr`. The
        # test named at `check_chart`'s `except` clause samples it.
        print(
            f"FAIL: --repo {render_controls(args.repo)}: cannot enter that directory "
            f"— {render_controls(str(exc))}."
        )
        return 1
    try:
        return _run_capped(args)
    finally:
        os.chdir(original)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
