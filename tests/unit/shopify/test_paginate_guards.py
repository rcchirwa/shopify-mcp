"""Parse guard for every client.paginate() call site in src/ (Story 10.120).

paginate() reads its response by key: each step of `connection_path`, then
`nodes`, then `pageInfo.hasNextPage` and `pageInfo.endCursor`. FakeClient never
parses the GraphQL, so an offline test sees a broken query only through a parse
guard that walks the query the same way.

The table of call sites is not written by hand. `discover_paginate_calls` reads
src/ with `ast`, so a new call site gets a row with no edit here. A call that it
cannot turn into a row (the query is not a module-level name, the
connection_path is not a literal, or paginate is referenced without a call)
fails `test_every_paginate_reference_resolves_to_a_row` and names its file and
line.

The six queries that older tests already guard (test_products.py,
test_publications.py) get this guard too. Those tests stay as they are; the
overlap is on purpose.
"""

import re
import sys
from pathlib import Path

import pytest

from tests.support.paginate_sites import (
    SRC_ROOT,
    CallSite,
    check_paginate_query,
    discover_paginate_calls,
)

SITES, PROBLEMS = discover_paginate_calls(SRC_ROOT, "shopify_mcp")


def test_every_paginate_reference_resolves_to_a_row():
    assert not PROBLEMS, "\n".join(PROBLEMS)


def test_discovery_finds_at_least_the_census():
    """The census on 0c85f74 (2026-11-24): 22 call sites, 18 distinct queries.

    A floor, not a pin: a new call site needs no edit here. The floor proves
    that discovery ran over the real tree and did not pass on an empty table.
    """
    assert len(SITES) >= 22
    assert len({site.query_name for site in SITES}) >= 18


@pytest.mark.parametrize("site", SITES, ids=lambda s: f"{s.where}:{s.query_name}")
def test_paginate_query_matches_what_paginate_reads(site: CallSite):
    check_paginate_query(site.query_text, site.connection_path, site.label)


# --- the guard on literal queries ----------------------------------------------
#
# Every query below is a LITERAL, so the guard keeps its teeth if every
# production query is rewritten.

