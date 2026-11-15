"""
Offline guard for Story 10.75 (SEC-24-remaining-sites), Story 10.103
(SEC-24-other-wordings) and Story 10.113 (SEC-24-joined-ids): no reply in the
tools package may echo an identifier unscrubbed. The 10.75 check below finds
one wording; the 10.103 check finds the subject, whatever the wording; the
10.113 check at the end finds joins over caller-derived lists, whatever the
way the reply is built. Each states what it still does not see.

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
import io
import re
import tokenize
from pathlib import Path
from typing import NamedTuple

import pytest

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


# ---------- Story 10.103 (SEC-24-other-wordings): the guard by subject ----------
#
# The check above sees one wording. This one sees the subject: any f-string
# interpolation in the tools package that reads a caller-supplied identifier,
# whatever the sentence around it. Each such interpolation must be
# `cap(sanitize_control_chars(...))`, or carry an exemption marker that names
# the subject and states why the value cannot be forged there:
#
#     # reflect-exempt[product_id]: post-lookup echo, the product was found by this id
#
# The marker goes on the line of the interpolation, or on a comment-only line
# directly above it. It exempts only the subject it names, on that line.
#
# What this still does NOT see, stated so a green run isn't over-read:
# - A caller value under a name that is not in `_SUBJECTS` (or, for the generic
#   names `gid`, `mid` and `numeric_id`, outside the function `_SCOPED_SUBJECTS` names). The
#   list is the step-1 sweep of 5418d9a; a new parameter name must be added.
# - Text built by concatenation, `.format()` or `%`, as for the check above.
# - A subject first copied into a local under another name, unless that name
#   is listed too (`vid`, `target_loc_label`, `keys_display` are).
# - Ids that a helper returns (a normalize step) and the reply joins into one
#   list, e.g. `', '.join(unknown)`: the taint does not follow a helper's
#   return value. Story 10.113's join check at the end of this module covers
#   the joins its census found, by list name; read its own limits there.
# - Audit-log text: interpolations inside `log_write(...)` arguments or a
#   `log_description=` value are skipped, because `log_write` applies
#   `cap(sanitize_control_chars(...))` itself (tools/_log.py).
# - Whether a marker's reason is TRUE. The guard checks that a reason exists;
#   review and the docs/tech-debt.md ledger entry check that it holds.

_SUBJECTS = frozenset(
    {
        "product_id",
        "handle",
        "collection_handle",
        "caller_handle",
        "order_id",
        "location_id",
        "inventory_item_id",
        "media_id",
        "vid",
        "target_loc_label",
        "ns_filter",
        "keys_display",
        "product_ref",
    }
)
# Dict keys whose value is caller input, read as `entry["ownerId"]` or
# `r.get("namespace")`.
_SUBJECT_KEYS = frozenset({"ownerId", "namespace", "key"})
# Generic names that hold a caller value only inside these functions.
_SCOPED_SUBJECTS = {
    "update_product_pricing": frozenset({"gid"}),
    "delete_product_media": frozenset({"mid"}),
    "delete_webhook": frozenset({"numeric_id"}),
}
# Generic dict keys that hold a caller value only inside these functions.
_SCOPED_SUBJECT_KEYS = {
    "reorder_product_media": frozenset({"id"}),
}
_LOG_CALLS = frozenset({"log_write"})
_LOG_KEYWORDS = frozenset({"log_description"})
_MARKER = re.compile(r"#\s*reflect-exempt\[(?P<subject>\w+)\]:(?P<reason>.*)$")
# A reason is at least this many words. "x" or "ok" is not a reason.
_MIN_REASON_WORDS = 3


class SubjectSite(NamedTuple):
    rel: str
    line: int
    functions: tuple[str, ...]
    subjects: frozenset[str]
    scrubbed: bool
    exempt: frozenset[str]

    @property
    def unguarded(self) -> frozenset[str]:
        return frozenset() if self.scrubbed else self.subjects - self.exempt


def _subjects_in(expr: ast.expr, functions: tuple[str, ...]) -> frozenset[str]:
    names = set(_SUBJECTS)
    for fn in functions:
        names |= _SCOPED_SUBJECTS.get(fn, frozenset())
    keys = set(_SUBJECT_KEYS)
    for fn in functions:
        keys |= _SCOPED_SUBJECT_KEYS.get(fn, frozenset())
    found = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name) and n.id in names}
    found |= {
        n.slice.value
        for n in ast.walk(expr)
        if isinstance(n, ast.Subscript)
        and isinstance(n.slice, ast.Constant)
        and n.slice.value in keys
    }
    found |= {
        n.args[0].value
        for n in ast.walk(expr)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value in keys
    }
    return frozenset(found)


def _log_text_ids(tree: ast.AST) -> set[int]:
    """ids of every node inside a log_write(...) argument or a log_description= value."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _name(node.func) in _LOG_CALLS:
            for arg in node.args:
                skip |= {id(n) for n in ast.walk(arg)}
        for kw in node.keywords:
            if kw.arg in _LOG_KEYWORDS:
                skip |= {id(n) for n in ast.walk(kw.value)}
    return skip


