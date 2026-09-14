"""
Order tools — read-only access to Shopify orders.

Thin MCP-tool surface over ``shopify.operations.orders``: this module keeps param
coercion (limit clamping), the untrusted-data wrapping, and output formatting; the
GraphQL strings live in ``shopify.queries.orders`` and the data access in
``shopify.operations.orders`` (Story 10.29 / A5).
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import orders as ops
from shopify_mcp.shopify.queries.orders import GET_ORDER_BY_ID, GET_ORDERS
from shopify_mcp.tools._gid import from_gid
from shopify_mcp.tools._untrusted import INJECTION_REMINDER, wrap

# The GraphQL strings now live in shopify.queries.orders. They are re-exported
# here so existing callers/tests (`from tools.orders import GET_ORDERS`) keep
# resolving to the same objects the operations layer executes.
__all__ = [
    "GET_ORDERS",
    "GET_ORDER_BY_ID",
    "register",
]

# The three UTM tags worth surfacing inline — a full UTMParameters selection
# also has `content`/`term`, but those two are the ones this tool's GraphQL
# selection doesn't even fetch (kept minimal to match the story's ask).
_UTM_KEYS = ("source", "medium", "campaign")


def _format_visit(visit: dict[str, Any] | None) -> str:
    """Render one CustomerVisit touchpoint (customerJourneySummary.firstVisit /
    .lastVisit) as shopper-facing text — the replacement for the removed
    referringSite/landingSite fields (Story 9.12). ``source`` and
    ``referrerUrl`` are shopper-influenced (an attacker can craft a UTM-tagged
    URL), so every value here goes through ``wrap()``, same as the old
    traffic-source line."""
    if not visit:
        return "direct / unknown"
    raw_label = visit.get("source") or visit.get("referrerUrl")
    rendered = wrap(raw_label) if raw_label else "direct"
    utm = visit.get("utmParameters") or {}
    utm_bits = [f"{key}={wrap(utm[key])}" for key in _UTM_KEYS if utm.get(key)]
    if utm_bits:
        rendered += f" (utm: {' '.join(utm_bits)})"
    return rendered


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_orders(limit: int = 20) -> str:
        """
        List recent orders with id, total price, line items, and traffic source.
        limit: number of orders to return (max 250).
        """
        limit = min(limit, 250)
        orders = ops.read_orders(client, limit)
        if not orders:
            return "No orders found."

        lines = [INJECTION_REMINDER + f"Recent orders ({len(orders)}):\n"]
        for o in orders:
            items = ", ".join(
                f"{wrap(li['name'])} x{li['quantity']}"
                for li in o.get("lineItems", {}).get("nodes", [])
            )
            journey = o.get("customerJourneySummary") or {}
            first_touch = _format_visit(journey.get("firstVisit"))
            last_touch = _format_visit(journey.get("lastVisit"))
            # `or {}` guards against nulls at any level — Shopify can return
            # totalPriceSet=null on orders still in a pending/edited state.
            total = ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount", "N/A")
            lines.append(
                f"  [{from_gid(o['id'])}] {o['name']} — ${total} — {o['createdAt'][:10]}\n"
                f"    Items: {items}\n"
                f"    First touch: {first_touch}\n"
                f"    Last touch: {last_touch}"
            )
        # GET_ORDERS caps each order's line items at a fixed first: N and cannot
        # paginate that nested-in-list connection, so warn (don't silently drop)
        # for every truncated order — parity with the single-order get_order cap
        # warning below (Story 10.34 / A3).
        cap = ops.GET_ORDERS_LINE_ITEM_CAP
        for gid in ops.capped_line_item_order_ids(orders):
            oid = from_gid(gid)
            lines.append(
                f"WARNING: order {oid} has more than {cap} line items — only the first "
                f"{cap} are shown here; get_orders cannot paginate per-order line items. "
                "Use get_order to retrieve the full line-item list."
            )
        return "\n".join(lines)

    @server.tool()
    def get_order(order_id: str) -> str:
        """Get a single order by id."""
        o, line_items, capped = ops.read_order(client, order_id)
        if not o:
            return f"Order {order_id} not found."

        total = ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount", "N/A")

        def _unit_price(li: dict[str, Any]) -> str:
            return ((li.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get(
                "amount", "N/A"
            )

        items = "\n".join(
            f"  • {wrap(li['name'])} x{li['quantity']} — ${_unit_price(li)}" for li in line_items
        )
        traffic_line = _format_visit((o.get("customerJourneySummary") or {}).get("lastVisit"))
        result = (
            INJECTION_REMINDER + f"Order: {o['name']} (id: {from_gid(o['id'])})\n"
            f"Date: {o['createdAt']}\n"
            f"Total: ${total}\n"
            f"Status: {o.get('displayFinancialStatus')} / {o.get('displayFulfillmentStatus')}\n"
            f"Traffic source: {traffic_line}\n"
            f"Line items:\n{items}"
        )
        if capped:
            result += "\nWARNING: line-item pagination stopped short — additional line items (if any) are not shown here."
        return result
