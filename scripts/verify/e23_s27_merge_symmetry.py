#!/usr/bin/env python3
"""E23-S27 — ``mergeSerializedWith`` symmetry validator for ``open_stories.json``.

``mergeSerializedWith`` answers one question: *what can this story NOT share a
batch with?* It is consumed by reading ONE row. But it is hand-maintained per
story at filing time, and a filer naturally records "what already exists" —
nothing updates the EARLIER row to point back at the newcomer. So the graph
accretes edges pointing backwards in time only, the newest story always has the
most complete view, and the oldest has the least. An audit on 2026-09-03 found
83 asymmetric ordered pairs across 135 rows, 22 of them involving an open row.

Symmetrising the data fixes today's rows and nothing else: the field re-accretes
one-directional edges immediately. Observed live on 2026-09-03, a session added
E22-S8's row carrying ``mergeSerializedWith: ['E22-S9']`` against a story that
had no row at all, and it took a SECOND row addition to close the edge.

⚠️ WHAT THIS DOES AND DOES NOT GUARANTEE. This is a MANUAL harness, like every
sibling in ``scripts/verify/``. **Nothing runs it automatically, and nothing can:**
the chart is gitignored per-machine (``.git/info/exclude``), so CI has no
``open_stories.json`` to check and a CI step would be a permanent no-op. It
therefore does not *prevent* a one-directional edge from being committed; it
detects one on demand, for whoever runs it. Its cadence and preconditions are
recorded in ``docs/TESTING_STRATEGY.md`` (Matrix 3) — run it after editing the
chart and before packing a batch.

THE FAILURE RULE, AND WHY IT IS SPLIT
-------------------------------------
The E23-S27 repair is scoped to OPEN rows by owner decision (2026-09-03): the
merged/superseded asymmetries are historical and operationally inert, and
repairing all 83 would churn 100+ rows nobody reads for scheduling. A validator
that FAILED on all 83 would therefore be red on a tree the owner deliberately
accepted — and a permanently red gate gets silenced or deleted, which is how
this defect class survives. So:

  FAIL (exit 1)  an asymmetry UNLESS BOTH ends are merged/superseded.
  FAIL (exit 1)  an edge naming a row that does not exist in the chart.
  FAIL (exit 1)  a row naming ITSELF (meaningless as a serialisation partner,
                 and trivially "symmetric" under a naive back-reference check).
  REPORT only    an asymmetry where BOTH ends are historical. Printed so the
                 remainder is visible as a stated decision rather than an
                 omission, but it does not fail the run.

⚠️ THE BLOCKING RULE IS PHRASED AS "UNLESS BOTH ARE HISTORICAL", NOT "IF EITHER
IS LIVE", SO THAT IT FAILS CLOSED. An earlier cut tested ``status in {"open",
"in-flight"}``, which silently downgraded to report-only any row whose status was
missing, typo'd (``"Open"``), or simply new (a ``"blocked"`` or ``"parked"`` added
later) — the validator hiding the very class of defect it exists to catch. An
unrecognised status now BLOCKS and shows the offending value in the message.

"Both ends" is deliberate, not shorthand for "the namer". The live
``E29-S72 (merged) -> E29-S65 (open)`` pair has a MERGED namer and an OPEN silent
end, and the open row is the one gaining the information — a rule that only
looked at the namer's status would miss it. ``in-flight`` counts as live: such a
story is still being packed around, and its row is the one a planner reads.

WHY UNKNOWN TARGETS AND SELF-EDGES ARE SEPARATE CATEGORIES rather than
"asymmetries": neither can be repaired by editing the named row. An unknown
target needs a different fix (file the missing story, or drop the typo), and a
self-edge is meaningless data; folding either into the asymmetry list would send
a reader looking for a row to edit that does not exist, or for a partner that is
the row itself.

MALFORMED CHARTS EXIT 2, NEVER 1. The chart is hand-edited by several concurrent
sessions, so shape damage is a live risk and must not be reported as "violations
found". A root that is not an object, a missing/non-list ``stories``, a row that
is not an object or lacks a string ``id``, a DUPLICATE ``id`` (the index would
otherwise be last-writer-wins, silently shadowing a row's edges), or a
``mergeSerializedWith`` that is not a list of strings (a bare string would
iterate per CHARACTER, fabricating one bogus violation per letter) all raise
``ChartShapeError`` and exit 2.

Not collected by pytest (``[tool.pytest.ini_options] testpaths = ["tests"]``);
not measured by coverage (``[tool.coverage.run] source = ["agent"]``), hence NO
``omit`` entry — verified empirically, the coverage report carries zero
``scripts/`` lines. A needless omit would be a trap for whoever later moves this
file into ``agent/``. Because coverage is blind here, the behaviour is proved by
MUTATION instead; the unit tests live in
``tests/e23/test_e23_s27_merge_serialization_symmetry.py`` and build their own
fixtures rather than reading the real chart (a test that read it would pass on a
developer machine and crash or vacuously skip in CI).

Run:  python scripts/verify/e23_s27_merge_symmetry.py [path/to/open_stories.json]
      With no argument it resolves the chart at the MAIN checkout root even when
      run from a git worktree — the chart exists only there, and worktrees are
      where story work happens, so a worktree-relative default would silently
      report "chart not present" and exit 0 in the common case.
Exit codes:
  0   chart absent, or audited clean of blocking violations
  1   blocking violations found
  2   the chart could not be read, parsed, or is structurally malformed
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

CHART_FILENAME = "open_stories.json"

# A story is "live" for scheduling purposes in these states, and an asymmetry
# touching one can still mislead a batch planner.
LIVE_STATUSES = frozenset({"open", "in-flight"})

# The complete historical vocabulary. An asymmetry is downgraded to report-only
# ONLY when BOTH ends are in here, so anything unrecognised blocks (see the
# fail-closed note in the module docstring).
HISTORICAL_STATUSES = frozenset({"merged", "superseded", "closed-superseded"})


class ChartShapeError(ValueError):
    """The chart parsed as JSON but is not a usable wave chart."""


class Asymmetry(NamedTuple):
    """``namer`` lists ``silent`` in mergeSerializedWith; ``silent`` is silent back."""

    namer: str
    silent: str
    namer_status: str
    silent_status: str

    def is_blocking(self) -> bool:
        """Blocking UNLESS both ends are historical — an unrecognised or missing
        status blocks rather than passing. See the module docstring."""
        return not (
            self.namer_status in HISTORICAL_STATUSES and self.silent_status in HISTORICAL_STATUSES
        )

    def describe(self) -> str:
        return (
            f"{_safe(self.namer)} ({_safe(self.namer_status)}) names "
            f"{_safe(self.silent)} ({_safe(self.silent_status)}); "
            f"{_safe(self.silent)} does not name it back"
        )


@dataclass
class AuditResult:
    row_count: int = 0
    blocking: list[Asymmetry] = field(default_factory=list)
    reported: list[Asymmetry] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)
    self_edges: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.blocking or self.unknown or self.self_edges)


def _safe(value: object) -> str:
    """Render a chart-supplied value for a terminal.

    Ids and statuses are printed straight into an operator's terminal and into
    CI logs. The chart is hand-edited by several sessions and agents, so a value
    carrying ANSI control sequences could clear the screen or forge/hide
    violation lines in the output a human is reading to decide whether the gate
    passed. Non-printable characters are escaped and the field is truncated.
    """
    text = "".join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))
    return text if len(text) <= 120 else text[:117] + "..."


def _edges(row: dict[str, Any]) -> list[str]:
    """This row's edge list, de-duplicated, order preserved.

    A row with no ``mergeSerializedWith`` (or an explicit null) has no edges:
    rows predating the field must not crash the audit, and such a row is still a
    valid SILENT end — that is exactly the live E23-S30 shape.

    A bare string is rejected rather than iterated: ``"E23-S30"`` would otherwise
    yield seven single-character targets and seven fabricated violations.
    Duplicates within one list are collapsed so a hand-editing slip inflates
    neither the printed output nor the violation count.
    """
    raw = row.get("mergeSerializedWith") or []
    if not isinstance(raw, list):
        raise ChartShapeError(
            f"row {_safe(row.get('id'))}: mergeSerializedWith must be a list, "
            f"got {type(raw).__name__}"
        )
    for target in raw:
        if not isinstance(target, str):
            raise ChartShapeError(
                f"row {_safe(row.get('id'))}: mergeSerializedWith entries must "
                f"be strings, got {type(target).__name__}"
            )
    return list(dict.fromkeys(raw))


def index_stories(stories: Any) -> dict[str, dict[str, Any]]:
    """Validate the row list and index it by id.

    Duplicate ids are rejected rather than resolved last-writer-wins: silently
    shadowing a row makes the earlier row's edges get checked against the later
    row's list, which can both hide a genuine violation and manufacture a false
    one.
    """
    if not isinstance(stories, list):
        raise ChartShapeError(f"chart 'stories' must be a list, got {type(stories).__name__}")
    by_id: dict[str, dict[str, Any]] = {}
    for row in stories:
        if not isinstance(row, dict):
            raise ChartShapeError(f"story rows must be objects, got {type(row).__name__}")
        story_id = row.get("id")
        if not isinstance(story_id, str) or not story_id:
            raise ChartShapeError(f"story row has no string 'id': {_safe(story_id)}")
        if story_id in by_id:
            raise ChartShapeError(f"duplicate story id {_safe(story_id)}")
        by_id[story_id] = row
    return by_id


def _status(row: dict[str, Any]) -> str:
    value = row.get("status")
    return value if isinstance(value, str) else ""


def find_asymmetries(stories: list[dict[str, Any]]) -> list[Asymmetry]:
    """Every ordered pair where A names B and B does not name A back.

    Self-edges and edges naming a row absent from the chart are skipped here;
    they are ``find_self_edges``' and ``find_unknown_targets``' business.
    """
    by_id = index_stories(stories)
    found: list[Asymmetry] = []
    for row in stories:
        for target in _edges(row):
            if target == row["id"] or target not in by_id:
                continue
            if row["id"] not in _edges(by_id[target]):
                found.append(
                    Asymmetry(
                        namer=row["id"],
                        silent=target,
                        namer_status=_status(row),
                        silent_status=_status(by_id[target]),
                    )
                )
    return found


def find_unknown_targets(stories: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Every ``(namer, target)`` where ``target`` has no row in the chart."""
    known = set(index_stories(stories))
    return [(row["id"], target) for row in stories for target in _edges(row) if target not in known]


