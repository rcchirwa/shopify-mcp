"""GID encode/decode utilities — canonical home under ``shopify``.

These are pure functions with no dependencies, needed by ``shopify.operations``
(which must not import from ``tools``). ``tools/_gid.py`` re-exports them so the
many existing ``from tools._gid import ...`` call sites keep working unchanged.
"""


def to_gid(resource_type: str, numeric_id: int | str) -> str:
    return f"gid://shopify/{resource_type}/{numeric_id}"


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