def _markers(source: str) -> dict[int, list[tuple[str, str]]]:
    """line -> [(subject, reason)] for every reflect-exempt comment in `source`."""
    out: dict[int, list[tuple[str, str]]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        m = _MARKER.search(tok.string)
        if m:
            out.setdefault(tok.start[0], []).append((m["subject"], m["reason"].strip()))
    return out


def _marker_target(line: int, lines: list[str]) -> int:
    """A marker on a comment-only line applies to the next line that is not a comment."""
    target = line
    while target <= len(lines) and lines[target - 1].strip().startswith("#"):
        target += 1
    return target


def _has_reason(reason: str) -> bool:
    return len(reason.split()) >= _MIN_REASON_WORDS


def _scan(source: str, rel: str) -> tuple[list[SubjectSite], dict[int, list[tuple[str, str]]]]:
    tree = ast.parse(source, filename=rel)
    lines = source.splitlines()
    markers = _markers(source)
    exempt_at: dict[int, set[str]] = {}
    for line, found in markers.items():
        for subject, reason in found:
            if _has_reason(reason):
                exempt_at.setdefault(_marker_target(line, lines), set()).add(subject)
    skip = _log_text_ids(tree)
    sites: list[SubjectSite] = []

    def visit(node: ast.AST, functions: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions = (*functions, node.name)
        if isinstance(node, ast.JoinedStr) and id(node) not in skip:
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                subjects = _subjects_in(part.value, functions)
                if subjects:
                    line = part.value.lineno
                    sites.append(
                        SubjectSite(
                            rel,
                            line,
                            functions,
                            subjects,
                            _is_scrubbed(part.value),
                            frozenset(exempt_at.get(line, set()) & subjects),
                        )
                    )
        for child in ast.iter_child_nodes(node):
            visit(child, functions)

    visit(tree, ())
    return sites, markers


def _all_subject_sites() -> list[tuple[str, list[SubjectSite], dict, list[str]]]:
    out = []
    for path in sorted(_TOOLS_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(_TOOLS_ROOT))
        sites, markers = _scan(source, rel)
        out.append((rel, sites, markers, source.splitlines()))
    return out


def _subject_sites() -> list[SubjectSite]:
    return [s for _rel, sites, _m, _l in _all_subject_sites() for s in sites]


def test_s10103_every_subject_interpolation_is_scrubbed_or_exempt():
    offenders = [
        f"{s.rel}:{s.line}: {sorted(s.unguarded)}" for s in _subject_sites() if s.unguarded
    ]
    assert not offenders, (
        "a reply echoes a caller-supplied identifier unscrubbed; wrap it as "
        "cap(sanitize_control_chars(...)) from tools/_scrub, or, only where the value "
        "cannot be forged there, add `# reflect-exempt[<subject>]: <reason>`:\n"
        + "\n".join(offenders)
    )


def test_s10103_every_marker_has_a_reason_and_guards_a_raw_echo():
    """A marker with no reason, or one that no longer sits on a raw echo of the
    subject it names, is an error: a stale marker would exempt the next echo
    someone writes on that line."""
    problems = []
    for rel, sites, markers, lines in _all_subject_sites():
        raw_at = {(s.line, subj) for s in sites if not s.scrubbed for subj in s.subjects}
        for line, found in markers.items():
            for subject, reason in found:
                if not _has_reason(reason):
                    problems.append(f"{rel}:{line}: marker for {subject} has no reason")
                elif (_marker_target(line, lines), subject) not in raw_at:
                    problems.append(f"{rel}:{line}: marker for {subject} guards no raw echo")
    assert not problems, "\n".join(problems)


def test_s10103_the_guard_sees_every_known_subject_site():
    """Proves the walk ran over real code, so the guard can't pass vacuously.

    On the Story 10.103 tree (origin/main 5418d9a plus this story): 110 subject
    interpolations, of which 13 carry a marker and 97 are scrubbed. Re-derived
    on the Story 10.113 tree (origin/main 99e2315 plus that story): the same
    110 / 13 / 97, since its fixes are joins, not subject interpolations; its
    own floor is `test_s10113_the_join_check_sees_every_known_site`. A new site
    raises the count and is checked above; a lower count means a site was
    removed or the walk stopped seeing it — re-derive before lowering a floor.
    """
    sites = _subject_sites()
    assert len(sites) >= 110
    assert sum(1 for s in sites if s.exempt) >= 13


def _scan_snippet(body: str) -> list[SubjectSite]:
    sites, _markers_found = _scan(body, "snippet.py")
    return sites


def test_s10103_control_pair_raw_echo_is_caught_and_scrubbed_echo_passes():
    """One pair per shape the guard covers: a bare name, a listed derived name,
    a subject inside a call, a subject dict key (subscript or `.get`), and a
    scoped generic name."""
    raw = {
        "name": 'def f(product_id):\n    return f"Product {product_id} not found."\n',
        "derived": 'def f(vids):\n    return "\\n".join(f"• {vid}" for vid in vids)\n',
        "call": 'def f(handle):\n    return f"from {handle or title}"\n',
        "key": "def f(c):\n    return f\"Owner {c['ownerId']} not found\"\n",
        "get": "def f(r):\n    return f\"{r.get('namespace') or '?'}.k\"\n",
        "scoped": 'def update_product_pricing(gid):\n    return f"id: {from_gid(gid)}"\n',
    }
    for shape, src in raw.items():
        assert any(s.unguarded for s in _scan_snippet(src)), shape
    scrubbed = {
        "name": 'def f(product_id):\n    return f"Product {cap(sanitize_control_chars(product_id))}."\n',
        "derived": 'def f(vids):\n    return "\\n".join(f"• {cap(sanitize_control_chars(vid))}" for vid in vids)\n',
        "call": 'def f(handle):\n    return f"from {cap(sanitize_control_chars(handle or title))}"\n',
        "key": 'def f(c):\n    return f"Owner {cap(sanitize_control_chars(c[\'ownerId\']))} not found"\n',
        "get": "def f(r):\n    return f\"{cap(sanitize_control_chars(r.get('namespace') or '?'))}.k\"\n",
        "scoped": 'def update_product_pricing(gid):\n    return f"id: {cap(sanitize_control_chars(from_gid(gid)))}"\n',
    }  # fmt: skip
    for shape, src in scrubbed.items():
        sites = _scan_snippet(src)
        assert sites, shape  # still seen
        assert not any(s.unguarded for s in sites), shape


def test_s10103_control_pair_scoped_name_is_free_outside_its_function():
    """`gid` is a caller value only in update_product_pricing; elsewhere it is
    the GID Shopify returned, so the guard must not see it."""
    assert not _scan_snippet('def other(gid):\n    return f"id: {from_gid(gid)}"\n')


def test_s10103_control_pair_scoped_key_is_seen_only_in_its_function():
    """`m["id"]` is a caller media id in reorder_product_media; elsewhere `id`
    is usually Shopify's, so the guard must not see it there."""
    src = "def {fn}(moves):\n    return f\"{{m['id']}}\"\n"
    assert any(s.unguarded for s in _scan_snippet(src.format(fn="reorder_product_media")))
    assert not _scan_snippet(src.format(fn="other"))


def test_s10103_every_identifier_channel_display_name_is_a_subject():
    """`_identifier_channel` returns the caller's id for display as its third
    value. Review round 2 found it echoed raw as `product_ref`, a name the list
    did not have. Every name bound to it must be listed (or unused: `_name`)."""
    names = set()
    for path in sorted(_TOOLS_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _name(node.value.func) == "_identifier_channel"
                and isinstance(node.targets[0], ast.Tuple)
            ):
                display = node.targets[0].elts[2]
                assert isinstance(display, ast.Name), ast.unparse(node)
                names.add(display.id)
    assert names  # the walk saw the helper's callers
    assert {n for n in names if not n.startswith("_")} <= _SUBJECTS


def test_s10103_control_pair_log_text_is_skipped_and_reply_text_is_not():
    logged = (
        "def f(product_id):\n"
        '    log_write("t", f"product={product_id}")\n'
        '    write_gate(log_description=f"id={product_id}")\n'
    )
    assert not _scan_snippet(logged)
    reply = 'def f(product_id):\n    return reply(f"product={product_id}")\n'
    assert any(s.unguarded for s in _scan_snippet(reply))


def test_s10103_control_pair_marker_with_reason_passes_and_without_fails():
    with_reason = (
        "def f(product_id):\n"
        "    # reflect-exempt[product_id]: post-lookup echo, the product was found\n"
        '    return f"Product {product_id}"\n'
    )
    assert [s.unguarded for s in _scan_snippet(with_reason)] == [frozenset()]
    same_line = (
        "def f(product_id):\n"
        '    return f"Product {product_id}"  # reflect-exempt[product_id]: found by this id\n'
    )
    assert [s.unguarded for s in _scan_snippet(same_line)] == [frozenset()]
    for bad in (
        "    # reflect-exempt[product_id]:\n",
        "    # reflect-exempt[product_id]: ok\n",
        "    # reflect-exempt[handle]: post-lookup echo, the collection was found\n",
        "    # reflect-exempt: post-lookup echo, the product was found\n",
    ):
        src = "def f(product_id):\n" + bad + '    return f"Product {product_id}"\n'
        assert [s.unguarded for s in _scan_snippet(src)] == [frozenset({"product_id"})], bad


def test_s10103_control_pair_marker_exempts_only_its_own_subject():
    """Two subjects on one line: the marker for one must leave the other guarded."""
    src = (
        "def f(media_id, product_id):\n"
        "    # reflect-exempt[product_id]: post-lookup echo, the product was found\n"
        '    return f"media {media_id} is not attached to product {product_id}."\n'
    )
    assert [s.unguarded for s in _scan_snippet(src)] == [frozenset({"media_id"}), frozenset()]


def test_s10103_control_pair_marker_does_not_reach_past_a_code_line():
    src = (
        "def f(product_id):\n"
        "    # reflect-exempt[product_id]: post-lookup echo, the product was found\n"
        "    x = 1\n"
        '    return f"Product {product_id}"\n'
    )
    assert [s.unguarded for s in _scan_snippet(src)] == [frozenset({"product_id"})]


# ---------- Story 10.113 (SEC-24-joined-ids): joins over caller-derived lists ----------
#
# The subject check above follows a caller value by its name inside an
# f-string. The joins this story fixed iterate over lists that a helper built
# (`unknown_gids`, `unknown_variant_gids`, `unknown`, `failed`) and are often
# glued on with `+`, so that check never saw them. This check finds every
# `<sep>.join(...)` whose items come from a caller-derived list, wherever the
# result goes (`+`, an f-string, a return value), and checks two shapes:
#
# - One line, a separator with no newline (`", "`): each item must be wrapped
#   in `sanitize_control_chars`, and the join itself must be the only argument
#   of `cap(...)` / `_cap(...)`. These are two separate properties, reported
#   separately: an item can be sanitized and the list still unbounded (F3),
#   or the list bounded and an item not sanitized (F2, F4).
# - Rows, a separator with a newline (`_render_failed`): the rows are meant to
#   be separate lines, so the whole block is NOT bounded or sanitized. Each read
#   of a listed row field (`channel_name`) must sit inside
#   `cap(sanitize_control_chars(...))`, and a row join that reads no listed
#   field fails, so a renamed key cannot pass silently.
#
# What this still does NOT see, stated so a green run isn't over-read:
# - Taint is by name and by scoped entry. A list is caller-derived only under
#   a name `_JOINED_LISTS` lists for the function that holds the join. A new
#   helper-returned list under a new name, or a join in a new function, is not
#   seen until it is added there. The same holds for a row field not in
#   `_ROW_FIELD_KEYS`.
# - Values passed through other variables or functions are not followed: a
#   list copied to a new name, a join built in a helper and returned to the
#   tool, or a joined string stored in a variable and bounded later.
# - Shopify-sourced lists that share a listed name are treated as caller
#   lists: `_render_failed`'s `failed` also carries Shopify apply-failure
#   names, and they get the same field rule.
# - Per-line lists that already scrub each id with `cap(sanitize_control_chars(...))`
#   (`media/_reorder.py`'s moves, `media/_delete.py`'s unmatched ids, the
#   unresolved variant ids in `inventory.py` and `products.py`) are not listed.
#   They cannot forge a line; their length grows only with the caller's own
#   count. Story 10.113's docs/tech-debt.md entry records each excluded join.
# - Audit-log text is skipped, as for the subject check.

_JOINED_LISTS = {
    "update_product_pricing": frozenset({"unknown_gids"}),
    "update_variant_image_binding": frozenset({"unknown_variant_gids", "unknown"}),
    "update_product_options": frozenset({"unknown_value_ids"}),
    "reorder_product_media": frozenset({"unknown"}),
    "_render_unresolved_names": frozenset({"failed"}),
    "_render_failed": frozenset({"failed"}),
}
_ROW_FIELD_KEYS = {
    "_render_failed": frozenset({"channel_name"}),
}
_COMPREHENSIONS = (ast.GeneratorExp, ast.ListComp, ast.SetComp)


class JoinSite(NamedTuple):
    rel: str
    line: int
    functions: tuple[str, ...]
    rows: bool
    items_sanitized: bool
    bounded: bool
    fields_scrubbed: bool

    @property
    def problems(self) -> tuple[str, ...]:
        if self.rows:
            return () if self.fields_scrubbed else ("row field not cap(sanitize_control_chars())",)
        found = []
        if not self.items_sanitized:
            found.append("item not sanitized")
        if not self.bounded:
            found.append("joined list not bounded")
        return tuple(found)


def _is_sanitize_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _name(node.func) == _SANITIZE


def _item_is_sanitized(elt: ast.expr) -> bool:
    """`sanitize_control_chars(x)`, or a per-item bound around it."""
    if _is_sanitize_call(elt):
        return True
    return (
        isinstance(elt, ast.Call)
        and _name(elt.func) in _CAPS
        and len(elt.args) == 1
        and _is_sanitize_call(elt.args[0])
    )


def _reads_key(node: ast.AST, keys: frozenset[str]) -> bool:
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in keys
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in keys
    )


def _row_fields_scrubbed(elt: ast.expr, keys: frozenset[str]) -> bool:
    """Every read of a listed field sits inside a `cap(sanitize_control_chars(...))`,
    and there is at least one such read."""
    inside: set[int] = set()
    for node in ast.walk(elt):
        if isinstance(node, ast.expr) and _is_scrubbed(node):
            inside |= {id(n) for n in ast.walk(node)}
    reads = [n for n in ast.walk(elt) if _reads_key(n, keys)]
    return bool(reads) and all(id(n) in inside for n in reads)


def _join_over(node: ast.Call, lists: frozenset[str]) -> ast.expr | None:
    """The per-item expression of `<str>.join(...)` over a listed name, else None.
    A bare `sep.join(name)` has the name itself as its item."""
    if not (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
        and len(node.args) == 1
    ):
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Name):
        return arg if arg.id in lists else None
    if isinstance(arg, _COMPREHENSIONS) and any(
        isinstance(g.iter, ast.Name) and g.iter.id in lists for g in arg.generators
    ):
        return arg.elt
    return None


