"""The one rule for the ``product_id`` / ``handle`` identifier pair.

Story 10.65 made supplying **both** identifiers an error on the six
product-resolving tools in ``tools/catalog_hygiene.py``: the old
``product_id``-wins precedence *silently discarded the handle*, so a caller who
named one product by id and a different one by handle got the first with
nothing indicating the second was ignored — a wrong-product write on a mutator.
Story 10.68 (``T-10.65-refuse-both-fanout``) extends that rule to the seven
tools in ``tools/products.py`` and ``tools/publications.py`` that took the same
pair and still applied the same silent precedence — four reads and **three
writes** (``publish_product_to_channels``, ``unpublish_product_from_channels``,
``set_product_publications``). The Trello card said "four of the seven are
writes" while naming three; the count is three, and a review pass caught the
miscount propagating into this docstring and the ledger.

This module is the **single definition** of that rule, deliberately promoted
out of ``operations/products.py``'s private ``_require_discriminator`` so
``operations/publications.py`` — which had no shared guard at all, inlining
``if product_id:`` — enforces the same thing rather than a parallel copy that
can drift. It lives under ``shopify/`` rather than ``tools/`` because
``shopify/`` must never import ``tools/`` (pinned by
``tests/architecture/test_layering.py``), and the operations layer is where the
guard has to sit for the rule to hold for non-MCP callers (CLI, scripts) too.

``tools/_product_resolver.py`` imports :data:`BOTH_IDENTIFIERS_ERROR` from here
so catalog_hygiene's refusal and these seven tools' refusal state the same
claim from one constant rather than two hand-kept copies. They are not
byte-identical, and the docs should not say they are: ``_resolve_product``
appends a ``_cap``-bounded ``— got product_id=… and handle=…`` tail, and the
seven tools render the constant as a plain sentence. What is genuinely shared
is the constant **and** the predicate below — the latter matters more, since
two layers agreeing on the wording while disagreeing on *what counts as
supplied* would be one rule in name only.

**The message does not echo the caller's values**, so there is nothing here to
bound with ``_scrub.cap`` — which is just as well, since ``cap`` lives under
``tools/`` and is unreachable across the layering boundary. The reflection
bound stays where values are actually echoed: ``_resolve_product``'s tail.

**Two enforcement points, on purpose.** The guards here are what make the rule
hold for non-MCP callers (CLI, scripts) and what would catch a future tool that
forgets. They are not the message path: each of the seven tools calls
``tools/_product_resolver.identifier_error`` as its *first* statement instead of
inheriting a raised ``ValueError`` from underneath. That is not belt-and-braces
for its own sake — ``tools/publications.py`` reads sales channels over the
network before it reaches a product, so a refusal inherited from the operations
layer arrived one round-trip late, could be masked entirely by a channel error,
and was rendered by a generic handler that appended an "app needs reinstall"
scope hint to what is purely an argument mistake.
"""

BOTH_IDENTIFIERS_ERROR = "Supply product_id or handle, not both"


def is_supplied(value: object) -> bool:
    """Did the caller actually supply this identifier?

    The predicate half of the rule, and the reason "one rule" is true at the
    level that matters: *what counts as supplied*. A blank or whitespace-only
    string is **absent**, not ambiguous — a client that fills unused schema
    fields with ``""`` (or ``"  "``) alongside a real identifier is making an
    unambiguous call and must not be refused.

    Typed ``object``, not ``str``, on purpose: a client that ignores the schema
    can send an int or a null, and anything non-string-and-non-None counts as
    **supplied** so it routes somewhere it gets rejected rather than silently
    discarded. That is the same load-bearing property Story 10.65 pinned in
    ``catalog_hygiene`` — treating a malformed ``product_id`` as absent would
    silently invert the channel choice and resolve by ``handle`` instead, which
    is the wrong-target write the whole rule exists to stop.

    ``tools/catalog_hygiene.py::_is_supplied`` is an alias of this function, so
    the six catalog_hygiene tools and the seven Story 10.68 tools agree on the
    predicate rather than each carrying a lookalike.
    """
    if value is None:
        return False
    return bool(value.strip()) if isinstance(value, str) else True


def both_identifiers_supplied(product_id: object, handle: object) -> bool:
    """True when the pair is ambiguous — the non-raising form of the rule.

    Exists so the tool layer can ask the question without catching an exception
    it caused itself, and so a tool can refuse *before* doing any other work
    (`tools/publications.py` resolves sales channels over the network, so it has
    to test the pair ahead of that rather than inherit the refusal from the
    operations layer underneath it).
    """
    return is_supplied(product_id) and is_supplied(handle)


def reject_both_identifiers(product_id: str, handle: str) -> None:
    """Raise ``ValueError`` when *both* identifiers are supplied.

    The operations-layer enforcement point. Deliberately only the both-supplied
    half of the rule: the neither-supplied case is left to each caller because
    the two operations layers answer it differently today and unifying them is
    a separate contract decision — ``operations/products.py`` raises,
    ``operations/publications.py`` returns ``(None, [], False)``.
    """
    if both_identifiers_supplied(product_id, handle):
        raise ValueError(BOTH_IDENTIFIERS_ERROR)
