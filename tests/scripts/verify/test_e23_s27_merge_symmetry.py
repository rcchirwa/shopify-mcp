"""E23-S27: the ``mergeSerializedWith`` symmetry validator.

The defect, the fail-closed blocking rule, the report-only split and the
gitignored-chart consequences are all stated once in the module docstring of
``scripts/verify/e23_s27_merge_symmetry.py``; they are not restated here, so the
two files cannot drift apart.

Two things specific to these tests:

⚠️ **No test may read the real chart.** It is gitignored per-machine, so a test
that read it would pass on a developer machine and crash or vacuously pass in
CI — the exact failure this story exists to stop. Every test below builds its own
fixture dict.

Ported from Holocron, where coverage did not measure ``scripts/``. In shopify-mcp,
``[tool.coverage.run] source`` includes ``scripts``, so the validator is under the 100%
gate. Each test is still written to fail for a single reason. Several assertions below are deliberately narrower than they look
comfortable being: ``test_main_reports_the_row_count`` pins the literal
``"Audited 2 rows"`` rather than ``"2"``, because ``"2"`` also matches a digit in
the pytest ``tmp_path`` echoed into the same output and passes even when the
count is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify.e23_s27_merge_symmetry import (
    Asymmetry,
    ChartShapeError,
    audit_chart,
    chart_from_common_dir,
    find_asymmetries,
    find_self_edges,
    find_unknown_targets,
    main,
)


def _chart(rows: list[dict[str, object]]) -> dict[str, object]:
    """A minimal chart document — only the keys the validator reads."""
    return {"generated": "2026-09-03", "stories": rows}


def _row(story_id: str, status: str, serialized: list[str] | None = None) -> dict[str, object]:
    return {
        "id": story_id,
        "status": status,
        "mergeSerializedWith": [] if serialized is None else serialized,
    }


def _write(tmp_path: Path, payload: object) -> Path:
    chart = tmp_path / "open_stories.json"
    chart.write_text(json.dumps(payload), encoding="utf-8")
    return chart


# --------------------------------------------------------------------------
# find_asymmetries — the core detector
# --------------------------------------------------------------------------


def test_detects_one_directional_edge() -> None:
    """A names B; B is silent about A. That is the whole defect."""
    rows = [_row("E29-S65", "open", ["E29-S62"]), _row("E29-S62", "merged")]
    assert find_asymmetries(rows) == [
        Asymmetry(
            namer="E29-S65",
            silent="E29-S62",
            namer_status="open",
            silent_status="merged",
        )
    ]


def test_symmetric_pair_is_not_flagged() -> None:
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    assert find_asymmetries(rows) == []


def test_missing_field_is_treated_as_no_edges() -> None:
    """Rows predating the field must not crash the audit, and a row with no
    field is still a valid SILENT end — that is exactly the E23-S30 shape."""
    rows = [{"id": "E23-S30", "status": "open"}, _row("E23-S43", "open", ["E23-S30"])]
    assert find_asymmetries(rows) == [
        Asymmetry(
            namer="E23-S43",
            silent="E23-S30",
            namer_status="open",
            silent_status="open",
        )
    ]


def test_explicit_null_field_is_treated_as_no_edges() -> None:
    rows = [
        {"id": "E23-S30", "status": "open", "mergeSerializedWith": None},
        _row("E23-S43", "open", ["E23-S30"]),
    ]
    assert [a.silent for a in find_asymmetries(rows)] == ["E23-S30"]


def test_duplicate_entries_in_one_list_are_collapsed() -> None:
    """A hand-editing slip must not inflate the violation count."""
    rows = [_row("E23-S43", "open", ["E23-S30", "E23-S30"]), _row("E23-S30", "open")]
    assert len(find_asymmetries(rows)) == 1


def test_edge_to_unknown_row_is_not_reported_as_asymmetry() -> None:
    """An edge naming a nonexistent row is a DIFFERENT defect with a different
    fix (file the missing row, or drop the typo). Counting it as an asymmetry
    would send a reader looking for a row to edit that does not exist."""
    rows = [_row("E22-S8", "open", ["E22-S9"])]
    assert find_asymmetries(rows) == []
    assert find_unknown_targets(rows) == [("E22-S8", "E22-S9")]


def test_unknown_targets_empty_when_every_edge_resolves() -> None:
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    assert find_unknown_targets(rows) == []


# --------------------------------------------------------------------------
# self-edges — trivially "symmetric", so they must be caught separately
# --------------------------------------------------------------------------


def test_self_edge_is_reported_and_not_counted_as_symmetric() -> None:
    rows = [_row("E23-S30", "open", ["E23-S30"])]
    assert find_self_edges(rows) == ["E23-S30"]
    assert find_asymmetries(rows) == []
    assert audit_chart(_chart(rows)).ok is False


def test_no_self_edges_in_a_normal_chart() -> None:
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    assert find_self_edges(rows) == []


# --------------------------------------------------------------------------
# the blocking / report-only split — and that it fails CLOSED
# --------------------------------------------------------------------------


def test_open_end_makes_an_asymmetry_blocking() -> None:
    rows = [_row("E29-S65", "open", ["E29-S62"]), _row("E29-S62", "merged")]
    result = audit_chart(_chart(rows))
    assert [a.silent for a in result.blocking] == ["E29-S62"]
    assert result.reported == []


def test_in_flight_end_makes_an_asymmetry_blocking() -> None:
    """``in-flight`` counts as live: the row is still being packed around, and
    it is the one a planner reads."""
    rows = [_row("E29-S88", "in-flight", ["E29-S62"]), _row("E29-S62", "merged")]
    assert [a.silent for a in audit_chart(_chart(rows)).blocking] == ["E29-S62"]


def test_blocking_when_only_the_SILENT_end_is_live() -> None:
    """Here the NAMER is merged and the silent end is open — the open row is the
    one gaining the information, so a rule that only looked at the namer's status
    would miss it. This is the live ``E29-S72 (merged) -> E29-S65 (open)`` shape."""
    rows = [_row("E29-S72", "merged", ["E29-S65"]), _row("E29-S65", "open")]
    result = audit_chart(_chart(rows))
    assert [a.silent for a in result.blocking] == ["E29-S65"]
    assert result.reported == []


@pytest.mark.parametrize("status", ["Open", "in flight", "blocked", "parked", ""])
def test_unrecognised_status_BLOCKS_rather_than_passing(status: str) -> None:
    """The fail-closed rule. A typo'd, renamed, or newly-invented status must not
    be silently downgraded to report-only — that would be the validator hiding
    the very class of defect it exists to catch."""
    rows = [_row("A", status, ["B"]), _row("B", "merged")]
    result = audit_chart(_chart(rows))
    assert [a.silent for a in result.blocking] == ["B"]
    assert result.reported == []


def test_missing_status_key_BLOCKS() -> None:
    rows = [{"id": "A", "mergeSerializedWith": ["B"]}, _row("B", "merged")]
    result = audit_chart(_chart(rows))
    assert [a.silent for a in result.blocking] == ["B"]


def test_reports_but_does_not_fail_on_merged_only_asymmetry() -> None:
    """Both ends historical: recorded, but not a failure. The repair is
    open-rows-only by owner decision, so failing here would be red on a tree
    that was deliberately accepted."""
    rows = [_row("E23-S21", "merged", ["E29-S62"]), _row("E29-S62", "merged")]
    result = audit_chart(_chart(rows))
    assert result.blocking == []
    assert [a.silent for a in result.reported] == ["E29-S62"]
    assert result.ok is True


@pytest.mark.parametrize("status", ["merged", "superseded", "closed-superseded"])
def test_every_historical_status_is_report_only(status: str) -> None:
    rows = [_row("A", status, ["B"]), _row("B", "superseded")]
    result = audit_chart(_chart(rows))
    assert result.blocking == []
    assert [a.silent for a in result.reported] == ["B"]


def test_unknown_target_is_blocking() -> None:
    """Currently zero chart-wide, but E22-S8 carried one for minutes on
    2026-09-03 — that is how E22-S9's missing row was discovered."""
    result = audit_chart(_chart([_row("E22-S8", "open", ["E22-S9"])]))
    assert result.unknown == [("E22-S8", "E22-S9")]
    assert result.ok is False