def _scan_joins(source: str, rel: str) -> list[JoinSite]:
    tree = ast.parse(source, filename=rel)
    skip = _log_text_ids(tree)
    sites: list[JoinSite] = []

    def visit(node: ast.AST, functions: tuple[str, ...], parent: ast.AST | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions = (*functions, node.name)
        if isinstance(node, ast.Call) and id(node) not in skip:
            lists: frozenset[str] = frozenset()
            keys: frozenset[str] = frozenset()
            for fn in functions:
                lists |= _JOINED_LISTS.get(fn, frozenset())
                keys |= _ROW_FIELD_KEYS.get(fn, frozenset())
            elt = _join_over(node, lists) if lists else None
            if elt is not None:
                assert isinstance(node.func, ast.Attribute)
                assert isinstance(node.func.value, ast.Constant)
                rows = "\n" in node.func.value.value
                bounded = (
                    isinstance(parent, ast.Call)
                    and _name(parent.func) in _CAPS
                    and len(parent.args) == 1
                    and not parent.keywords
                    and parent.args[0] is node
                )
                sites.append(
                    JoinSite(
                        rel,
                        node.lineno,
                        functions,
                        rows,
                        _item_is_sanitized(elt),
                        bounded,
                        _row_fields_scrubbed(elt, keys),
                    )
                )
        for child in ast.iter_child_nodes(node):
            visit(child, functions, node)

    visit(tree, (), None)
    return sites


def _join_sites() -> list[JoinSite]:
    out = []
    for path in sorted(_TOOLS_ROOT.rglob("*.py")):
        out += _scan_joins(path.read_text(encoding="utf-8"), str(path.relative_to(_TOOLS_ROOT)))
    return out


def test_s10113_every_caller_list_join_is_sanitized_and_bounded():
    offenders = [
        f"{s.rel}:{s.line} ({s.functions[-1]}): {', '.join(s.problems)}"
        for s in _join_sites()
        if s.problems
    ]
    assert not offenders, (
        "a reply joins caller-derived ids unscrubbed. One line: "
        "cap(', '.join(sanitize_control_chars(x) for x in xs)) (`_cap` in catalog_hygiene). "
        "Rows: cap(sanitize_control_chars(...)) on the caller field of each row:\n"
        + "\n".join(offenders)
    )


def test_s10113_the_join_check_sees_every_known_site():
    """Proves the walk ran over real code, so the check can't pass vacuously.

    On the Story 10.113 tree (origin/main 99e2315 plus this story): six
    one-line joins (F1 `update_product_pricing`, F2 and the media-id join in
    `update_variant_image_binding`, `update_product_options`' value ids, F3
    `reorder_product_media`, F4 `_render_unresolved_names`) and one row join
    (F5 `_render_failed`). A lower count means a site was removed or the walk
    stopped seeing it; re-derive before lowering a floor.
    """
    sites = _join_sites()
    seen = {(s.rel, s.functions[-1], s.rows) for s in sites}
    assert seen >= {
        ("catalog_hygiene.py", "update_product_pricing", False),
        ("catalog_hygiene.py", "update_variant_image_binding", False),
        ("catalog_hygiene.py", "update_product_options", False),
        ("media/_reorder.py", "reorder_product_media", False),
        ("publications.py", "_render_unresolved_names", False),
        ("publications.py", "_render_failed", True),
    }
    assert sum(1 for s in sites if not s.rows) >= 6
    assert sum(1 for s in sites if s.rows) >= 1


def _join_problems(src: str) -> list[tuple[str, ...]]:
    return [s.problems for s in _scan_joins(src, "snippet.py")]


# Control pairs built from the real F1-F5 expressions on origin/main 99e2315
# (raw) and as this story fixed them. Each raw form must fail for exactly the
# reason in the story's table; each fixed form must be seen and pass.
_S10113_PAIRS = {
    "F1": (
        "def update_product_pricing(unknown_gids):\n"
        '    head = "Error: resolved variant(s) not found on product: " + ", ".join(\n'
        "        from_gid(g) for g in unknown_gids\n"
        "    )\n",
        ("item not sanitized", "joined list not bounded"),
        "def update_product_pricing(unknown_gids):\n"
        '    head = "Error: resolved variant(s) not found on product: " + _cap(\n'
        '        ", ".join(sanitize_control_chars(from_gid(g)) for g in unknown_gids)\n'
        "    )\n",
    ),
    "F2": (
        "def update_variant_image_binding(unknown_variant_gids, product_ref):\n"
        "    msg = (\n"
        '        f"variant GIDs not on product {cap(sanitize_control_chars(product_ref))}: "\n'
        '        + _cap(", ".join(from_gid(g) for g in unknown_variant_gids))\n'
        "    )\n",
        ("item not sanitized",),
        "def update_variant_image_binding(unknown_variant_gids, product_ref):\n"
        "    msg = (\n"
        '        f"variant GIDs not on product {cap(sanitize_control_chars(product_ref))}: "\n'
        '        + _cap(", ".join(sanitize_control_chars(from_gid(g)) for g in unknown_variant_gids))\n'
        "    )\n",
    ),
    "F3": (
        "def reorder_product_media(unknown):\n"
        '    return "Error: these media ids are not attached to the product: " + ", ".join(\n'
        "        cap(sanitize_control_chars(u)) for u in unknown\n"
        "    )\n",
        ("joined list not bounded",),
        "def reorder_product_media(unknown):\n"
        '    return "Error: these media ids are not attached to the product: " + cap(\n'
        '        ", ".join(sanitize_control_chars(u) for u in unknown)\n'
        "    )\n",
    ),
    "F4": (
        "def _render_unresolved_names(failed):\n"
        '    return cap(", ".join(str(f.get("channel_name", "?")) for f in failed))\n',
        ("item not sanitized",),
        "def _render_unresolved_names(failed):\n"
        "    return cap(\n"
        '        ", ".join(sanitize_control_chars(str(f.get("channel_name", "?"))) for f in failed)\n'
        "    )\n",
    ),
    "F5": (
        "def _render_failed(failed):\n"
        '    return "\\n".join(\n'
        "        f\"  • {cap(str(f.get('channel_name', '?')))}: {cap(str(f.get('error')))}\" for f in failed\n"
        "    )\n",
        ("row field not cap(sanitize_control_chars())",),
        "def _render_failed(failed):\n"
        '    return "\\n".join(\n'
        "        f\"  • {cap(sanitize_control_chars(str(f.get('channel_name', '?'))))}: \"\n"
        "        f\"{cap(str(f.get('error')))}\"\n"
        "        for f in failed\n"
        "    )\n",
    ),
}


@pytest.mark.parametrize("pair", list(_S10113_PAIRS), ids=list(_S10113_PAIRS))
def test_s10113_control_pair_raw_join_fails_for_its_reason_and_fixed_join_passes(pair):
    raw, reasons, fixed = _S10113_PAIRS[pair]
    assert _join_problems(raw) == [reasons]
    assert _join_problems(fixed) == [()]


def test_s10113_control_pair_join_shapes_the_rule_must_tell_apart():
    """Shapes around the real ones: a bare list, a bound with a raised limit,
    a list comprehension, an unlisted function, and log text."""
    fn = "def reorder_product_media(unknown):\n    return {}\n"
    assert _join_problems(fn.format('", ".join(unknown)')) == [
        ("item not sanitized", "joined list not bounded")
    ]
    assert _join_problems(
        fn.format('cap(", ".join(sanitize_control_chars(u) for u in unknown), 9999)')
    ) == [("joined list not bounded",)]
    assert _join_problems(
        fn.format('cap(", ".join(sanitize_control_chars(u) for u in unknown), limit=9999)')
    ) == [("joined list not bounded",)]
    assert _join_problems(
        fn.format('cap(", ".join([sanitize_control_chars(u) for u in unknown]))')
    ) == [()]
    # Sanitizing the joined text is not sanitizing each item: the rule is the
    # 10.103 form, so this shape is refused even though the bound holds.
    assert _join_problems(fn.format('sanitize_control_chars(cap(", ".join(unknown)))')) == [
        ("item not sanitized",)
    ]
    assert not _join_problems('def other(unknown):\n    return ", ".join(unknown)\n')
    assert not _join_problems(
        'def reorder_product_media(unknown):\n    log_write("t", ", ".join(unknown))\n'
    )


def test_s10113_control_pair_row_join_needs_the_listed_field():
    """A row join that reads no listed field fails closed; a scrubbed field
    with an unscrubbed copy of the same field in the row still fails."""
    renamed = (
        "def _render_failed(failed):\n"
        '    return "\\n".join(f"  • {cap(sanitize_control_chars(str(f.get(\'name\'))))}" for f in failed)\n'
    )
    assert _join_problems(renamed) == [("row field not cap(sanitize_control_chars())",)]
    twice = (
        "def _render_failed(failed):\n"
        '    return "\\n".join(\n'
        "        f\"  • {cap(sanitize_control_chars(f['channel_name']))} ({f['channel_name']})\"\n"
        "        for f in failed\n"
        "    )\n"
    )
    assert _join_problems(twice) == [("row field not cap(sanitize_control_chars())",)]
