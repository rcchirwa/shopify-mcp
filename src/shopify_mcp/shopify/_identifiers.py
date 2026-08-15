"""The one rule for the ``product_id`` / ``handle`` identifier pair.

Story 10.65 made supplying **both** identifiers an error on the six
product-resolving tools in ``tools/catalog_hygiene.py``: the old
``product_id``-wins precedence *silently discarded the handle*, so a caller who
named one product by id and a different one by handle got the first with
nothing indicating the second was ignored — a wrong-product write on a mutator.
Story 10.68 (``T-10.65-refuse-both-fanout``) extends that rule to the seven
tools in ``tools/products.py`` and ``tools/publications.py`` that took the same
pair and still applied the same silent precedence, four of them writes.

This module is the **single definition** of that rule, deliberately promoted
out of ``operations/products.py``'s private ``_require_discriminator`` so
``operations/publications.py`` — which had no shared guard at all, inlining
``if product_id:`` — enforces the same thing rather than a parallel copy that
can drift. It lives under ``shopify/`` rather than ``tools/`` because
``shopify/`` must never import ``tools/`` (pinned by
``tests/architecture/test_layering.py``), and the operations layer is where the
guard has to sit for the rule to hold for non-MCP callers (CLI, scripts) too.

``tools/_product_resolver.py`` imports :data:`BOTH_IDENTIFIERS_ERROR` from here
so catalog_hygiene's refusal and these seven tools' refusal are the same
sentence, not two that merely resemble each other.

**The message does not echo the caller's values.** ``_resolve_product`` appends
a ``_cap``-bounded ``got product_id=… and handle=…`` tail, but ``_scrub.cap``
lives under ``tools/`` and is unreachable from here across the layering
boundary. Rather than duplicate the cap or reflect unbounded caller text at
this layer, the operations-layer message stays value-free; each tool renders it
through its own error path, where the reflection bound already applies.
"""

BOTH_IDENTIFIERS_ERROR = "Supply product_id or handle, not both"


def reject_both_identifiers(product_id: str, handle: str) -> None:
    """Raise ``ValueError`` when *both* identifiers are supplied.

    Deliberately only the both-supplied half of the rule. The neither-supplied
    case is left to each caller because the two operations layers answer it
    differently today and unifying them is not this story's business:
    ``operations/products.py`` raises, ``operations/publications.py`` returns
    ``(None, [], False)``.
    """
    if product_id and handle:
        raise ValueError(BOTH_IDENTIFIERS_ERROR)