CLEAN = """
query Q($handle: String!, $first: Int!, $after: String) {
  collectionByHandle(handle: $handle) {
    products(first: $first, after: $after) {
      nodes { id }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
PATH = ["collectionByHandle", "products"]

FRAGMENT_CLEAN = """
fragment Core on Product {
  variants(first: $first, after: $after) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
query Q($id: ID!, $first: Int = 50, $after: String) { product(id: $id) { ...Core } }
"""


def _check(text: str, path: list[str] = PATH) -> None:
    check_paginate_query(text, path, "GET_X (src/x.py:1)")


def test_guard_accepts_a_clean_query():
    _check(CLEAN)


def test_guard_accepts_a_connection_inside_a_fragment():
    _check(FRAGMENT_CLEAN, ["product", "variants"])


FAILURE_CLASSES = [
    (
        "pageInfo one level too high",
        "      pageInfo { hasNextPage endCursor }\n    }",
        "    }\n    pageInfo { hasNextPage endCursor }",
        "no field 'pageInfo'",
    ),
    ("aliased connection", "    products(", "    p: products(", "aliased"),
    ("aliased leaf", "{ hasNextPage endCursor }", "{ more: hasNextPage endCursor }", "aliased"),
    ("missing endCursor", "{ hasNextPage endCursor }", "{ hasNextPage }", "endCursor"),
    ("missing nodes", "nodes { id }", "edges { node { id } }", "nodes"),
    ("dropped after: $after", ", after: $after)", ")", "after: \\$after"),
    ("after bound to another variable", "after: $after)", "after: $handle)", "after: \\$after"),
    ("literal first", "first: $first,", "first: 5,", "first: \\$first"),
    ("undeclared $after", ", $after: String)", ")", "\\$after is declared 0 times"),
    ("undeclared $first", "$first: Int!, ", "", "\\$first is declared 0 times"),
    (
        "$first declared twice",
        "$first: Int!, ",
        "$first: Int!, $first: Int, ",
        "\\$first is declared 2 times",
    ),
    ("after passed twice", "after: $after)", "after: $after, after: $after)", "after"),
    (
        "duplicate walked field",
        "  collectionByHandle(handle: $handle) {",
        "  collectionByHandle(handle: $handle) { id }\n  collectionByHandle(handle: $handle) {",
        "more than once",
    ),
    (
        "decoy ahead of an aliased real field",
        "    products(first: $first, after: $after) {",
        "    products(first: 1) { nodes { id } pageInfo { hasNextPage endCursor } }\n"
        "    real: products(first: $first, after: $after) {",
        "more than once",
    ),
    (
        "directive on the connection",
        "after: $after) {",
        "after: $after) @include(if: true) {",
        "directive",
    ),
    # Review round 1: the declared types and the backward arguments.
    (
        "non-null $after",
        "$after: String)",
        "$after: String!)",
        "\\$after must be String \\(nullable",
    ),
    ("$after of another type", "$after: String)", "$after: Int)", "\\$after must be String"),
    ("$first of another type", "$first: Int!,", "$first: String!,", "\\$first must be Int"),
    ("$first as a list", "$first: Int!,", "$first: [Int],", "\\$first must be Int"),
    ("last on the connection", "after: $after)", "after: $after, last: 5)", "passes last:"),
    ("before on the connection", "after: $after)", 'after: $after, before: "x")', "passes before:"),
]


@pytest.mark.parametrize(
    ("label", "old", "new", "message"),
    [pytest.param(*case, id=case[0]) for case in FAILURE_CLASSES],
)
def test_guard_rejects_each_failure_class(label: str, old: str, new: str, message: str):
    assert old in CLEAN, f"{label}: the mutation does not apply"
    bad = CLEAN.replace(old, new, 1)
    with pytest.raises(AssertionError, match=message):
        _check(bad)


def test_guard_rejects_a_skipped_spread_on_the_path():
    """Consultation Q7: the spread is present but adds no key."""
    skipped = FRAGMENT_CLEAN.replace("...Core", "...Core @skip(if: true)")
    with pytest.raises(AssertionError, match="directive"):
        _check(skipped, ["product", "variants"])


def test_guard_rejects_a_fragment_alone():
    fragment_only = FRAGMENT_CLEAN.split("query Q")[0]
    with pytest.raises(AssertionError, match="one query operation"):
        _check(fragment_only, ["variants"])


def test_a_failure_names_the_query_and_the_call_site():
    with pytest.raises(AssertionError, match=r"GET_X \(src/x\.py:1\): no field 'pageInfo'"):
        _check(CLEAN.replace("pageInfo", "info"))


# --- discovery on a scratch package ----------------------------------------------


@pytest.fixture(autouse=True)
def _forget_scratchpkg():
    """Drop the scratch package from sys.modules before and after each test.

    Review round 1: monkeypatch.delitem records nothing for an absent key, so the
    module a test imported stayed in sys.modules for the rest of the session.
    """
    for name in ("scratchpkg.mod", "scratchpkg"):
        sys.modules.pop(name, None)
    yield
    for name in ("scratchpkg.mod", "scratchpkg"):
        sys.modules.pop(name, None)


def _package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Write `scratchpkg/mod.py` with `body` and make it importable."""
    root = tmp_path / "scratchpkg"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "mod.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    return root


def test_a_new_call_site_gets_a_row_and_a_bad_query_fails_by_name(tmp_path, monkeypatch):
    """Step 5: a new call site needs no table edit, and its failure names it."""
    root = _package(
        tmp_path,
        monkeypatch,
        'Q = "query Q($first: Int!, $after: String) { products(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }"\n'
        "\n"
        "def read(client):\n"
        '    return client.paginate(Q, {}, connection_path=["products"])\n',
    )
    sites, problems = discover_paginate_calls(root, "scratchpkg")
    assert problems == []
    (site,) = sites
    assert site.where == "scratchpkg/mod.py:4"
    assert (site.query_name, site.connection_path) == ("Q", ["products"])
    with pytest.raises(
        AssertionError, match=re.escape(f"Q ({site.where}): ") + r".*after: \$after"
    ):
        check_paginate_query(site.query_text, site.connection_path, site.label)


Q_OK = "Q = 'query Q { a }'\n"
CALL_Q = "c.paginate(Q, {}, connection_path=['a'])"
ONCE = "Q must be bound once, at module top level"
UNRESOLVED = [
    (
        "local name",
        "def f(c, q):\n    return c.paginate(q, {}, connection_path=['a'])\n",
        "q must be bound once, at module top level (found 1 binding(s), top level: False)",
    ),
    ("parameter hides a constant", Q_OK + f"def f(c, Q):\n    return {CALL_Q}\n", ONCE),
    (
        "local assignment hides a constant",
        Q_OK + f"def f(c):\n    Q = 'x'\n    return {CALL_Q}\n",
        ONCE,
    ),
    # Review round 1: binding forms that are not an `ast.Name` store.
    (
        "local import hides a constant",
        Q_OK + f"def f(c):\n    from os import sep as Q\n    return {CALL_Q}\n",
        ONCE,
    ),
    (
        "except-as hides a constant",
        Q_OK
        + f"def f(c):\n    try:\n        pass\n    except Exception as Q:\n        return {CALL_Q}\n",
        ONCE,
    ),
    (
        "global rebinds the constant",
        Q_OK + f"def f(c):\n    global Q\n    Q = 'x'\n    return {CALL_Q}\n",
        ONCE,
    ),
    (
        "module-level comprehension target",
        Q_OK + f"rows = [{CALL_Q} for c, Q in []]\n",
        ONCE,
    ),
    (
        "class body hides a constant",
        Q_OK + f"class K:\n    Q = 'x'\n    def f(self, c):\n        return {CALL_Q}\n",
        ONCE,
    ),
    (
        "match capture hides a constant",
        Q_OK + f"def f(c, v):\n    match v:\n        case Q:\n            return {CALL_Q}\n",
        ONCE,
    ),
    (
        "constant bound twice at top level",
        Q_OK + Q_OK + f"def f(c):\n    return {CALL_Q}\n",
        ONCE,
    ),
    (
        "unknown name",
        "def f(c):\n    return c.paginate(MISSING, {}, connection_path=['a'])\n",
        "MISSING must be bound once",
    ),
    (
        "name bound to a number",
        "Q = 5\n" + f"def f(c):\n    return {CALL_Q}\n",
        "Q is not a string",
    ),
    (
        "path is a variable",
        Q_OK + "def f(c, p):\n    return c.paginate(Q, {}, connection_path=p)\n",
        "connection_path",
    ),
    ("no path", Q_OK + "def f(c):\n    return c.paginate(Q, {})\n", "connection_path"),
    (
        "path holds a number",
        Q_OK + "def f(c):\n    return c.paginate(Q, {}, connection_path=['a', 1])\n",
        "connection_path",
    ),
    ("no arguments", "def f(c):\n    return c.paginate()\n", "query is not a module-level name"),
    ("reference, no call", "def f(c):\n    walk = c.paginate\n    return walk\n", "not called"),
    ("getattr", "def f(c):\n    return getattr(c, 'paginate')\n", "string 'paginate'"),
    # Story 10.121: a name built from string literals with `+`.
    (
        "getattr of a concatenated name",
        "def f(c):\n    return getattr(c, 'pagi' + 'nate')\n",
        "'paginate' built from string literals",
    ),
    (
        "three-part concatenation",
        "def f(c):\n    return getattr(c, 'pa' + 'gi' + 'nate')\n",
        "'paginate' built from string literals",
    ),
]


@pytest.mark.parametrize(
    ("body", "message"), [pytest.param(b, m, id=label) for label, b, m in UNRESOLVED]
)
def test_an_unresolved_call_site_is_a_problem_that_names_it(tmp_path, monkeypatch, body, message):
    root = _package(tmp_path, monkeypatch, body)
    sites, problems = discover_paginate_calls(root, "scratchpkg")
    assert sites == []
    (problem,) = problems
    assert problem.startswith("scratchpkg/mod.py:")
    assert message in problem


DIRECT_CALL = (
    'Q = "query Q($first: Int!, $after: String) { products(first: $first, after: $after) '
    '{ nodes { id } pageInfo { hasNextPage endCursor } } }"\n'
    "\n"
    "def read(client):\n"
    '    return client.paginate(Q, {}, connection_path=["products"])\n'
)


def test_a_computed_lookup_of_a_discovered_call_becomes_a_problem_at_its_line(
    tmp_path, monkeypatch
):
    """Story 10.121: change ONLY the method lookup of a call that discovery finds.

    Before Story 10.121 the changed call gave no row AND no problem: a silent
    miss. "No row" alone would pass on that defect, so the problem is required,
    at the changed file and line.
    """
    root = _package(tmp_path, monkeypatch, DIRECT_CALL)
    sites, problems = discover_paginate_calls(root, "scratchpkg")
    assert (len(sites), problems) == (1, [])

    computed = DIRECT_CALL.replace("client.paginate(", 'getattr(client, "pagi" + "nate")(')
    assert computed != DIRECT_CALL
    (root / "mod.py").write_text(computed)
    sites, problems = discover_paginate_calls(root, "scratchpkg")
    assert sites == []
    assert len(problems) == 1
    assert problems[0].startswith("scratchpkg/mod.py:4: ")
    assert "'paginate' built from string literals" in problems[0]


def test_a_concatenation_that_is_not_paginate_is_not_a_problem(tmp_path, monkeypatch):
    """The check matches the whole value, not a prefix: 'pagi' + 'nation' is another name."""
    root = _package(tmp_path, monkeypatch, "def f(c):\n    return getattr(c, 'pagi' + 'nation')\n")
    assert discover_paginate_calls(root, "scratchpkg") == ([], [])
