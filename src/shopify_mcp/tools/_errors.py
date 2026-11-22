"""The text reply for a Shopify call that failed (Story 10.118).

A tool that lets a Shopify exception leave it shows the model
``Error executing tool <name>: <str(exc)>`` with ``isError=true``. These helpers
give the reply the project already uses in ``catalog_hygiene.py``: a text head
``Error reading <what> (<ExcType>): <text>`` for a read, or
``Error calling <mutationName> (<ExcType>): <text>`` for a write. The text is
capped, and the reminder is added when Shopify's own words are fenced in it.

``read_boundary`` is for a read tool only. A write tool guards each call by hand
(or through ``write_gate``), because a write that fails in transport may have
landed, and a read boundary would not say so.
"""

import functools
from collections.abc import Callable
from typing import Any

from shopify_mcp.errors import ShopifyTransportError
from shopify_mcp.tools._scrub import cap
from shopify_mcp.tools._untrusted import with_reminder

MAY_HAVE_LANDED = (
    "The write may have landed: Shopify can apply a mutation before the connection fails. "
    "Read the item back before you retry."
)


def read_error(what: str, exc: Exception) -> str:
    """The reply for a read that failed. A read cannot change anything."""
    return with_reminder(f"Error reading {what} ({type(exc).__name__}): {cap(str(exc))}")


def write_error(mutation: str, exc: Exception) -> str:
    """The reply for a write that failed.

    A ``ShopifyTransportError`` means the connection failed, and Shopify can
    apply a mutation before that happens. Only that error gets the note.
    """
    text = f"Error calling {mutation} ({type(exc).__name__}): {cap(str(exc))}"
    if isinstance(exc, ShopifyTransportError):
        text += "\n" + MAY_HAVE_LANDED
    return with_reminder(text)


def read_boundary(what: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorate a READ tool so any exception becomes ``read_error(what, exc)``.

    Put it under ``@server.tool(...)``. ``functools.wraps`` keeps the name, the
    docstring and the signature, so FastMCP registers the same tool.
    """

    def decorate(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                return read_error(what, exc)

        return wrapper

    return decorate
