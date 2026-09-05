"""Offline tests for ShopifyClient.paginate().

Calls the unbound method with a MagicMock `self` so the full loop logic —
cursor forwarding, max_pages cap, missing pageInfo fallback — is exercised
without instantiating ShopifyClient (which needs live credentials).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/test_paginate.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from shopify_mcp.client import ShopifyClient
from tests.support import FakeClient

QUERY = "query Q($first: Int!, $after: String) { data { nodes { id } pageInfo { hasNextPage endCursor } } }"
PATH = ["data"]

# Story 10.78 (T-10.6-paginate-vanish): the nested shape the collection reads
# walk, kept alongside the flat PATH so the vanished-connection guard is proven
# at both depths — a guard that fired only on a one-key path would miss every
# caller in shopify/operations/products.py.
NESTED_PATH = ["collectionByHandle", "products"]


def _page(nodes, has_next, cursor=None):
    return {
        "data": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }
    }


def _mock_client(responses):
    m = MagicMock()
    m.execute.side_effect = list(responses)
    return m


def test_single_page_returns_nodes_not_capped():
    resp = _page([{"id": "a"}], has_next=False)
    m = _mock_client([resp])
    first, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert nodes == [{"id": "a"}]
    assert capped is False
    assert first == resp


def test_two_pages_concatenates_nodes_and_forwards_cursor():
    page0 = _page([{"id": "a"}, {"id": "b"}], has_next=True, cursor="cur1")
    page1 = _page([{"id": "c"}], has_next=False)
    m = _mock_client([page0, page1])
    first, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert nodes == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert capped is False
    assert first == page0
    # Second call must carry the cursor from page 0.
    assert m.execute.call_args_list[1][0][1]["after"] == "cur1"


def test_max_pages_cap_sets_capped_true_and_logs_warning():
    responses = [_page([{"id": str(i)}], has_next=True, cursor=f"c{i}") for i in range(3)]
    m = _mock_client(responses)
    with patch("shopify_mcp.client.logger") as mock_log:
        _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH, max_pages=3)
    assert capped is True
    assert len(nodes) == 3
    mock_log.warning.assert_called_once()


def test_empty_nodes_page():
    m = _mock_client([_page([], has_next=False)])
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert nodes == []
    assert capped is False


def test_missing_page_info_treated_as_no_next_page():
    """pageInfo absent → default to {} → hasNextPage=False → single page, not capped."""
    m = _mock_client([{"data": {"nodes": [{"id": "x"}]}}])
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert nodes == [{"id": "x"}]
    assert capped is False
    assert m.execute.call_count == 1


def test_page_size_and_after_none_on_first_call():
    """First call always sends after=None with the requested page_size."""
    m = _mock_client([_page([], has_next=False)])
    ShopifyClient.paginate(m, QUERY, {"id": "123"}, connection_path=PATH, page_size=25)
    call_vars = m.execute.call_args_list[0][0][1]
    assert call_vars["first"] == 25
    assert call_vars["after"] is None


def test_null_cursor_with_has_next_page_returns_capped_without_refetch():
    """If Shopify returns hasNextPage=True but endCursor=null, paginate must
    abort and return capped=True rather than re-fetching page 0 in a loop."""
    m = _mock_client([_page([{"id": "a"}], has_next=True, cursor=None)])
    with patch("shopify_mcp.client.logger") as mock_log:
        _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert capped is True
    assert nodes == [{"id": "a"}]
    assert m.execute.call_count == 1
    # Two warnings fire: the null-cursor guard + the generic cap warning.
    assert mock_log.warning.call_count == 2
    assert any("endCursor=null" in str(call) for call in mock_log.warning.call_args_list)


# ---------- Story 10.78: a connection that vanishes mid-walk is a truncation ----------
#
# paginate() used to return capped=False whenever hasNextPage was falsy — and a
# connection that disappeared on page 2+ collapses to {} under the path walk's
# `or {}`, so its absent pageInfo read as "no more pages". Page 1 had already
# said hasNextPage=True, so the walk knew more existed and reported the partial
# result as complete. These four combinations pin both vanish SHAPES (parent
# gone null, key absent) at both path DEPTHS (nested, top-level).


def _nested_page(nodes, has_next, cursor=None):
    """One page of a products connection nested inside collectionByHandle."""
    return {
        "collectionByHandle": {
            "id": "gid://shopify/Collection/999",
            "products": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }
    }


# (path, page-0 builder, page-1 payload that makes the connection vanish)
_VANISH_CASES = [
    pytest.param(NESTED_PATH, _nested_page, {"collectionByHandle": None}, id="nested-null-parent"),
    pytest.param(
        NESTED_PATH, _nested_page, {"collectionByHandle": {"id": "x"}}, id="nested-missing-key"
    ),
    pytest.param(PATH, _page, {"data": None}, id="flat-null-connection"),
    pytest.param(PATH, _page, {}, id="flat-missing-key"),
]


@pytest.mark.parametrize(("path", "build_page", "vanished"), _VANISH_CASES)
def test_connection_vanishing_on_page_two_is_capped(path, build_page, vanished):
    """A connection that resolves to nothing on page 1+ is a TRUNCATION, so the
    walk must report capped=True rather than the complete result it used to."""
    m = _mock_client([build_page([{"id": "a"}], has_next=True, cursor="cur1"), vanished])
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=path)
    assert capped is True
    # AC2: a truncated walk hands back what it got, exactly as the max_pages
    # and endCursor-is-null paths do. Discarding the partial result would be a
    # different bug, not a fix.
    assert nodes == [{"id": "a"}]
    assert m.execute.call_count == 2


def test_vanished_connection_logs_warning_naming_path_and_page():
    """The abnormal stop is logged like the endCursor-is-null branch beside it,
    naming connection_path and the page index so an operator can tell a
    vanished connection from an exhausted page budget."""
    m = _mock_client(
        [_nested_page([{"id": "a"}], has_next=True, cursor="cur1"), {"collectionByHandle": None}]
    )
    with patch("shopify_mcp.client.logger") as mock_log:
        _, _, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=NESTED_PATH)
    assert capped is True
    # The vanish warning plus the shared cap warning the break falls through to.
    assert mock_log.warning.call_count == 2
    vanish_calls = [c for c in mock_log.warning.call_args_list if "vanished" in str(c)]
    assert len(vanish_calls) == 1
    rendered = str(vanish_calls[0])
    assert "collectionByHandle" in rendered
    # page=1 — the second request, the one that came back empty.
    assert "1" in rendered


def test_vanished_connection_on_a_later_page_keeps_every_earlier_page():
    """Three pages deep: the two pages that arrived are returned whole, and the
    third page's disappearance is what sets the flag."""
    m = _mock_client(
        [
            _page([{"id": "a"}], has_next=True, cursor="c0"),
            _page([{"id": "b"}], has_next=True, cursor="c1"),
            {},
        ]
    )
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert nodes == [{"id": "a"}, {"id": "b"}]
    assert capped is True


