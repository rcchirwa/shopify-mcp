"""Self-tests for tests/support/graphql_shape.py (Story 10.119).

Every query here is a LITERAL, never a production one, so the guards keep their
teeth even if every query in shopify/queries/ is rewritten. The first five tests
moved here from test_products.py and test_publications.py (Story 10.84); they
now drive the shared walker instead of four local copies.
"""

import graphql
import pytest

from tests.support.graphql_shape import field_names, walk_field

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
