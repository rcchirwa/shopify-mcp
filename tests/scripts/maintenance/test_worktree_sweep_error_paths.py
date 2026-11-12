"""Error paths of ``scripts/maintenance/e32_s01_worktree_sweep.py`` that the Holocron suite
does not reach. Holocron did not measure coverage of ``scripts/``; shopify-mcp does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.maintenance import e32_s01_worktree_sweep as sweep


def test_a_chart_that_cannot_be_read_is_reported_not_raised(tmp_path: Path) -> None:
    chart, note = sweep.read_chart(tmp_path / "missing.json")
    assert chart is None
    assert note == "chart in-flight rows: unreadable (FileNotFoundError)"


def test_classifying_outside_a_git_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="could not read the git common directory"):
        sweep.classify_worktrees(
            tmp_path,
            [],
            main_path=None,
            live=sweep.LiveSet(shas=frozenset()),
            live_only=frozenset(),
        )
