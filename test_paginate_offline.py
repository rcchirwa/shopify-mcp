"""Offline tests for ShopifyClient.paginate().

Calls the unbound method with a MagicMock `self` so the full loop logic —
cursor forwarding, max_pages cap, missing pageInfo fallback — is exercised
without instantiating ShopifyClient (which needs live credentials).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_paginate_offline.py -v
"""

from unittest.mock import MagicMock, patch

from shopify_client import ShopifyClient

QUERY = "query Q($first: Int!, $after: String) { data { nodes { id } pageInfo { hasNextPage endCursor } } }"
PATH = ["data"]


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
    with patch("shopify_client.logger") as mock_log:
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
    with patch("shopify_client.logger") as mock_log:
        _, nodes, capped = ShopifyClient.paginate(m, QUERY, {}, connection_path=PATH)
    assert capped is True
    assert nodes == [{"id": "a"}]
    assert m.execute.call_count == 1
    # Two warnings fire: the null-cursor guard + the generic cap warning.
    assert mock_log.warning.call_count == 2
    assert any("endCursor=null" in str(call) for call in mock_log.warning.call_args_list)