def test_clean_chart_is_ok() -> None:
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    result = audit_chart(_chart(rows))
    assert result.blocking == []
    assert result.unknown == []
    assert result.self_edges == []
    assert result.ok is True


def test_result_counts_every_row_it_audited() -> None:
    """Guards the vacuous pass: a validator that audited nothing also reports
    no violations, so the row count is part of the result."""
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    assert audit_chart(_chart(rows)).row_count == 2


def test_describe_names_both_statuses_and_the_direction() -> None:
    """``describe`` is the entire explanatory payload of a failure message. The
    ids alone are not enough: without the statuses a reader cannot tell why the
    pair blocked, and without the direction they cannot tell which row to edit."""
    text = Asymmetry("E29-S72", "E29-S65", "merged", "open").describe()
    assert text == ("E29-S72 (merged) names E29-S65 (open); E29-S65 does not name it back")


def test_describe_escapes_terminal_control_sequences() -> None:
    """Chart values are printed straight into an operator's terminal, and the
    chart is hand-edited by several sessions."""
    text = Asymmetry("A\x1b[2J", "B", "open", "merged").describe()
    assert "\x1b" not in text


# --------------------------------------------------------------------------
# malformed charts — exit 2, never 1
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="root-is-a-list"),
        # A list CONTAINING the string "stories" is the case that proves the
        # root-type guard is load-bearing rather than redundant: `"stories" not
        # in chart` is False for it, so without the guard it reaches
        # `chart["stories"]` and dies with an uncaught TypeError instead of
        # exiting 2. Found by mutation — the other root cases all survive the
        # guard's removal because the membership test happens to catch them.
        pytest.param(["stories"], id="root-is-a-list-containing-stories"),
        pytest.param("nope", id="root-is-a-string"),
        pytest.param({"generated": "x"}, id="no-stories-key"),
        pytest.param({"stories": None}, id="stories-is-null"),
        pytest.param({"stories": {"a": 1}}, id="stories-is-an-object"),
        pytest.param({"stories": ["not-a-row"]}, id="row-is-not-an-object"),
        pytest.param({"stories": [{"status": "open"}]}, id="row-has-no-id"),
        pytest.param({"stories": [{"id": 7, "status": "open"}]}, id="id-not-a-string"),
        pytest.param(
            {"stories": [{"id": "A", "status": "open", "mergeSerializedWith": "B-1"}]},
            id="edges-are-a-bare-string",
        ),
        pytest.param(
            {"stories": [{"id": "A", "status": "open", "mergeSerializedWith": [7]}]},
            id="edge-is-not-a-string",
        ),
        pytest.param(
            {"stories": [{"id": "A", "status": "open"}, {"id": "A", "status": "merged"}]},
            id="duplicate-story-id",
        ),
    ],
)
def test_malformed_chart_raises_a_shape_error(payload: object) -> None:
    with pytest.raises(ChartShapeError):
        audit_chart(payload)


