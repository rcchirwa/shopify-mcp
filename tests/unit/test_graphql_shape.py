"""Self-tests for tests/support/graphql_shape.py (Story 10.119).

Every query here is a LITERAL, never a production one, so the guards keep their
teeth even if every query in shopify/queries/ is rewritten. The first five tests
moved here from test_products.py and test_publications.py (Story 10.84); they
now drive the shared walker instead of four local copies.
"""

import graphql
import pytest

from tests.support.admin_schema_snapshot import SNAPSHOT_PATH
from tests.support.graphql_shape import (
    ROOT_TYPE,
    SPREAD_PARENT_TYPES,
    expand_walked_spreads,
    field_names,
    walk_field,
)

CLEAN = """
query Q($handle: String!, $first: Int!, $after: String) {
  collectionByHandle(handle: $handle) {
    products(first: $first, after: $after) {
      nodes { id title }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# The decoy is unaliased and has no `after:` argument. The real field is aliased
# away. paginate() reads the response key `products`, so it would read the decoy.
DECOY = """
query Q($first: Int!, $after: String) {
  products(first: 1) { nodes { id } pageInfo { hasNextPage endCursor } }
  real: products(first: $first, after: $after) {
    nodes { id title }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def test_walker_rejects_an_aliased_terminal_field():
    """Story 10.84 AC4: the walker refuses an alias on the last step of a path.

    An alias is valid GraphQL that Shopify answers happily, and the response then
    carries the ALIAS as its key while the AST still reports the name. A
    name-matching walker stays satisfied while paginate() reads {} and returns
    zero nodes with capped=False, the same as an empty collection.
    """
    aliased = """
    query Q($first: Int!, $after: String) {
      x: products(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    with pytest.raises(AssertionError, match="aliased"):
        walk_field(aliased, ["products"])


def test_walker_rejects_an_aliased_intermediate_step():
    """The assertion covers EVERY step of the path, not only the last one.

    A one-element path cannot tell the two apart. The alias here sits on the
    FIRST of two steps for that reason.
    """
    aliased = """
    query Q($handle: String!, $first: Int!, $after: String) {
      c: collectionByHandle(handle: $handle) {
        products(first: $first, after: $after) {
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    with pytest.raises(AssertionError, match="aliased"):
        walk_field(aliased, ["collectionByHandle", "products"])


def test_walker_rejects_an_aliased_step_below_a_clean_connection():
    """The `nodes` step is walked like any other: `n: nodes` must fail."""
    aliased = "query Q { products { n: nodes { id } pageInfo { hasNextPage } } }"
    with pytest.raises(AssertionError, match="aliased"):
        walk_field(aliased, ["products", "nodes"])


def test_field_names_rejects_an_aliased_leaf():
    """The leaf guard has teeth too: the walked path is not the whole surface.

    paginate() reads hasNextPage out of pageInfo by key. Aliasing it stops the
    walk after one page and reports capped=False: a truncation reported as a
    complete answer, worse than the empty result an aliased connection gives.
    """
    aliased = "query Q { products { pageInfo { hn: hasNextPage endCursor } } }"
    page_info = walk_field(aliased, ["products", "pageInfo"]).selection_set
    assert page_info is not None
    with pytest.raises(AssertionError, match="aliased"):
        field_names(page_info, "pageInfo")


def test_walker_rejects_an_aliased_field_in_a_fragment_definition():
    """The root may be a fragment, not only an operation (publications reads one)."""
    aliased = """
    fragment F on Product {
      x: resourcePublications(first: 1) { nodes { id } }
    }
    """
    with pytest.raises(AssertionError, match="aliased"):
        walk_field(aliased, ["resourcePublications"])


def test_walker_rejects_a_decoy_ahead_of_an_aliased_real_field():
    """Story 10.119: the first match by name must not decide the verdict.

    Both Story 10.84 walkers took the first FieldNode with a matching name, so
    the unaliased decoy passed every guard while the real, aliased field was
    never looked at.
    """
    with pytest.raises(AssertionError, match="more than once"):
        walk_field(DECOY, ["products"])


def test_walker_rejects_an_unaliased_duplicate_after_the_real_field():
    """Order must not matter: the decoy may come after the real field."""
    query = "query Q { products { nodes { id } } products { pageInfo { hasNextPage } } }"
    with pytest.raises(AssertionError, match="more than once"):
        walk_field(query, ["products"])


def test_walker_rejects_a_duplicate_on_an_intermediate_step():
    """The duplicate rule applies at every step, not only the terminal one."""
    query = """
    query Q {
      collectionByHandle(handle: "a") { id }
      collectionByHandle(handle: "a") { products { nodes { id } } }
    }
    """
    with pytest.raises(AssertionError, match="more than once"):
        walk_field(query, ["collectionByHandle", "products"])


def test_walker_rejects_a_missing_field():
    with pytest.raises(AssertionError, match="no field 'products'"):
        walk_field("query Q { shop { id } }", ["products"])


def test_walker_ignores_a_fragment_spread_that_shares_the_name():
    """A FragmentSpreadNode has a `.name` and no `.alias`; it is not a field.

    Matching on `.name` alone would count the spread as a second `products` (or
    crash on its missing alias). Only a FieldNode is a candidate.
    """
    query = """
    query Q { products { nodes { id } pageInfo { hasNextPage } } ...products }
    fragment products on QueryRoot { shop { id } }
    """
    assert walk_field(query, ["products"]).name.value == "products"


def test_walker_accepts_a_clean_query_and_returns_the_field():
    field = walk_field(CLEAN, ["collectionByHandle", "products"])
    assert isinstance(field, graphql.FieldNode)
    assert field.name.value == "products"
    assert {a.name.value for a in field.arguments} == {"first", "after"}


def test_walker_accepts_a_selection_set_as_root():
    (operation,) = graphql.parse(CLEAN).definitions
    root = operation.selection_set
    assert walk_field(root, ["collectionByHandle"]).name.value == "collectionByHandle"


def test_walker_refuses_an_empty_path():
    """An empty path names no field, so there is nothing to return."""
    with pytest.raises(ValueError, match="empty path"):
        walk_field(CLEAN, [])


def test_field_names_returns_the_names_of_a_clean_leaf_set():
    page_info = walk_field(CLEAN, ["collectionByHandle", "products", "pageInfo"]).selection_set
    assert page_info is not None
    assert field_names(page_info, "pageInfo") == {"hasNextPage", "endCursor"}


def test_field_names_rejects_a_leaf_selected_twice():
    """A leaf set hides a repeat; the helper must not."""
    page_info = walk_field(
        "query Q { products { pageInfo { hasNextPage endCursor hasNextPage } } }",
        ["products", "pageInfo"],
    ).selection_set
    assert page_info is not None
    with pytest.raises(AssertionError, match="more than once"):
        field_names(page_info, "pageInfo")


def test_field_names_skips_fragment_spreads_and_inline_fragments():
    """Only fields count. A spread has no alias, and an inline fragment has no name."""
    query = """
    query Q { products { id ...Extra ... on Product { title } } }
    fragment Extra on Product { handle }
    """
    selection = walk_field(query, ["products"]).selection_set
    assert selection is not None
    assert field_names(selection, "products") == {"id"}


def test_walker_rejects_a_directive_on_a_walked_field():
    """@skip or @include can drop the response key, and paginate() then reads {}."""
    query = "query Q($b: Boolean!) { products(first: 1) @include(if: $b) { nodes { id } } }"
    with pytest.raises(AssertionError, match="directive"):
        walk_field(query, ["products"])


def test_field_names_rejects_a_directive_on_a_leaf():
    query = "query Q { products { pageInfo { hasNextPage @skip(if: true) endCursor } } }"
    page_info = walk_field(query, ["products", "pageInfo"]).selection_set
    assert page_info is not None
    with pytest.raises(AssertionError, match="directive"):
        field_names(page_info, "pageInfo")


def test_walker_walks_the_operation_not_a_fragment_listed_before_it():
    """A fragment ahead of the operation must not take the operation's place."""
    query = """
    fragment F on QueryRoot { products(first: 5) { nodes { id } } }
    query Q($first: Int!, $after: String) { x: products(first: $first, after: $after) { nodes { id } } }
    """
    with pytest.raises(AssertionError, match="aliased"):
        walk_field(query, ["products"])


def test_walker_refuses_a_mutation_root():
    with pytest.raises(AssertionError, match="not a mutation"):
        walk_field("mutation M { products(first: 1) { nodes { id } } }", ["products"])


def test_walker_refuses_text_with_two_operations():
    query = "query A { products { id } } query B { products { id } }"
    with pytest.raises(AssertionError, match="one operation"):
        walk_field(query, ["products"])


def test_walker_refuses_text_with_no_operation_and_two_fragments():
    query = "fragment A on Product { id } fragment B on Product { title }"
    with pytest.raises(AssertionError, match="single fragment"):
        walk_field(query, ["id"])


def test_walker_names_the_query_in_a_failure_message():
    with pytest.raises(AssertionError, match="GET_X: no field 'products'"):
        walk_field("query Q { shop { id } }", ["products"], where="GET_X")


# --- expand_walked_spreads (Story 10.120) -----------------------------------
#
# The connection that paginate() reads sits inside a named fragment for six call
# sites. GraphQL merges the fields of a spread into the parent object, so the
# response keys are the same as if the fields were written in place. The
# expander writes them in place on the walked levels only, and refuses every
# spread whose effect on those keys it cannot prove.

SPREAD = """
fragment Core on Product {
  id
  variants(first: $first, after: $after) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
query Q($id: ID!, $first: Int = 50, $after: String) {
  product(id: $id) {
    ...Core
  }
}
"""

WALK = ["product", "variants", "pageInfo"]


def _expand_and_walk(text: str, path: list[str] = WALK) -> graphql.FieldNode:
    return walk_field(expand_walked_spreads(text, path, "GET_X"), path, "GET_X")


def test_expander_puts_a_spread_field_where_the_walker_finds_it():
    """Without the expander the walker fails on this correct query (census, 2026-11-24)."""
    with pytest.raises(AssertionError, match="no field 'variants'"):
        walk_field(SPREAD, ["product", "variants"])
    page_info = _expand_and_walk(SPREAD)
    assert page_info.selection_set is not None
    assert field_names(page_info.selection_set, "pageInfo") == {"hasNextPage", "endCursor"}


def test_expander_keeps_the_arguments_of_a_field_from_a_fragment():
    variants = walk_field(
        expand_walked_spreads(SPREAD, WALK, "GET_X"), ["product", "variants"], "GET_X"
    )
    assert {a.name.value for a in variants.arguments} == {"first", "after"}


def test_expander_expands_a_spread_inside_a_spread():
    nested = """
    fragment Inner on Product { variants(first: $first, after: $after) { pageInfo { hasNextPage endCursor } } }
    fragment Outer on Product { id ...Inner }
    query Q($first: Int!, $after: String) { product(id: "1") { ...Outer } }
    """
    assert _expand_and_walk(nested).name.value == "pageInfo"


def test_expander_shows_a_decoy_beside_the_spread_to_the_walker():
    """Consultation Q4: a field beside the spread and one from the spread collide.

    Checked apart, the fragment is clean and the spread is present. Together the
    response has two candidates for one key, so the walker must refuse it.
    """
    decoy = SPREAD.replace("...Core", "preview: variants(first: 1) { nodes { id } }\n    ...Core")
    with pytest.raises(AssertionError, match="more than once"):
        _expand_and_walk(decoy)
    sibling = SPREAD.replace("...Core", "variants(first: 1) { nodes { id } }\n    ...Core")
    with pytest.raises(AssertionError, match="more than once"):
        _expand_and_walk(sibling)


def test_expander_refuses_a_spread_with_a_directive():
    """Consultation Q7: `@skip(if: true)` drops every key the spread adds."""
    skipped = SPREAD.replace("...Core", "...Core @skip(if: true)")
    with pytest.raises(AssertionError, match=r"GET_X: \.\.\.Core at product carries a directive"):
        _expand_and_walk(skipped)


def test_expander_refuses_an_unknown_fragment():
    unknown = SPREAD.replace("...Core", "...Missing")
    with pytest.raises(AssertionError, match="no fragment named 'Missing'"):
        _expand_and_walk(unknown)


def test_expander_refuses_two_fragments_with_one_name():
    twice = SPREAD + "\nfragment Core on Product { id }\n"
    with pytest.raises(AssertionError, match="'Core' is defined 2 times"):
        _expand_and_walk(twice)


def test_expander_refuses_a_fragment_cycle():
    cycle = """
    fragment A on Product { id ...B }
    fragment B on Product { title ...A }
    query Q { product(id: "1") { ...A } }
    """
    with pytest.raises(AssertionError, match="cycle"):
        _expand_and_walk(cycle)


def test_expander_refuses_a_fragment_on_another_type():
    """A spread on another type adds its fields only for some objects, or never."""
    other = SPREAD.replace("fragment Core on Product", "fragment Core on Collection")
    with pytest.raises(AssertionError, match="on Collection, but product returns Product"):
        _expand_and_walk(other)


def test_expander_refuses_a_spread_where_the_type_is_not_known():
    """The type of a level comes from SPREAD_PARENT_TYPES only. No row, no expansion."""
    unknown_level = """
    fragment Page on PageInfo { hasNextPage endCursor }
    query Q($first: Int!, $after: String) {
      product(id: "1") { variants(first: $first, after: $after) { pageInfo { ...Page } } }
    }
    """
    with pytest.raises(AssertionError, match=r"type of product\.variants\.pageInfo is not known"):
        _expand_and_walk(unknown_level)


def test_expander_refuses_an_inline_fragment_on_a_walked_level():
    """Consultation Q3: version one refuses every inline fragment on the path."""
    inline = SPREAD.replace("...Core", "...Core\n    ... on Product { title }")
    with pytest.raises(AssertionError, match="inline fragment at product"):
        _expand_and_walk(inline)


def test_expander_leaves_levels_off_the_path_alone():
    """A spread inside `nodes` is payload: paginate() does not read it by key."""
    payload = """
    fragment Row on ProductVariant { id sku }
    query Q($first: Int!, $after: String) {
      product(id: "1") {
        variants(first: $first, after: $after) {
          nodes { ...Row ... on ProductVariant { title } }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    assert _expand_and_walk(payload).name.value == "pageInfo"


def test_expander_refuses_text_with_no_operation():
    """Consultation Q2: a fragment alone must never count as a guarded query."""
    with pytest.raises(AssertionError, match="GET_X: expected one query operation, found 0"):
        expand_walked_spreads("fragment F on Product { id }", ["id"], "GET_X")


def test_expander_refuses_a_mutation():
    with pytest.raises(AssertionError, match="not a mutation"):
        expand_walked_spreads("mutation M { a { b } }", ["a"], "GET_X")


def test_spread_parent_types_match_the_schema_snapshot():
    """Every row of the type table is checked against the committed Admin schema."""
    schema = graphql.build_schema(SNAPSHOT_PATH.read_text(), assume_valid=True)
    assert schema.query_type is not None
    assert schema.query_type.name == ROOT_TYPE
    assert SPREAD_PARENT_TYPES, "the table is empty, so this test would check nothing"
    for (parent, field), expected in SPREAD_PARENT_TYPES.items():
        parent_type = schema.get_type(parent)
        assert isinstance(parent_type, graphql.GraphQLObjectType), parent
        returned = graphql.get_named_type(parent_type.fields[field].type)
        assert isinstance(returned, graphql.GraphQLObjectType), (parent, field)
        assert returned.name == expected, (parent, field, returned.name)
