"""GID encode/decode utilities shared across all tool modules.

The implementations now live in ``shopify._ids`` (the canonical home, so the
``shopify`` domain layer can use GID coercion without importing ``tools``). This
module re-exports them to keep every existing ``from tools._gid import ...``
call site working unchanged. See Story 10.23 / A5 (Q3-helper reconciliation).
"""

from shopify._ids import from_gid, to_gid

__all__ = ["from_gid", "to_gid"]
