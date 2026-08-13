"""Smoke test for SHA-pinned CI marketplace actions (Story 10.61 / SEC-26).

There is no feature code to test-drive here — the story replaces mutable-tag
``uses:`` references in ``.github/workflows/`` with full commit SHAs. The
real verification is that all three CI jobs still run and pass against the
pinned actions (see docs/tech-debt.md's SEC-26 entry) — nothing local
exercises a GitHub Actions workflow. This module is the executable spec that
keeps the *pins themselves* honest: every ``uses:`` line must reference a
40-character commit SHA, not a tag, and must carry a trailing comment naming
the human-readable version so the file stays reviewable.

Usage:
  pytest tests/architecture/test_ci_workflow_pins.py -v
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Matches a `uses: owner/repo@ref` step, capturing the ref and any trailing
# comment. A 40-char hex ref is a commit SHA; anything else (a tag like `v4`,
# a branch, `main`) is mutable. The leading `-` is optional and NOT required
# to share the line with `uses:`: GitHub Actions allows both the compact
# `- uses: action@ref` form and the equally valid `- name: Foo` /
# `  uses: action@ref` two-line form. Requiring the dash inline would let a
# mutable-tag step written in the two-line style slip past this check
# unnoticed — the exact regression this test exists to catch (Story 10.61).
_USES_RE = re.compile(
    r"^(?P<indent>\s*)(?:-\s*)?uses:\s*(?P<action>\S+)@(?P<ref>\S+)(?:\s*#\s*(?P<comment>.+))?$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _workflow_files() -> list[Path]:
    files = sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))
    assert files, f"No workflow files found under {_WORKFLOWS_DIR}"
    return files


def _uses_lines() -> list[tuple[Path, int, re.Match[str]]]:
    matches = []
    for path in _workflow_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _USES_RE.match(line)
            if match:
                matches.append((path, lineno, match))
    assert matches, f"No `uses:` steps found under {_WORKFLOWS_DIR} — expected at least one"
    return matches


def test_every_uses_line_is_pinned_to_a_commit_sha():
    """AC 1: no `uses:` line references a mutable tag."""
    for path, lineno, match in _uses_lines():
        ref = match.group("ref")
        assert _SHA_RE.match(ref), (
            f"{path.relative_to(_REPO_ROOT)}:{lineno} pins {match.group('action')}@{ref}, "
            f"which is not a 40-character commit SHA. Marketplace actions must be pinned "
            f"to a commit SHA, not a mutable tag (Story 10.61 / SEC-26)."
        )


def test_every_pinned_line_names_its_version_in_a_comment():
    """AC 2: every pinned line carries a comment naming the human-readable version."""
    for path, lineno, match in _uses_lines():
        comment = match.group("comment")
        assert comment and comment.strip(), (
            f"{path.relative_to(_REPO_ROOT)}:{lineno} pins {match.group('action')}@{match.group('ref')} "
            f"with no trailing `# vX.Y.Z` comment naming the version it corresponds to."
        )
