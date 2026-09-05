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

from shopify_mcp.shopify._client import GraphQLClient
from shopify_mcp.shopify._identifiers import reject_both_identifiers
from shopify_mcp.shopify._ids import to_gid
from shopify_mcp.shopify.queries.publications import (
    GET_COLLECTION_PUBLICATIONS_BY_HANDLE,
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
    ``handle``. **Supplying both raises ``ValueError`` before any network
    call** — Story 10.68 (``T-10.65-refuse-both-fanout``) replaced the
    ``product_id``-wins precedence this function applied through Story 10.67,
    which silently discarded the handle and so could resolve — and, via the
    three publication write tools above this layer, mutate — the wrong product.
    The rule is shared with ``operations/products.py`` via
    ``shopify._identifiers``. This guard is what holds the rule for **non-MCP
    callers** (CLI, scripts) and for any future caller that forgets; the four
    ``tools/publications.py`` tools do **not** rely on it for their message —
    each vets the pair itself before its sales-channel reads, because a refusal
    inherited from here would arrive a round-trip late and be rendered with a
    misleading scope hint. See ``tools/_product_resolver.identifier_error``.

    Returns ``(product_or_None, resource_publication_nodes, capped)``.
    ``product_or_None`` is the product node (``id title handle ...``) or None
    when neither identifier is supplied or Shopify returns a null product
    (deleted / wrong id / unknown handle) — the neither-supplied case keeps
    returning rather than raising, deliberately unchanged by 10.68. ``capped``
    is True when the walk stopped short of the end — see
    ``ShopifyClient.paginate`` for the three ways that can happen."""
    reject_both_identifiers(product_id, handle)
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


def read_collection_publications(
    client: GraphQLClient, handle: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a collection and all its resourcePublications, paginated.

    Handle-only, mirroring every other collection read in this tree: there is no
    by-id collection query, and Story 10.83 deliberately did not add one. With a
    single identifier there is no both-supplied ambiguity, so unlike
    :func:`read_product_publications` this function needs no
    ``reject_both_identifiers`` guard — there is no pair to refuse.

    Returns ``(collection_or_None, resource_publication_nodes, capped)``, the
    same triple its product sibling returns. ``collection_or_None`` is None when
    no handle is supplied or Shopify returns a null collection.

    **The node list contains only the publications the collection IS on.**
    Shopify's ``resourcePublications`` defaults to ``onlyPublished: true``, so a
    channel the collection is not on is absent rather than present with
    ``isPublished: false`` — confirmed live on 2026-09-05 against a store whose
    publication roster had 7 entries while an unpublished collection returned 0
    nodes. Callers must derive the not-published set as roster-minus-listed."""
    if not handle:
        return None, [], False
    data, rps, capped = client.paginate(
        GET_COLLECTION_PUBLICATIONS_BY_HANDLE,
        {"handle": handle},
        connection_path=["collectionByHandle", "resourcePublications"],
        page_size=PUBLICATIONS_PAGE_SIZE,
    )
    return data.get("collectionByHandle"), rps, capped


# ---------- writes (return the raw mutation result) ----------
#
# Both mutations are generic over Shopify's ``Publishable`` interface, so they
# serve products and collections alike. ``resource_gid`` was named
# ``product_gid`` until Story 10.83 — the parameter never had anything
# product-specific about it, and the old name would now be actively misleading.


def publish(client: GraphQLClient, resource_gid: str, publication_ids: list[str]) -> dict[str, Any]:
    """Execute ``publishablePublish`` for a publishable resource against the given publications.

    Builds the ``[{"publicationId": ...}]`` PublicationInput list from
    ``publication_ids`` (full publication GIDs taken from a publication node's
    ``id``). ``resource_gid`` is the target's full GID — a Product or a
    Collection."""
    inputs = [{"publicationId": pid} for pid in publication_ids]
    return client.execute(PUBLISHABLE_PUBLISH, {"id": resource_gid, "input": inputs})


def unpublish(
    client: GraphQLClient, resource_gid: str, publication_ids: list[str]
) -> dict[str, Any]:
    """Execute ``publishableUnpublish`` for a publishable resource against the given publications.

    Mirror of :func:`publish` — builds the same ``[{"publicationId": ...}]`` input
    from ``publication_ids`` and executes the unpublish mutation."""
    inputs = [{"publicationId": pid} for pid in publication_ids]
    return client.execute(PUBLISHABLE_UNPUBLISH, {"id": resource_gid, "input": inputs})
