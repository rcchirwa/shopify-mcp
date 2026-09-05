"""
Offline guard that FakeClient.paginate() still mirrors ShopifyClient.paginate()
(Story 10.78 / T-10.6-paginate-vanish).

``tests/support/fake_client.py`` calls its ``paginate`` a "Mirror of
ShopifyClient.paginate()", and every operations-layer test in the repo walks
pages through it rather than through the real client. That makes a divergence
invisible in the worst way: the offline suite goes on passing while it stops
exercising the control flow that actually ships. Story 10.78 was the second
time the pair had to be brought back into step by hand, so the docstring's
promise is pinned here instead of trusted.

The two are deliberately NOT identical — the real one logs, the fake one does
not, and their annotations differ (``list[dict[str, Any]]`` vs ``list[Any]``).
So this compares the DECISIONS rather than the text: the branch conditions and
the loop headers, in order. That is exactly what went out of step — a missing
``if page > 0 and not connection`` branch — and it is what an operations-layer
test can never notice on its own.

Comparing conditions rather than whole bodies is a deliberate trade: it will
not catch a divergence that keeps every branch but changes what happens inside
one. Test-level coverage of the fake's behaviour (see
tests/unit/test_paginate.py) is what covers that half.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/architecture/test_paginate_mirror.py -v
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL = _REPO_ROOT / "src" / "shopify_mcp" / "client.py"
_FAKE = _REPO_ROOT / "tests" / "support" / "fake_client.py"


def _paginate_def(path: Path) -> tuple[ast.FunctionDef, str]:
    """The `def paginate` node in `path`, plus that file's source.

    Raises if there is not exactly one — a rename or a second definition must
    fail loudly rather than leave this module silently checking nothing.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "paginate"
    ]
    assert len(found) == 1, (
        f"expected exactly one `def paginate` in {path.name}, found {len(found)}"
    )
    return found[0], source


def _decisions(node: ast.FunctionDef, source: str) -> list[str]:
    """Loop headers and branch conditions inside `node`, in source order.

    Nested function definitions are not walked — neither implementation has
    one today, and stepping into one would compare unrelated logic.
    """
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.If):
            out.append(f"if {ast.get_source_segment(source, child.test)}")
        elif isinstance(child, ast.For):
            target = ast.get_source_segment(source, child.target)
            iterator = ast.get_source_segment(source, child.iter)
            out.append(f"for {target} in {iterator}")
    return out


def test_fake_paginate_makes_the_same_decisions_as_the_real_one():
    real_node, real_src = _paginate_def(_REAL)
    fake_node, fake_src = _paginate_def(_FAKE)
    assert _decisions(fake_node, fake_src) == _decisions(real_node, real_src), (
        "FakeClient.paginate() has drifted from ShopifyClient.paginate(). Every "
        "operations-layer test runs against the fake, so this divergence would "
        "not show up as a failure anywhere else — mirror the change across."
    )


def test_both_paginate_bodies_were_actually_found():
    """Guards the guard: if either file is moved or renamed, `_paginate_def`
    must raise rather than let an empty comparison pass."""
    real_node, real_src = _paginate_def(_REAL)
    decisions = _decisions(real_node, real_src)
    # The walk loop, the page-0 capture, the vanished-connection branch, the
    # hasNextPage return, and the null-cursor abort — five at the time of
    # writing. Asserting non-emptiness rather than a count keeps this from
    # becoming a second thing to update on every edit.
    assert decisions, "no loops or branches parsed out of ShopifyClient.paginate()"