def test_bare_string_edge_list_is_not_iterated_per_character() -> None:
    """``"B-1"`` would otherwise yield three single-character targets and three
    fabricated unknown-target violations."""
    with pytest.raises(ChartShapeError, match="must be a list"):
        audit_chart(_chart([{"id": "A", "status": "open", "mergeSerializedWith": "B-1"}]))


def test_duplicate_id_is_rejected_rather_than_shadowing_a_row() -> None:
    """Last-writer-wins indexing would check the first row's edges against the
    second row's list — hiding a real violation and inventing a false one."""
    with pytest.raises(ChartShapeError, match="duplicate story id"):
        audit_chart(_chart([_row("A", "open", ["B"]), _row("A", "merged"), _row("B", "open")]))


# --------------------------------------------------------------------------
# chart_from_common_dir — the worktree-aware default
# --------------------------------------------------------------------------


def test_common_dir_absolute_resolves_to_the_main_checkout_root() -> None:
    """From a worktree, ``--git-common-dir`` is the MAIN checkout's ``.git``, so
    the chart resolves there and not into the worktree (where it never exists)."""
    assert chart_from_common_dir("/repos/Holocron/.git", Path("/repos/worktrees/story")) == Path(
        "/repos/Holocron/open_stories.json"
    )


def test_common_dir_relative_is_anchored_to_the_script_not_the_cwd() -> None:
    """Outside a worktree git answers RELATIVE TO THE DIRECTORY IT RAN IN, and
    the caller runs it in the script's own directory — so from the main checkout
    it returns ``../../.git`` (verified), which must be anchored there rather
    than interpreted against whatever cwd the operator happened to invoke from."""
    assert chart_from_common_dir("../../.git", Path("/repos/Holocron/scripts/verify")) == Path(
        "/repos/Holocron/open_stories.json"
    )


