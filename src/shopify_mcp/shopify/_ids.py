"""GID encode/decode utilities — canonical home under ``shopify``.

These are pure functions with no dependencies, needed by ``shopify.operations``
(which must not import from ``tools``). ``tools/_gid.py`` re-exports them so the
many existing ``from tools._gid import ...`` call sites keep working unchanged.
"""

PRODUCT_GID_PREFIX = "gid://shopify/Product/"

INVALID_PRODUCT_ID_ERROR = (
    "product_id must be a numeric ID or a full Product GID (gid://shopify/Product/<id>)"
)

INVALID_SUBSCRIPTION_ID_ERROR = (
    "subscription_id must be a numeric ID or a full WebhookSubscription GID "
    "(gid://shopify/WebhookSubscription/<id>)"
)

INVALID_ORDER_ID_ERROR = (
    "order_id must be a numeric ID or a full Order GID (gid://shopify/Order/<id>)"
)

INVALID_INVENTORY_ITEM_ID_ERROR = (
    "inventory_item_id must be a numeric ID or a full InventoryItem GID "
    "(gid://shopify/InventoryItem/<id>)"
)

INVALID_LOCATION_ID_ERROR = (
    "location_id must be a numeric ID or a full Location GID (gid://shopify/Location/<id>)"
)


def to_gid(resource_type: str, numeric_id: int | str) -> str:
    return f"gid://shopify/{resource_type}/{numeric_id}"


def typed_gid(resource_type: str, value: int | str, error: str) -> str:
    """Return the ``resource_type`` GID for an id given as a number or a GID.

    Surrounding whitespace is stripped first. A GID of that type passes
    through, and a value that is not a GID is wrapped. These raise
    ValueError(``error``): a GID of another type, a GID with nothing after
    the prefix, and a path such as ``foo/1``. The caller's message must not
    echo the value.

    ``to_gid`` wraps without checking, so a full GID came out double-prefixed
    (Story 10.108). ``from_gid`` keeps only the last segment, so a GID of
    another type once deleted a different record of this type (Story 10.109).
    """
    prefix = to_gid(resource_type, "")
    text = str(value).strip()
    if text.startswith(prefix):
        if text == prefix:
            raise ValueError(error)
        return text
    if text.startswith("gid://") or "/" in text:
        raise ValueError(error)
    return to_gid(resource_type, text)


def product_gid(product_id: int | str) -> str:
    """Return the Product GID for a ``product_id`` given as a number or a GID.

    The rule is ``typed_gid``. A GID of another type never reaches Shopify as
    a product id, and a Product GID with nothing after the prefix is refused,
    as ``_resolve_product`` does.
    """
    return typed_gid("Product", product_id, INVALID_PRODUCT_ID_ERROR)


def numeric_typed_gid(resource_type: str, value: int | str, error: str) -> str:
    """Return the ``resource_type`` GID by the ``typed_gid`` rule, for a numeric id only.

    The id must also be ASCII digits, as a plain value or after the prefix.
    So an empty or blank value, ``abc`` and ``gid://shopify/<Type>/42/7`` raise
    ValueError(``error``). So the GID always has the same id that a preview shows.
    """
    gid = typed_gid(resource_type, value, error)
    numeric_id = gid.removeprefix(to_gid(resource_type, ""))
    if not (numeric_id.isascii() and numeric_id.isdigit()):
        raise ValueError(error)
    return gid


def webhook_subscription_gid(subscription_id: int | str) -> str:
    """Return the WebhookSubscription GID for a ``subscription_id`` (Story 10.109).

    A GID of another type once deleted the WebhookSubscription with the same
    number. The preview always shows the id that the call deletes.
    """
    return numeric_typed_gid("WebhookSubscription", subscription_id, INVALID_SUBSCRIPTION_ID_ERROR)


def order_gid(order_id: int | str) -> str:
    """Return the Order GID for an ``order_id`` (Story 10.110)."""
    return numeric_typed_gid("Order", order_id, INVALID_ORDER_ID_ERROR)


def inventory_item_gid(inventory_item_id: int | str) -> str:
    """Return the InventoryItem GID for an ``inventory_item_id`` (Story 10.110)."""
    return numeric_typed_gid("InventoryItem", inventory_item_id, INVALID_INVENTORY_ITEM_ID_ERROR)


def location_gid(location_id: int | str) -> str:
    """Return the Location GID for a ``location_id`` (Story 10.110)."""
    return numeric_typed_gid("Location", location_id, INVALID_LOCATION_ID_ERROR)


def from_gid(gid: str) -> str:
    """Return the trailing numeric id of a Shopify GID (``gid://shopify/T/123`` -> ``"123"``).

    The param is typed ``str`` — narrowed from ``str | None`` (Story 10.33 / Q6).
    The wider type let mypy silently accept ``str | None`` arguments, masking
    possible ``None``-propagation; the ``str`` contract makes mypy flag any such
    caller. (Audit found none today: the one genuinely typed ``str | None`` local,
    ``loc_gid``, is guarded with ``loc_gid or ""`` before the call; every other site
    passes ``Any`` — straight from ``dict[str, Any]`` payloads or via
    ``payload.get("id")`` locals, some additionally ``if``-guarded — see Q5.)

    The runtime ``if not gid`` guard is deliberately retained, NOT dead code: those
    ``Any``-typed sites can still pass ``None`` at runtime (Shopify returns ``id: null``
    on partial / permissions-trimmed fields), invisible to mypy, so the guard degrades
    them to ``""`` rather than raising ``AttributeError`` on ``None.split``.
    """
    if not gid:
        return ""
    return gid.split("/")[-1]
