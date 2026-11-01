"""
Every registered MCP tool declares annotations (Story 10.73).

The tools are read from a server built by ``create_server()`` and listed with
``list_tools()`` — the same objects FastMCP serializes into a ``tools/list``
response — never from a hand-kept list, so a new tool cannot ship without
hints. The count is pinned independently as well: a check that loops over the
server's own list cannot see a tool that is missing from it.

``docs/tool-annotations.md`` is the table of record (one row per tool, with the
reason for each hint). It is compared with the served hints row for row, and
README's tool tables must name the same hint set per tool, so neither document
can drift from what the server actually declares.

Annotations are advisory hints (the MCP spec forbids clients from relying on
them for security decisions); these checks keep them accurate, not enforced.

Usage:
  pytest tests/architecture/test_tool_annotations.py -v
"""

import asyncio
import re
from pathlib import Path

import pytest
from mcp.types import Tool

import shopify_mcp.client as _client_module
import shopify_mcp.server as _server_module
from shopify_mcp.server import create_server

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# Spelled here, not derived from the server, so a dropped registration fails.
# Change it in the same commit that adds or removes a tool.
_EXPECTED_TOOL_COUNT = 52

_ROW = re.compile(r"^\| `(?P<name>[a-z_]+)` \|(?P<rest>.*)\|\s*$")


@pytest.fixture(scope="module")
def tools() -> list[Tool]:
    """Every tool on a real server, built with synthetic credentials.

    Both ``server._ENV_PATH`` and ``client._ENV_PATH`` point at a missing file,
    as in ``test_confirmed_output_guard._write_tool_names``: both modules call
    ``load_dotenv(..., override=True)``, so a real ``.env`` would otherwise
    replace the synthetic token. Listing tools touches no network.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SHOPIFY_STORE_URL", "test.myshopify.com")
        mp.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_test00000000000000000000000")
        nonexistent_env = _REPO_ROOT / "tests" / "nonexistent.env"
        mp.setattr(_server_module, "_ENV_PATH", nonexistent_env)
        mp.setattr(_client_module, "_ENV_PATH", nonexistent_env)
        server = create_server()
        return asyncio.run(server.list_tools())


def _served_hints(tool: Tool) -> tuple[bool | None, ...]:
    """The four hints as sent on the wire (``None`` = not declared)."""
    wire = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    annotations = wire.get("annotations") or {}
    return tuple(annotations.get(h) for h in _HINTS)


def _table_rows(path: Path) -> dict[str, list[str]]:
    """``{tool name: [cells after the name]}`` for every tool row in a doc."""
    rows: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            assert match["name"] not in rows, f"{path.name}: duplicate row for {match['name']}"
            rows[match["name"]] = [c.strip() for c in match["rest"].split("|")]
    return rows


def _flag(cell: str) -> bool:
    assert cell in ("true", "false"), f"not a hint value: {cell!r}"
    return cell == "true"


def test_tool_count_is_pinned(tools: list[Tool]) -> None:
    assert len(tools) == _EXPECTED_TOOL_COUNT, (
        f"{len(tools)} tools registered, expected {_EXPECTED_TOOL_COUNT}. "
        "Update _EXPECTED_TOOL_COUNT and docs/tool-annotations.md together."
    )


def test_every_tool_declares_all_four_hints(tools: list[Tool]) -> None:
    bare = sorted(t.name for t in tools if None in _served_hints(t))
    assert not bare, f"{len(bare)} tools do not declare all four annotation hints: {bare}"


def test_confirm_gated_tools_are_not_read_only(tools: list[Tool]) -> None:
    """The confirm rule: a tool that can write describes its confirmed path."""
    wrong = sorted(
        t.name
        for t in tools
        if "confirm" in (t.inputSchema.get("properties") or {}) and _served_hints(t)[0] is not False
    )
    assert not wrong, f"confirm-gated tools not declared readOnlyHint=False: {wrong}"


def test_read_only_tools_state_the_safe_values(tools: list[Tool]) -> None:
    """Rule 5: a read tool never leaves a client on the spec defaults."""
    wrong = sorted(
        t.name
        for t in tools
        if _served_hints(t)[0] is True and _served_hints(t)[1:3] != (False, True)
    )
    assert not wrong, f"read-only tools not declaring destructive=False, idempotent=True: {wrong}"


def test_every_tool_is_open_world(tools: list[Tool]) -> None:
    closed = sorted(t.name for t in tools if _served_hints(t)[3] is not True)
    assert not closed, f"tools not declaring openWorldHint=True (all call Shopify): {closed}"


def test_docs_table_matches_served_hints(tools: list[Tool]) -> None:
    """docs/tool-annotations.md is the table of record; it must equal the wire."""
    rows = _table_rows(_REPO_ROOT / "docs" / "tool-annotations.md")
    served = {t.name: _served_hints(t) for t in tools}
    assert set(rows) == set(served), (
        f"table rows without a tool: {sorted(set(rows) - set(served))}; "
        f"tools without a row: {sorted(set(served) - set(rows))}"
    )
    mismatched = sorted(
        name for name, cells in rows.items() if tuple(_flag(c) for c in cells[1:5]) != served[name]
    )
    assert not mismatched, f"docs/tool-annotations.md disagrees with the server for: {mismatched}"


def test_docs_rows_match_their_hint_set() -> None:
    """Each row's four values equal the values of the hint set it names."""
    text = (_REPO_ROOT / "docs" / "tool-annotations.md").read_text(encoding="utf-8")
    sets = {
        m[1]: tuple(_flag(v.strip()) for v in m[2].split("|")[:4])
        for m in re.finditer(r"^\| `([A-Z_]+)` \|(.*)\|\s*$", text, re.MULTILINE)
    }
    assert sets, "no hint-set table found in docs/tool-annotations.md"
    rows = _table_rows(_REPO_ROOT / "docs" / "tool-annotations.md")
    wrong = sorted(
        name
        for name, cells in rows.items()
        if sets.get(cells[0].strip("`")) != tuple(_flag(c) for c in cells[1:5])
    )
    assert not wrong, f"rows whose values differ from their named hint set: {wrong}"


def test_readme_names_each_tools_hint_set() -> None:
    """README's tool tables carry the same hint set per tool, in their last column."""
    doc_rows = _table_rows(_REPO_ROOT / "docs" / "tool-annotations.md")
    readme_rows = _table_rows(_REPO_ROOT / "README.md")
    assert set(readme_rows) == set(doc_rows), (
        f"README rows without a docs row: {sorted(set(readme_rows) - set(doc_rows))}; "
        f"tools missing from README: {sorted(set(doc_rows) - set(readme_rows))}"
    )
    wrong = sorted(n for n, cells in readme_rows.items() if cells[-1] != doc_rows[n][0])
    assert not wrong, f"README hint set differs from docs/tool-annotations.md for: {wrong}"
