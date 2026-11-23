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
named exception here. Today no production query does (checked on 72 queries).
"""

from collections import Counter
from collections.abc import Sequence

import graphql


def _selection_set_of(root: str | graphql.SelectionSetNode) -> graphql.SelectionSetNode:
    """Return `root`, or the selection set of the first definition in query text.

    The first definition is the operation, or a fragment when the text is only
    a fragment definition.
    """
    if isinstance(root, graphql.SelectionSetNode):
        return root
    definition = graphql.parse(root).definitions[0]
    selection_set = getattr(definition, "selection_set", None)
    assert isinstance(selection_set, graphql.SelectionSetNode), "definition has no selection set"
    return selection_set


def walk_field(root: str | graphql.SelectionSetNode, path: Sequence[str]) -> graphql.FieldNode:
    """Walk `path` down `root` and return the field at its end.

    `root` is query text (the first definition is walked) or a selection set. At
    every step exactly one FieldNode must carry the name, and it must be
    unaliased. This mirrors what client.paginate() does at runtime with
    connection_path, so a query whose pageInfo sits at the wrong nesting level
    fails here the way it would fail against the real API.

    The rule covers every step of every path a caller walks, not one field at one
    call site. The caller reads `.selection_set` for the next level and
    `.arguments` for the arguments.
    """
    if not path:
        raise ValueError("walk_field needs a non-empty path: an empty path names no field")
    selection_set: graphql.SelectionSetNode | None = _selection_set_of(root)
    node: graphql.FieldNode | None = None
    walked: list[str] = []
    for key in path:
        assert selection_set is not None, f"{'.'.join(walked)!r} has no selection set to walk"
        matches = [
            sel
            for sel in selection_set.selections
            if isinstance(sel, graphql.FieldNode) and sel.name.value == key
        ]
        assert matches, f"no field {key!r} in selection set"
        assert len(matches) == 1, (
            f"{key!r} is selected more than once ({len(matches)} times); paginate() reads "
            f"one response key, so the walked field must be the only one named {key!r}"
        )
        node = matches[0]
        assert node.alias is None, (
            f"{key!r} is aliased to {node.alias.value!r}; paginate() reads the "
            f"response key, which would be {node.alias.value!r}"
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

    A set also hides a name that appears twice, so a repeat is refused here.
    Fragment spreads and inline fragments are skipped: they are not fields.
    """
    fields = [sel for sel in selection_set.selections if isinstance(sel, graphql.FieldNode)]
    for sel in fields:
        assert sel.alias is None, (
            f"{where}: {sel.name.value!r} is aliased to {sel.alias.value!r}; the response "
            f"key would be {sel.alias.value!r}"
        )
    counts = Counter(sel.name.value for sel in fields)
    repeated = sorted(name for name, count in counts.items() if count > 1)
    assert not repeated, f"{where}: {repeated} selected more than once"
    return set(counts)
