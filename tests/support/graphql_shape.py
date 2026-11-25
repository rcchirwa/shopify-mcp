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

The two helpers do not follow fragments: a field reached only through a spread or
an inline fragment is not found, so a walked path that moves into a fragment
fails loudly with "no field". See the Story 10.84 ledger entry. Story 10.120 adds
expand_walked_spreads, which writes a named spread in place on a walked path when
it can prove the spread applies, and refuses every other case. Inline fragments on
a walked path stay unsupported.
"""

import dataclasses
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


# Story 10.120: the type each level of a walked path returns, so a named spread
# there can be proven to apply. Query text alone cannot tell the type of a field,
# so a row here is an assumption. test_spread_parent_types_match_the_schema_snapshot
# checks every row against the committed Admin schema snapshot. A spread at a
# level with no row is refused, not guessed.
ROOT_TYPE = "QueryRoot"
SPREAD_PARENT_TYPES: dict[tuple[str, str], str] = {
    (ROOT_TYPE, "product"): "Product",
    (ROOT_TYPE, "productByHandle"): "Product",
    (ROOT_TYPE, "order"): "Order",
}


def expand_walked_spreads(text: str, path: Sequence[str], where: str) -> graphql.SelectionSetNode:
    """Return the query's selection set with named spreads written in place on `path`.

    GraphQL merges the fields of a spread into the parent object, so the response
    keys are the same as if the fields were written in place. walk_field and
    field_names skip spreads, so a walked field that lives in a fragment is not
    found. This helper writes the fragment's fields in place at the root and at
    every level `path` walks into, and leaves every other level as it is.

    It has one job: give the walker the selections it must check. It matches no
    field and checks no alias; walk_field and field_names still do that. A spread
    is written in place only when its effect on the response keys is certain.
    Each other case fails and names the query and the level:

    * a spread with a directive (`@skip` or `@include` can drop all its keys);
    * an unknown fragment, a name defined twice, or a cycle;
    * a level whose type is not in SPREAD_PARENT_TYPES, or a fragment on another
      type (it adds its fields only for some objects, or never);
    * an inline fragment (not needed by any query today, so not supported).

    `text` must hold exactly one query operation. A fragment definition alone
    never stands in for a query.
    """
    definitions = graphql.parse(text).definitions
    operations = [d for d in definitions if isinstance(d, graphql.OperationDefinitionNode)]
    assert len(operations) == 1, f"{where}: expected one query operation, found {len(operations)}"
    fragment_nodes = [d for d in definitions if isinstance(d, graphql.FragmentDefinitionNode)]
    counts = Counter(f.name.value for f in fragment_nodes)
    for name, count in counts.items():
        assert count == 1, f"{where}: fragment {name!r} is defined {count} times"
    fragments = {f.name.value: f for f in fragment_nodes}
    return _expand_level(_selection_set_of(text), path, ROOT_TYPE, "", fragments, where)


def _expand_level(
    selection_set: graphql.SelectionSetNode,
    path: Sequence[str],
    level_type: str | None,
    at: str,
    fragments: dict[str, graphql.FragmentDefinitionNode],
    where: str,
) -> graphql.SelectionSetNode:
    """Write spreads in place at this level, then go one level down `path`."""
    level = at or ROOT_TYPE
    selections: list[graphql.SelectionNode] = []
    pending: list[tuple[graphql.SelectionNode, tuple[str, ...]]] = [
        (sel, ()) for sel in selection_set.selections
    ]
    while pending:
        sel, stack = pending.pop(0)
        if isinstance(sel, graphql.InlineFragmentNode):
            raise AssertionError(
                f"{where}: inline fragment at {level}; the guard cannot tell which keys it adds"
            )
        if not isinstance(sel, graphql.FragmentSpreadNode):
            selections.append(sel)
            continue
        name = sel.name.value
        assert not sel.directives, (
            f"{where}: ...{name} at {level} carries a directive; @skip or @include can "
            f"drop every key it adds"
        )
        assert name in fragments, f"{where}: no fragment named {name!r} (spread at {level})"
        assert name not in stack, f"{where}: fragment cycle {' -> '.join((*stack, name))}"
        assert level_type is not None, (
            f"{where}: ...{name}: the type of {level} is not known (no row in "
            f"SPREAD_PARENT_TYPES), so the guard cannot prove the fragment applies"
        )
        condition = fragments[name].type_condition.name.value
        assert condition == level_type, (
            f"{where}: ...{name} is on {condition}, but {level} returns {level_type}"
        )
        spread = [(inner, (*stack, name)) for inner in fragments[name].selection_set.selections]
        pending[:0] = spread
    if path:
        head, rest = path[0], path[1:]
        child_at = f"{at}.{head}" if at else head
        child_type = SPREAD_PARENT_TYPES.get((level_type, head)) if level_type else None
        selections = [
            dataclasses.replace(
                sel,
                selection_set=_expand_level(
                    sel.selection_set, rest, child_type, child_at, fragments, where
                ),
            )
            if isinstance(sel, graphql.FieldNode)
            and sel.name.value == head
            and sel.selection_set is not None
            else sel
            for sel in selections
        ]
    return graphql.SelectionSetNode(selections=tuple(selections))


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