def find_self_edges(stories: list[dict[str, Any]]) -> list[str]:
    """Every row that names itself — meaningless as a serialisation partner, and
    trivially "symmetric" under a naive back-reference check, so it would
    otherwise pass clean."""
    index_stories(stories)
    return [row["id"] for row in stories if row["id"] in _edges(row)]


def audit_chart(chart: Any) -> AuditResult:
    if not isinstance(chart, dict):
        raise ChartShapeError(f"chart root must be an object, got {type(chart).__name__}")
    if "stories" not in chart or chart["stories"] is None:
        raise ChartShapeError("chart has no 'stories' list")
    stories = chart["stories"]
    index_stories(stories)

    result = AuditResult(row_count=len(stories))
    for asymmetry in find_asymmetries(stories):
        if asymmetry.is_blocking():
            result.blocking.append(asymmetry)
        else:
            result.reported.append(asymmetry)
    result.unknown = find_unknown_targets(stories)
    result.self_edges = find_self_edges(stories)
    return result


def chart_from_common_dir(common_dir: str, anchor: Path) -> Path:
    """Turn ``git rev-parse --git-common-dir`` output into the chart path.

    ``--git-common-dir`` is the MAIN checkout's ``.git`` even when run inside a
    linked worktree, so its parent is the main checkout root — which is the only
    place the chart exists. It comes back relative (``.git``) when run from the
    main checkout itself, so it is anchored before resolving.
    """
    path = Path(common_dir)
    if not path.is_absolute():
        path = anchor / path
    return path.resolve().parent / CHART_FILENAME


