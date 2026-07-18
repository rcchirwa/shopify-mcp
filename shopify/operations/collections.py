"""Typed collection operations — business logic over ``shopify.queries.collections``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + input-building + query/mutation execution, returning
structured data. No MCP imports and no output formatting, so these are callable
from non-MCP entry points (CLI, scripts, tests). ``tools/collections.py`` layers
the smart/manual classification, the preview/confirm flow, async job polling, and
string formatting on top.

Story 10.26 / A5. Collections has a by-handle read (``GET_COLLECTION_BY_HANDLE``)
but no by-id twin, so there is no duplicated selection set to factor into a
shared fragment — none is extracted (cf. ``products`` / ``catalog_hygiene``,
which had by-id / by-handle pairs).
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify._ids import to_gid
from shopify.queries.collections import (
    ADD_PRODUCTS_TO_COLLECTION,
    GET_COLLECTION_BY_HANDLE,
    REMOVE_PRODUCTS_FROM_COLLECTION,
    UPDATE_COLLECTION,
)

# ---------- reads ----------


def read_collection_by_handle(client: GraphQLClient, handle: str) -> dict[str, Any] | None:
    """Read a collection by handle.

    Returns the raw ``collectionByHandle`` node (id/title/handle/descriptionHtml/
    ruleSet) or ``None`` when no collection has that handle. The caller classifies
    smart vs. manual from the presence of ``ruleSet``.
    """
    data = client.execute(GET_COLLECTION_BY_HANDLE, {"handle": handle})
    return data.get("collectionByHandle")


# ---------- writes (return the raw mutation result) ----------


def update_collection(
    client: GraphQLClient,
    collection_id: str,
    *,
    new_title: str = "",
    new_description: str | None = None,
) -> dict[str, Any]:
    """Execute a ``collectionUpdate`` setting title and/or descriptionHtml.

    ``collection_id`` is the GID read back from ``read_collection_by_handle``.
    Only the provided fields go into the input, so a title-only update leaves the
    description untouched (and vice versa). ``new_description`` uses ``None`` —
    not an empty string — to mean "not provided", so a caller can still write an
    explicit empty description (e.g. Story 10.35's sanitizer reducing fully
    disallowed markup to "") without it being mistaken for a no-op.
    """
    inp: dict[str, Any] = {"id": collection_id}
    if new_title:
        inp["title"] = new_title
    if new_description is not None:
        inp["descriptionHtml"] = new_description
    return client.execute(UPDATE_COLLECTION, {"input": inp})


def _change_membership(
    client: GraphQLClient, mutation: str, collection_id: str, product_id: str
) -> dict[str, Any]:
    """Shared add/remove flow: GID-coerce the product id and run ``mutation``.

    ``collection_id`` is already a GID (from the preceding read); only
    ``product_id`` needs coercion.
    """
    return client.execute(
        mutation,
        {"id": collection_id, "productIds": [to_gid("Product", product_id)]},
    )


def add_products_to_collection(
    client: GraphQLClient, collection_id: str, product_id: str
) -> dict[str, Any]:
    """Execute ``collectionAddProductsV2`` adding one product to a collection."""
    return _change_membership(client, ADD_PRODUCTS_TO_COLLECTION, collection_id, product_id)


def remove_products_from_collection(
    client: GraphQLClient, collection_id: str, product_id: str
) -> dict[str, Any]:
    """Execute ``collectionRemoveProducts`` removing one product from a collection."""
    return _change_membership(client, REMOVE_PRODUCTS_FROM_COLLECTION, collection_id, product_id)
