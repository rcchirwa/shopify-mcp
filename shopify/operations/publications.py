"""Typed publications operations — data access over ``shopify.queries.publications``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + GraphQL-variable building + query/mutation execution,
returning structured data (reads) or the raw Shopify response (writes). No MCP
imports and no output formatting, so these are callable from non-MCP entry points
(CLI, scripts, tests) — Story 10.30 / A5, AC4. ``tools/publications.py`` layers the
channel-name/-id resolution cache, the publish/unpublish/declarative-set diff, the
preview/confirm flow, userError mapping, and string formatting on top.
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify._ids import to_gid
from shopify.queries.publications import (
    GET_PRODUCT_PUBLICATIONS_BY_HANDLE,
    GET_PRODUCT_PUBLICATIONS_BY_ID,
    LIST_PUBLICATIONS,
    PUBLISHABLE_PUBLISH,
    PUBLISHABLE_UNPUBLISH,
)

# Page size for the paginated reads (the publications list and a product's
# resourcePublications) via ``client.paginate()`` — how many nodes are fetched per
# Shopify request.
PUBLICATIONS_PAGE_SIZE = 50


# ---------- reads ----------


def read_publications(client: GraphQLClient) -> list[dict[str, Any]]:
    """List every publication (sales channel) on the store, paginated.

    Returns the publication node list (``id name supportsFuturePublishing``)."""
    _resp, nodes, _capped = client.paginate(
        LIST_PUBLICATIONS, {}, connection_path=["publications"], page_size=PUBLICATIONS_PAGE_SIZE
    )
    return nodes


def read_product_publications(
    client: GraphQLClient, product_id: str, handle: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a product and all its resourcePublications, paginated.

    Resolves by ``product_id`` (coerced to a Product GID) when given, else by
    ``handle``; ``product_id`` wins when both are supplied. Returns
    ``(product_or_None, resource_publication_nodes, capped)``. ``product_or_None``
    is the product node (``id title handle ...``) or None when neither identifier
    is supplied or Shopify returns a null product (deleted / wrong id / unknown
    handle). ``capped`` is True when pagination hit the max-pages cap."""
    if product_id:
        data, rps, capped = client.paginate(
            GET_PRODUCT_PUBLICATIONS_BY_ID,
            {"id": to_gid("Product", product_id)},
            connection_path=["product", "resourcePublications"],
            page_size=PUBLICATIONS_PAGE_SIZE,
        )
        return data.get("product"), rps, capped
    if handle:
        data, rps, capped = client.paginate(
            GET_PRODUCT_PUBLICATIONS_BY_HANDLE,
            {"handle": handle},
            connection_path=["productByHandle", "resourcePublications"],
            page_size=PUBLICATIONS_PAGE_SIZE,
        )
        return data.get("productByHandle"), rps, capped
    return None, [], False


# ---------- writes (return the raw mutation result) ----------


def publish(client: GraphQLClient, product_gid: str, publication_ids: list[str]) -> dict[str, Any]:
    """Execute ``publishablePublish`` for a product against the given publications.

    Builds the ``[{"publicationId": ...}]`` PublicationInput list from
    ``publication_ids`` (full publication GIDs taken from a publication node's
    ``id``). ``product_gid`` is the product's full GID."""
    inputs = [{"publicationId": pid} for pid in publication_ids]
    return client.execute(PUBLISHABLE_PUBLISH, {"id": product_gid, "input": inputs})


def unpublish(
    client: GraphQLClient, product_gid: str, publication_ids: list[str]
) -> dict[str, Any]:
    """Execute ``publishableUnpublish`` for a product against the given publications.

    Mirror of :func:`publish` — builds the same ``[{"publicationId": ...}]`` input
    from ``publication_ids`` and executes the unpublish mutation."""
    inputs = [{"publicationId": pid} for pid in publication_ids]
    return client.execute(PUBLISHABLE_UNPUBLISH, {"id": product_gid, "input": inputs})