def default_chart_path() -> Path:  # pragma: no cover - thin subprocess shim
    anchor = Path(__file__).resolve().parent
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=anchor,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return Path(__file__).resolve().parents[2] / CHART_FILENAME
    return chart_from_common_dir(common, anchor)


def main(chart_path: Path | None = None) -> int:
    path = default_chart_path() if chart_path is None else chart_path
    if not path.exists():
        # A checkout without the chart: it is gitignored per-machine, so CI and
        # a fresh clone never have one. Exiting non-zero would make every such
        # run red for a reason the operator cannot fix.
        print(f"SKIP: chart not present at {path} — nothing to validate.")
        return 0

    try:
        chart = json.loads(path.read_text(encoding="utf-8"))
        result = audit_chart(chart)
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError is not a ValueError: json.loads raises it on deeply
        # nested input, and it must exit 2 like any other unreadable chart
        # rather than escaping as a traceback.
        print(f"ERROR: could not read {path}: {type(exc).__name__}: {exc}")
        return 2

    print(f"Audited {result.row_count} rows in {path}.")

    if result.reported:
        print(
            f"\nREPORT ONLY — {len(result.reported)} asymmetric pair(s) between "
            "historical rows (both ends merged/superseded). Left unrepaired by "
            "the E23-S27 owner decision of 2026-09-03; recorded, not failed:"
        )
        for asymmetry in result.reported:
            print(f"  - {asymmetry.describe()}")

    if result.unknown:
        print(f"\nFAIL — {len(result.unknown)} edge(s) naming a nonexistent row:")
        for namer, target in result.unknown:
            print(f"  - {_safe(namer)} names {_safe(target)}, which has no row in the chart")

    if result.self_edges:
        print(f"\nFAIL — {len(result.self_edges)} row(s) naming themselves:")
        for story_id in result.self_edges:
            print(f"  - {_safe(story_id)} lists itself in mergeSerializedWith")

    if result.blocking:
        print(
            f"\nFAIL — {len(result.blocking)} asymmetric pair(s) that are not "
            "purely historical. A batch planner reading the silent row learns "
            "nothing about this serialisation:"
        )
        for asymmetry in result.blocking:
            print(f"  - {asymmetry.describe()}")

    if result.ok:
        print("\nOK: no blocking mergeSerializedWith violations.")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
