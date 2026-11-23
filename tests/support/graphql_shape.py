"""Walk a parsed GraphQL query the way client.paginate() reads its response.

Two helpers enforce one rule (Stories 10.84 and 10.119): a field that paginate()
reads must be selected ONCE and UNALIASED.

paginate() descends the RESPONSE by key, and the response key of a field is its
alias when one is present and its name otherwise. So a guard that finds fields
by name only mirrors the runtime when two things hold:

* No walked field carries an alias. An alias is valid GraphQL that Shopify
  answers happily. The AST still reports the name, every other assertion still
  holds, and paginate() reads {} and returns zero nodes with capped=False.
* The name is unique in its selection set. Before Story 10.119 each walker took
  the FIRST field with a matching name. An unaliased decoy `products(first: 1)`
  placed before an aliased real `real: products(first: $first, after: $after)`
  passed every guard, and paginate() read the decoy too.

Only a FieldNode is a candidate. FragmentSpreadNode also has a `.name` and has no
`.alias`, so a duck-typed match would raise AttributeError the moment a fragment
shared a walked field's name.

A query that must select a walked field twice on purpose needs an explicit,
named exception here. No production query did when this was written (Story 10.119
checked every query constant in shopify/queries/).

Fragments are out of scope: a field reached only through a spread or an inline
fragment is not found, so a walked path that moves into a fragment fails loudly
with "no field". See the Story 10.84 ledger entry.
"""

from collections import Counter
from collections.abc import Sequence

import graphql


def _selection_set_of(root: str | graphql.SelectionSetNode) -> graphql.SelectionSetNode:
    """Return `root`, or the selection set that query text asks paginate() to read.

    Text with an operation must hold exactly one, and it must be a query: a
    fragment listed ahead of the operation must not be walked in its place. Text
    with no operation must be one fragment definition (the shared Product
    fragment is read that way).
    """
    if isinstance(root, graphql.SelectionSetNode):
        return root
    definitions = graphql.parse(root).definitions
    operations = [d for d in definitions if isinstance(d, graphql.OperationDefinitionNode)]
    if operations:
        assert len(operations) == 1, f"expected one operation, found {len(operations)}"
        (operation,) = operations
        assert operation.operation is graphql.OperationType.QUERY, (
            f"paginate() reads a query, not a {operation.operation.value}"
        )
        return operation.selection_set
    assert len(definitions) == 1, "text with no operation must be a single fragment definition"
    (fragment,) = definitions
    assert isinstance(fragment, graphql.FragmentDefinitionNode), "definition is not a fragment"
    return fragment.selection_set


def walk_field(
    root: str | graphql.SelectionSetNode, path: Sequence[str], where: str = ""
) -> graphql.FieldNode:
    """Walk `path` down `root` and return the field at its end.

    `root` is query text (the first definition is walked) or a selection set. At
    every step exactly one FieldNode must carry the name, and it must be
    unaliased and carry no directive (`@skip` or `@include` can drop the response
    key). `where` names the query in a failure message. This mirrors what client.paginate() does at runtime with
    connection_path, so a query whose pageInfo sits at the wrong nesting level
    fails here the way it would fail against the real API.

    The rule covers every step of every path a caller walks, not one field at one
    call site. The caller reads `.selection_set` for the next level and
    `.arguments` for the arguments.
    """
    if not path:
        raise ValueError("walk_field needs a non-empty path: an empty path names no field")
    prefix = f"{where}: " if where else ""
    selection_set: graphql.SelectionSetNode | None = _selection_set_of(root)
    node: graphql.FieldNode | None = None
    walked: list[str] = []
    for key in path:
        assert selection_set is not None, (
            f"{prefix}{'.'.join(walked)!r} has no selection set to walk"
        )
        matches = [
            sel
            for sel in selection_set.selections
            if isinstance(sel, graphql.FieldNode) and sel.name.value == key
        ]
        assert matches, f"{prefix}no field {key!r} in selection set"
        assert len(matches) == 1, (
            f"{prefix}{key!r} is selected more than once ({len(matches)} times); paginate() "
            f"reads one response key, so the walked field must be the only one named {key!r}"
        )
        node = matches[0]
        assert node.alias is None, (
            f"{prefix}{key!r} is aliased to {node.alias.value!r}; paginate() reads the "
            f"response key, which would be {node.alias.value!r}"
        )
        assert not node.directives, (
            f"{prefix}{key!r} carries a directive; @skip or @include can drop its response key"
        )
        walked.append(key)
        selection_set = node.selection_set
    assert node is not None
    return node


def field_names(selection_set: graphql.SelectionSetNode, where: str) -> set[str]:
    """Return the field names in `selection_set`, each selected once and unaliased.

    The leaf half of the rule walk_field enforces. paginate() reads `nodes` and
    `pageInfo` out of a connection by literal key, and `hasNextPage` and
    `endCursor` out of that pageInfo the same way. A set of names built straight
    off these selections is blind in exactly the way the walker was. Aliasing
    `hasNextPage` is the worst case: paginate() reads None, stops after page 1
    and reports capped=False, so a truncation looks like a complete answer.

    A set also hides a name that appears twice, so a repeat is refused here. That
    is stricter than the runtime (GraphQL merges equal fields) on purpose: it
    keeps the leaf rule the same as the walker's.
    Fragment spreads and inline fragments are skipped: they are not fields.
    """
    fields = [sel for sel in selection_set.selections if isinstance(sel, graphql.FieldNode)]
    for sel in fields:
        assert sel.alias is None, (
            f"{where}: {sel.name.value!r} is aliased to {sel.alias.value!r}; the response "
            f"key would be {sel.alias.value!r}"
        )
        assert not sel.directives, (
            f"{where}: {sel.name.value!r} carries a directive; @skip or @include can drop it"
        )
    counts = Counter(sel.name.value for sel in fields)
    repeated = sorted(name for name, count in counts.items() if count > 1)
    assert not repeated, f"{where}: {repeated} selected more than once"
    return set(counts)
