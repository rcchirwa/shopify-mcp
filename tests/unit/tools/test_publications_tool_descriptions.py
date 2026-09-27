"""
Pin the DRAFT assigned-but-not-live state (Story 10.99) into the four product
publication tools' agent-facing DESCRIPTIONS, not just their output (Story
10.100).

Story 10.99 (PR #181) taught the tools a third channel state for a DRAFT
product — assigned but not live, going live the instant the product turns
ACTIVE — and reflected it in every tool's rendered OUTPUT. Its code review
noted, and skipped, that the tool DESCRIPTIONS an MCP client actually reads
(the docstrings FastMCP registers as `Tool.description`) never mention it, so
an agent calling these tools blind has no way to know the state exists. This
module is that follow-up.

The description is read from the REGISTERED server, the same way
`test_confirmed_output_guard.py::_write_tool_names` does it — never
`func.__doc__` directly — so the test fails if a description were ever lost
between the docstring and what FastMCP hands to a client. `_ENV_PATH` is
monkeypatched on both `shopify_mcp.server` and `shopify_mcp.client` to a
nonexistent path for the same reason that guard does it: both modules call
`load_dotenv(override=True)` before reading `Settings()`, and a real `.env`
at the repo root would silently override the synthetic token below.
"""

import asyncio
from pathlib import Path

import pytest

import shopify_mcp.client as _client_module
import shopify_mcp.server as _server_module
from shopify_mcp.server import create_server


def _tool_descriptions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setenv("SHOPIFY_STORE_URL", "test.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_test00000000000000000000000")
    nonexistent_env = tmp_path / "nonexistent.env"
    monkeypatch.setattr(_server_module, "_ENV_PATH", nonexistent_env)
    monkeypatch.setattr(_client_module, "_ENV_PATH", nonexistent_env)
    server = create_server()
    tools = asyncio.run(server.list_tools())
    return {t.name: t.description or "" for t in tools}


# (tool name, phrase(s) that must appear — all of them). Each phrase is
# load-bearing: it would be lost if the new paragraph were deleted, and none
# of them appear anywhere else in the tool's existing (pre-10.100) docstring
# text, so a deleted paragraph cannot pass by accident. "could not be fully
# read" pins each tool's sentence about the trailing incomplete-read note.
_MUST_CONTAIN: dict[str, tuple[str, ...]] = {
    "get_product_publications": (
        "Assigned (goes live when the product is ACTIVE)",
        "could not be fully read",
    ),
    "publish_product_to_channels": ("(assigned, not live)", "DRAFT", "could not be fully read"),
    "unpublish_product_from_channels": (
        "(assigned, not live)",
        "(was assigned, not live)",
        "could not be fully read",
    ),
    "set_product_publications": (
        "(assigned, not live)",
        "(was assigned, not live)",
        "could not be fully read",
    ),
}

# Collection tools stay out of Story 10.99's and 10.100's scope (collections
# have no status and no assigned state — see the module docstring and the
# 2026-09-26 tech-debt entry). Their descriptions must never pick up the
# assigned-state labels, which would signal scope creep into the collection
# tools. The labels are matched, not the bare word "assigned", so an unrelated
# use of the word cannot fail this check.
_ASSIGNED_STATE_LABELS = (
    "(assigned, not live)",
    "(was assigned, not live)",
    "Assigned (goes live",
)
_COLLECTION_TOOLS = (
    "get_collection_publications",
    "publish_collection_to_channels",
    "unpublish_collection_from_channels",
)


@pytest.mark.parametrize("name,phrases", sorted(_MUST_CONTAIN.items()))
def test_product_publication_tool_description_documents_the_assigned_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, phrases: tuple[str, ...]
) -> None:
    descriptions = _tool_descriptions(monkeypatch, tmp_path)
    for phrase in phrases:
        assert phrase in descriptions[name], (
            f"{name}: description is missing {phrase!r} — the DRAFT "
            f"assigned-but-not-live paragraph (Story 10.100) looks deleted.\n"
            f"description={descriptions[name]!r}"
        )


@pytest.mark.parametrize("name", _COLLECTION_TOOLS)
def test_collection_publication_tool_description_does_not_mention_assigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    descriptions = _tool_descriptions(monkeypatch, tmp_path)
    for label in _ASSIGNED_STATE_LABELS:
        assert label not in descriptions[name], (
            f"{name}: collection tools have no assigned-channel state (Story "
            f"10.99/10.100 are product-only) — this description should not "
            f"carry {label!r}.\ndescription={descriptions[name]!r}"
        )
