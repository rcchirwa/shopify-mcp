"""GID encode/decode utilities — canonical home under ``shopify``.

These are pure functions with no dependencies, needed by ``shopify.operations``
(which must not import from ``tools``). ``tools/_gid.py`` re-exports them so the
many existing ``from tools._gid import ...`` call sites keep working unchanged.
"""


def to_gid(resource_type: str, numeric_id: int | str) -> str:
    return f"gid://shopify/{resource_type}/{numeric_id}"


def from_gid(gid: str | None) -> str:
    # Tolerate None/empty so callers can pass `obj.get("id")` or
    # `obj.get("id", "")` without a pre-check — Shopify responses may
    # return `id: null` on partial/permissions-trimmed fields, and the
    # dict .get(..., "") default doesn't catch the "key present, value None" case.
    if not gid:
        return ""
    return gid.split("/")[-1]
