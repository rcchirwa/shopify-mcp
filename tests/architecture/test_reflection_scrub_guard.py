"""
Offline guard for Story 10.75 (SEC-24-remaining-sites): no "not found" reply in
the tools package may echo an identifier unscrubbed.

Every interpolation inside an f-string whose literal text says
"No <kind> found with" must be `cap(sanitize_control_chars(...))` — `_cap` is
accepted for the outer call, since the module-local `_cap` helpers delegate to
`tools/_scrub.cap` with a narrower bound.

AST, not a regex, on purpose. The card's original grep matched only
`with (id|handle) {` and so missed the five sites written with a quoted handle
(`... handle '{handle}'.`); the parser sees an f-string's parts whatever the
quoting, and implicit string concatenation arrives as a single JoinedStr. The
literal text is matched with each interpolation standing in as `{}`, so an
interpolated or multi-word kind (`No {kind} found with`, `No product variant
found with`) is still seen — and then its `{kind}` must be scrubbed too.

What it does NOT see, stated so a green run isn't over-read: a reply built by
concatenation, `.format()` or `%` rather than an f-string (the tools package
uses f-strings throughout), and differently worded replies (`Product {id} not
found.`, `no product found for {ref!r}`), which are outside this card's Class A.

This class was re-discovered three times before this story (10.39 built the
helpers, 10.58 closed two sites, 10.74's review raised two more). Without this
guard the sweep is a point-in-time fix.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/architecture/test_reflection_scrub_guard.py -v
"""

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_ROOT = _REPO_ROOT / "src" / "shopify_mcp" / "tools"

_NOT_FOUND = re.compile(r"No .+? found with")
_CAPS = {"cap", "_cap"}
_SANITIZE = "sanitize_control_chars"


def _name(func: ast.expr) -> str | None:
    return func.id if isinstance(func, ast.Name) else None


def _is_scrubbed(value: ast.expr) -> bool:
    """`cap(sanitize_control_chars(x))` or `_cap(sanitize_control_chars(x))`.

    The outer call must take exactly one argument: a second positional or a
    `limit=` keyword would raise the bound past the shared default.
    """
    if not (isinstance(value, ast.Call) and _name(value.func) in _CAPS):
        return False
    if len(value.args) != 1 or value.keywords:
        return False
    inner = value.args[0]
    return isinstance(inner, ast.Call) and _name(inner.func) == _SANITIZE


def _not_found_fstrings() -> list[tuple[str, int, ast.JoinedStr]]:
    found = []
    for path in sorted(_TOOLS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
            )
            if _NOT_FOUND.search(literal):
                found.append((str(path.relative_to(_TOOLS_ROOT)), node.lineno, node))
    return found


def test_every_not_found_interpolation_is_scrubbed():
    offenders = []
    for rel, lineno, node in _not_found_fstrings():
        for part in node.values:
            if isinstance(part, ast.FormattedValue) and not _is_scrubbed(part.value):
                offenders.append(f"{rel}:{lineno}: {{{ast.unparse(part.value)}}}")
    assert not offenders, (
        "a 'No … found with' reply echoes an unscrubbed value; wrap it as "
        "cap(sanitize_control_chars(...)) from tools/_scrub:\n" + "\n".join(offenders)
    )


def test_the_guard_sees_every_known_site():
    """Proves the walk ran over real code, so the guard can't pass vacuously.

    21 on origin/main e5e2d7a (Story 10.75 step 1 re-run): products 7,
    catalog_hygiene 4, collections 3, media 5, inventory 2. A new site raises
    the count and is checked above; a lower count means a site was removed or
    the walk stopped seeing it — re-derive before lowering the floor.
    """
    assert len(_not_found_fstrings()) >= 21


def test_the_predicate_rejects_the_shapes_it_must():
    """Control pair: the predicate must tell scrubbed from unscrubbed forms."""

    def value(src: str) -> ast.expr:
        node = ast.parse(src, mode="eval").body
        assert isinstance(node, ast.JoinedStr)
        part = next(p for p in node.values if isinstance(p, ast.FormattedValue))
        return part.value

    assert _is_scrubbed(value('f"No product found with id {cap(sanitize_control_chars(x))}."'))
    assert _is_scrubbed(
        value('f"No product found with handle {_cap(sanitize_control_chars(h))!r}."')
    )
    assert not _is_scrubbed(value('f"No product found with id {x}."'))
    assert not _is_scrubbed(value('f"No product found with id {cap(x)}."'))
    assert not _is_scrubbed(value('f"No product found with id {sanitize_control_chars(cap(x))}."'))
    assert not _is_scrubbed(value('f"No product found with id {str(x)}."'))
    assert not _is_scrubbed(
        value('f"No product found with id {cap(sanitize_control_chars(x), 9999)}."')
    )
    assert not _is_scrubbed(
        value('f"No product found with id {cap(sanitize_control_chars(x), limit=9999)}."')
    )


def test_the_literal_match_sees_interpolated_and_multi_word_kinds():
    """Control pair for the literal match: shapes a `No \\w+` pattern missed."""

    def matches(src: str) -> bool:
        node = ast.parse(src, mode="eval").body
        assert isinstance(node, ast.JoinedStr)
        literal = "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in node.values)
        return bool(_NOT_FOUND.search(literal))

    assert matches('f"No {kind} found with id {x}."')
    assert matches('f"No product variant found with id {x}."')
    assert matches("f\"No collection found with handle '{h}'.\"")
    assert not matches('f"No metafields found for keys: {k}"')