def test_fake_client_paginate_also_caps_on_a_vanished_connection():
    """FakeClient.paginate calls itself a mirror of the real one, and every
    operations-layer test in the repo runs against it — so the guard has to
    exist in both or the offline suite stops testing the real control flow."""
    fc = FakeClient(
        [_nested_page([{"id": "a"}], has_next=True, cursor="cur1"), {"collectionByHandle": None}]
    )
    _, nodes, capped = fc.paginate(QUERY, {}, connection_path=NESTED_PATH)
    assert capped is True
    assert nodes == [{"id": "a"}]


# ---------- Story 10.78: the negative half — the guard must not over-fire ----------
#
# Every test below PASSES BOTH BEFORE AND AFTER the guard lands. They are not
# the RED tests; they are what stops the fix from turning "empty" into
# "truncated" and breaking Story 10.76's not-found path. "The connection
# resolved to nothing at all" is the condition — never "it had no nodes".


def test_legitimately_empty_final_page_stays_uncapped():
    """PASSES BOTH BEFORE AND AFTER. A present connection returning zero nodes
    with hasNextPage=false on page 2 is a COMPLETE walk that happened to end on
    an empty page — not a vanished one."""
    m = _mock_client(
        [_page([{"id": "a"}], has_next=True, cursor="cur1"), _page([], has_next=False)]
    )
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert capped is False
    assert nodes == [{"id": "a"}]


def test_nested_legitimately_empty_final_page_stays_uncapped():
    """PASSES BOTH BEFORE AND AFTER. The same claim one level down, where the
    parent object is still present and only its products list is empty."""
    m = _mock_client(
        [
            _nested_page([{"id": "a"}], has_next=True, cursor="cur1"),
            _nested_page([], has_next=False),
        ]
    )
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=NESTED_PATH)
    assert capped is False
    assert nodes == [{"id": "a"}]


def test_page_zero_null_parent_stays_uncapped_with_first_page_intact():
    """PASSES BOTH BEFORE AND AFTER, and is the one Story 10.76 depends on: a
    page-0 `collectionByHandle: null` means "no such collection", not a
    truncation, so it must still resolve to capped=False in ONE request with
    the first-page dict returned unchanged. read_products_by_collection and
    read_collection_with_descriptions map a missing handle to None from exactly
    this shape — a guard that fired on page 0 would break both."""
    absent = {"collectionByHandle": None}
    m = _mock_client([absent])
    first, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=NESTED_PATH)
    assert capped is False
    assert nodes == []
    assert first == absent
    assert m.execute.call_count == 1


def test_page_zero_missing_key_stays_uncapped():
    """PASSES BOTH BEFORE AND AFTER. The absent-key shape of the same page-0
    tolerance: nothing was ever there to walk, so nothing was truncated."""
    m = _mock_client([{}])
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert capped is False
    assert nodes == []
    assert m.execute.call_count == 1


def test_single_page_nested_walk_is_unaffected():
    """PASSES BOTH BEFORE AND AFTER. A one-page walk never reaches the guard."""
    m = _mock_client([_nested_page([{"id": "a"}], has_next=False)])
    _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=NESTED_PATH)
    assert capped is False
    assert nodes == [{"id": "a"}]