# --------------------------------------------------------------------------
# main() — exit codes and the chart-absent path
# --------------------------------------------------------------------------


def test_main_exits_zero_and_says_so_when_chart_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A checkout without the chart (it is gitignored per-machine) must not be
    red for a reason the operator cannot fix."""
    exit_code = main(tmp_path / "open_stories.json")
    assert exit_code == 0
    assert "chart not present" in capsys.readouterr().out.lower()


def test_main_exits_one_on_blocking_asymmetry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [_row("E29-S65", "open", ["E29-S62"]), _row("E29-S62", "merged")]
    exit_code = main(_write(tmp_path, _chart(rows)))
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "E29-S65 (open) names E29-S62 (merged)" in out


def test_main_exits_one_and_names_an_unknown_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_write(tmp_path, _chart([_row("E22-S8", "open", ["E22-S9"])])))
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "E22-S9, which has no row in the chart" in out


def test_main_exits_one_and_names_a_self_edge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_write(tmp_path, _chart([_row("A", "open", ["A"])])))
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "A lists itself in mergeSerializedWith" in out


def test_main_exits_zero_on_symmetric_chart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    exit_code = main(_write(tmp_path, _chart(rows)))
    assert exit_code == 0
    assert "OK: no blocking" in capsys.readouterr().out


def test_main_reports_the_row_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Pinned as the literal phrase: a bare ``"2"`` also matches a digit in the
    ``tmp_path`` echoed into the same output, and passes on a wrong count."""
    rows = [
        _row("E29-S65", "open", ["E29-S62"]),
        _row("E29-S62", "merged", ["E29-S65"]),
    ]
    main(_write(tmp_path, _chart(rows)))
    assert "Audited 2 rows" in capsys.readouterr().out


def test_main_reports_merged_only_pairs_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The report line is asserted, not just the exit code: a validator that
    ignored merged pairs entirely would also exit 0 here."""
    rows = [_row("E23-S21", "merged", ["E29-S62"]), _row("E29-S62", "merged")]
    exit_code = main(_write(tmp_path, _chart(rows)))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "E23-S21 (merged) names E29-S62 (merged)" in out


def test_main_exits_two_on_unparseable_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2, not 1: a damaged chart must be distinguishable from one carrying
    real violations, because the chart is hand-edited by concurrent sessions."""
    chart = tmp_path / "open_stories.json"
    chart.write_text("{not json", encoding="utf-8")
    exit_code = main(chart)
    assert exit_code == 2
    assert "ERROR: could not read" in capsys.readouterr().out


def test_main_exits_two_on_structurally_malformed_chart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_write(tmp_path, {"stories": [{"status": "open"}]}))
    assert exit_code == 2
    assert "ERROR: could not read" in capsys.readouterr().out


def test_main_exits_two_on_deeply_nested_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``json.loads`` raises RecursionError, which is not a ValueError, so it
    would otherwise escape as a traceback."""
    chart = tmp_path / "open_stories.json"
    chart.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    exit_code = main(chart)
    assert exit_code == 2
    assert "ERROR: could not read" in capsys.readouterr().out
