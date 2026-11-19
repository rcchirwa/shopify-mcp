"""Census guard for Story 10.116 (SEC-04-titles-census): every use of a store
title is fenced or is a recorded exception.

Story 10.112 fenced every store title with ``wrap()``. Its guard,
``test_title_fencing.py``, lists the 45 fence points by hand (``APPENDIX_B``),
so a NEW raw title render (a new tool, branch or output line) failed no test.
This file scans the source of every module under ``src/shopify_mcp/tools/``
with ``ast`` and classifies each USE of a title value, not each read: one read
can feed a fenced display line, a raw JSON tail and a second display line.

Sources: every literal-key read of ``title`` or ``variantTitle`` --
``x["title"]`` or ``x.get("title", ...)``, under any local name.

Flows the scanner follows (a bounded set that covers today's code):
  - local aliases, tuple unpacking, ``for`` targets and comprehension targets;
  - the falsy guard ``wrap(t) if t else <t or our own text>``;
  - text built from the value: f-strings, ``+``, ``.join()``, ``.append()``;
  - containers: dict values, list and tuple items, comprehensions; a later
    read of a dict key that is not the one that holds the title is clean;
  - helpers defined in the scanned modules: an argument enters the helper's
    parameter, and a return goes back to the caller (to every caller when
    the read is inside the helper);
  - the pass-through helpers in ``PASSTHROUGH`` (text in, the same text out).

The fence: a call that resolves, by the module's imports, to
``shopify_mcp.tools._untrusted.wrap``. A local function, a local variable or
parameter, or another import named ``wrap`` is not the fence.

Each use ends in one class:
  - FENCED: it goes through the shared ``wrap()`` on that path (the guard
    test and the falsy branch of the fence belong to the same fence point);
  - an exception class (``ALLOWED_DESTS``): JSON tail, audit log, length,
    predicate, naming validator, Shopify call. Each such use must match
    exactly one row of ``EXCEPTION_ROWS``;
  - DISPLAY: raw text reaches tool output. Always a failure;
  - UNSUPPORTED: a use the scanner cannot classify. Always a failure.

A row is keyed by module, qualified function and title key of the READ, and
by the use pattern: the normalized path from the read (``ast.unparse``) to the
destination. Each row has a reason and an expected match count. A stale row,
a wrong count (a duplicate match), a row with no reason and an unclassified
use each fail and name the file, the function and the current line.

A title that reaches a helper with no named parameter for it (``**kwargs``,
``*args`` or a ``**`` splat) is UNSUPPORTED. A title stored under a computed
dict key, or as a list item, follows every key or index read of that value.

``run_census`` is the one entry point. The real-tree test, the APPENDIX_B
identity test and every mutation test call it. The mutations change the
parsed source text in memory; no file is written. The scan of the unmutated
tree is cached; a mutated copy is always scanned again.

APPENDIX_B identity. ``FENCE_POINTS`` maps each hand-listed id to (module,
qualified function, n-th title fence in it). The check does not trust that
map alone: ``_observed_fence_lines`` runs every CASES row of
``test_title_fencing.py`` and records the line of the ``wrap()`` call that
fenced the id's own distinct title value. Each mapped fence must sit on one
of those lines, so an id mapped to the wrong fence of the same module fails.

Scope beyond the card, recorded on purpose (Robert, review round 1: record,
do not narrow):
  - SEO titles. The census counts the ``title`` key under ``seo`` too, because
    it cannot tell an SEO title from a product title. That gives two fence
    points beyond APPENDIX_B (``update_product_seo``, ``get_product_full``;
    fenced since Story 10.92) and the LENGTH row of ``update_product_seo``.
  - Two exception classes beyond the card's list of permitted uses, each with
    one row. VALIDATOR: ``format_validation_diff`` uses the old title only to
    sort naming issues, and its text never holds the old title (a test proves
    it). SHOPIFY_CALL: an ``ops.*`` call sends the value to Shopify, not to the
    tool output. Neither is a display use. A NEW use of either class still
    fails until it has its own row with a reason and a count.

Detection limit. The scanner starts from recognized ``title`` and
``variantTitle`` reads. It can miss a title that the code exposes only under
another key, a GraphQL alias, attribute access or ``getattr``, and an object
that holds a title and is rendered whole (for example ``f"{product}"``).
``displayName`` (metaobjects) is out of scope; on base its only render is
``catalog_hygiene._metaobject_lines``, which fences it. The scanner cannot
tell an SEO title from a product title (see above). Names are tracked per
function without flow order, so a name that holds a title anywhere in a
function counts as a title at every load in it; that errs toward failing.
"""

import ast
import builtins
import functools
import importlib
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

import pytest

import shopify_mcp.tools as tools_pkg
from shopify_mcp.tools import _untrusted
from shopify_mcp.validators.naming import format_validation_diff
from tests.unit.tools.test_title_fencing import APPENDIX_B, CASES, _defaults
from tests.unit.tools.test_title_fencing import _run as _run_case

PACKAGE = "shopify_mcp.tools"
TOOLS_DIR = Path(tools_pkg.__file__).parent
TITLE_KEYS = frozenset({"title", "variantTitle"})
WRAP = "shopify_mcp.tools._untrusted.wrap"

FENCED = "FENCED"
DISPLAY = "DISPLAY"
UNSUPPORTED = "UNSUPPORTED"
JSON = "JSON"
AUDIT_LOG = "AUDIT_LOG"
LENGTH = "LENGTH"
PREDICATE = "PREDICATE"
VALIDATOR = "VALIDATOR"
SHOPIFY_CALL = "SHOPIFY_CALL"
ALLOWED_DESTS = frozenset({JSON, AUDIT_LOG, LENGTH, PREDICATE, VALIDATOR, SHOPIFY_CALL})

# Text in, the same text out: the title is still raw in the result.
PASSTHROUGH = {
    "shopify_mcp.tools._untrusted.with_reminder": "adds the reminder line before the text",
    "shopify_mcp.tools._response.with_confirm_hint": "adds the confirm hint after the text",
    "shopify_mcp.tools._scrub.cap": "shortens the text",
    "shopify_mcp.tools._scrub.sanitize_control_chars": "removes control characters",
    "shopify_mcp.tools.discounts._collapse_to_single_line": "joins the text onto one line",
}
# Callees where the value leaves the tool's output text.
SINKS = {
    "json.dumps": JSON,
    "shopify_mcp.tools._log.log_write": AUDIT_LOG,
    "builtins.len": LENGTH,
    "shopify_mcp.validators.naming.format_validation_diff": VALIDATOR,
}
WRITE_GATE = "shopify_mcp.tools._write_tool.write_gate"
WRITE_GATE_KWARGS = {"log_description": AUDIT_LOG, "preview": DISPLAY, "done_text": DISPLAY}
SHOPIFY_OPS = "shopify_mcp.shopify.operations."
# Callees that the scanner names by these tables, never by their body.
_NAMED_CALLEES = frozenset({WRAP, WRITE_GATE, *PASSTHROUGH, *SINKS})

_Func = ast.FunctionDef | ast.AsyncFunctionDef
_Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.Module
_Path = tuple[str | int, ...]


@dataclass(frozen=True)
class Row:
    """One permitted raw use. module and function name the title read."""

    module: str
    function: str
    key: str
    pattern: str
    dest: str
    reason: str
    count: int


@dataclass(frozen=True)
class Use:
    module: str
    function: str
    line: int
    key: str
    pattern: str
    dest: str
    site: str
    # (module, function, (line, column)) of the wrap() call, for FENCED.
    fence: tuple[str, str, tuple[int, int]] | None = None


@dataclass
class Census:
    uses: list[Use] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # (module, function, n-th title fence in it) -> current line of the wrap().
    fence_sites: dict[tuple[str, str, int], int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Exception rows (the permitted raw uses on base 4766b87)
# --------------------------------------------------------------------------

# Counts are uses, not reads: a name is tracked without flow order, so a dict
# that reaches `payload` counts once per load of `payload` in that function.
EXCEPTION_ROWS: tuple[Row, ...] = (
    Row(
        "catalog_hygiene",
        "_shape_options_snapshot",
        "title",
        "v.get('title') > {'title'} > variants.append() > {'variants'} > return to register.update_product_options > -> _format_options_payload(product_snapshot=) > {'product'} > =payload > json.dumps() => JSON",
        JSON,
        "JSON tail of update_product_options: the product snapshot keeps each raw variant title for machine readers (Story 10.112 R9). One use per call of the snapshot helper.",
        11,
    ),
    Row(
        "catalog_hygiene",
        "_shape_product_snapshot",
        "title",
        "product_node.get('title') > {'title'} > return to register.update_product_category > -> _build_payload(product=) > {'product'} > return > =payload > -> _format_payload(payload=) > json.dumps() => JSON",
        JSON,
        "JSON tail of update_product_category: the product snapshot keeps the raw title (R9). The head shows no title.",
        4,
    ),
    Row(
        "catalog_hygiene",
        "register.get_product_metafields",
        "title",
        "product_node.get('title') > {'title'} > =product_summary > -> _format_read_metafields_payload(product=) > {'product'} > return > =payload > -> _render(payload=) > json.dumps() => JSON",
        JSON,
        "JSON tail `product` keeps the raw title (R9); the head fences the same title at C07.",
        1,
    ),
    Row(
        "catalog_hygiene",
        "register.get_product_metafields",
        "title",
        "v_node.get('title') > {'variantTitle'} > variant_buckets.append() > =variant_metafields_payload > -> _format_read_metafields_payload(variant_metafields=) > {'variantMetafields'} > return > =payload > -> _render(payload=) > json.dumps() => JSON",
        JSON,
        "JSON tail `variantMetafields` keeps the raw variant title (R9); the head fences it at C08.",
        1,
    ),
    Row(
        "catalog_hygiene",
        "register.get_product_metafields",
        "title",
        "v_node.get('title') > {'variantTitle'} > variant_buckets.append() > truth test => PREDICATE",
        PREDICATE,
        "`if include_variants and variant_buckets`: a truth test of the list that holds the variant titles. It renders nothing.",
        1,
    ),
    Row(
        "catalog_hygiene",
        "register.update_product_category",
        "title",
        "current_product.get('title') > {'title'} > =preview_product > -> _build_payload(product=) > {'product'} > return > =payload > -> _format_payload(payload=) > json.dumps() => JSON",
        JSON,
        "JSON tail of the update_product_category preview: the product it would write keeps the raw title (R9).",
        4,
    ),
    Row(
        "inventory",
        "register.update_variant_inventory_tracking",
        "title",
        "v['title'] > [for] > f-string > shopify_mcp.tools._log.log_write() => AUDIT_LOG",
        AUDIT_LOG,
        "Audit log line: the changed and unchanged variant titles stay raw (R9). The log is not tool output. One read for `changed`, one for `unchanged`.",
        2,
    ),
    Row(
        "products",
        "register.update_product_seo",
        "title",
        "old_seo.get('title') > or-and > =old_title > builtins.len() => LENGTH",
        LENGTH,
        "`_log_desc` logs only the length of the stored SEO title, a number.",
        1,
    ),
    Row(
        "products",
        "register.update_product_seo._execute",
        "title",
        "old_seo.get('title') > if-else > ops.update_product_seo() => SHOPIFY_CALL",
        SHOPIFY_CALL,
        "A stored SEO title the caller did not change is sent back to productUpdate as read (Story 9.19). It goes to Shopify, not to the output.",
        1,
    ),
    Row(
        "products",
        "register.update_product_title",
        "title",
        "product.get('title', '') > =old_title > f-string > write_gate(log_description=) => AUDIT_LOG",
        AUDIT_LOG,
        "write_gate's log_description is audit-log text; it keeps the raw old title (R9).",
        1,
    ),
    Row(
        "products",
        "register.update_product_title",
        "title",
        "product.get('title', '') > =old_title > shopify_mcp.validators.naming.format_validation_diff() => VALIDATOR",
        VALIDATOR,
        "format_validation_diff uses the old title only to sort naming issues into introduced / pre-existing / fixed. Its text holds new_title and fixed messages, never old_title (validators/naming.py).",
        1,
    ),
)

# --------------------------------------------------------------------------
# APPENDIX_B fence ids -> (module, qualified function, n-th title fence in it)
# --------------------------------------------------------------------------

FENCE_POINTS: dict[str, tuple[str, str, int]] = {
    "P01": ("products", "register.get_products", 1),
    "P02": ("products", "register.get_products", 2),
    "P03": ("products", "register.get_product", 1),
    "P04": ("products", "register.get_product", 2),
    "P05": ("products", "register.update_product_title", 1),
    "P06": ("products", "register.get_products_by_collection", 1),
    "P07": ("products", "register.get_product_description", 1),
    "P08": ("products", "register.get_products_with_descriptions", 1),
    "P09": ("products", "register.get_product_full", 1),
    "P10": ("products", "register.get_product_full", 3),
    "P11": ("products", "register.get_product_collections", 1),
    "P12": ("products", "register.get_product_collections", 2),
    "P13": ("products", "register.update_variant_inventory_policy", 1),
    "P14": ("products", "register.update_variant_inventory_policy", 2),
    "P15": ("products", "register.update_variant_inventory_policy", 3),
    "P16": ("products", "register.update_variant_inventory_policy", 4),
    "I01": ("inventory", "_variant_label", 1),
    "I02": ("inventory", "_pair_prefix", 1),
    "I03": ("inventory", "register.get_inventory", 1),
    "I04": ("inventory", "register.get_inventory", 2),
    "I05": ("inventory", "register.update_variant_inventory_tracking._variant_line", 1),
    "I06": ("inventory", "register.update_variant_inventory_tracking", 1),
    "I07": ("inventory", "register.update_variant_inventory_tracking", 2),
    "I08": ("inventory", "register.update_variant_inventory_quantity", 1),
    "I09": ("inventory", "register.update_variant_inventory_quantity", 2),
    "I10": ("inventory", "register.update_variant_inventory_quantity", 3),
    "C01": ("catalog_hygiene", "register.update_product_pricing", 1),
    "C02": ("catalog_hygiene", "register.update_product_pricing", 2),
    "C03": ("catalog_hygiene", "register.update_product_pricing", 3),
    "C04": ("catalog_hygiene", "register.update_variant_image_binding", 1),
    "C05": ("catalog_hygiene", "register.update_variant_image_binding", 2),
    "C06": ("catalog_hygiene", "register.update_variant_image_binding", 3),
    "C07": ("catalog_hygiene", "register.get_product_metafields", 1),
    "C08": ("catalog_hygiene", "register.get_product_metafields", 2),
    "L01": ("collections", "register.get_collection", 1),
    "L02": ("collections", "register.update_collection", 1),
    "L03": ("collections", "register._membership_mutation", 1),
    "B01": ("publications", "_render_publications_report", 1),
    "B02": ("publications", "register.publish_product_to_channels", 1),
    "B03": ("publications", "register.unpublish_product_from_channels", 1),
    "B04": ("publications", "register._collection_target", 1),
    "B05": ("publications", "register.set_product_publications", 1),
    "B06": ("publications", "register.set_product_publications", 2),
    "M01": ("media._list", "_render_media_list", 1),
    "D01": ("discounts", "register.get_discount_codes", 1),
}


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------


def _title_key(node: ast.AST) -> str | None:
    """The title key when node is a literal-key read of one, else None."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        key = node.slice
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        key = node.args[0]
    else:
        return None
    if isinstance(key, ast.Constant) and key.value in TITLE_KEYS:
        return str(key.value)
    return None


def _norm(node: ast.expr) -> str:
    """Same text for x["title"] and x.get("title"), so a guard matches its fence."""
    key = _title_key(node)
    if key is not None:
        base = node.value if isinstance(node, ast.Subscript) else node.func.value
        return f"{ast.dump(base)}[{key}]"
    return ast.dump(node)


class _Module:
    def __init__(self, name: str, source: str) -> None:
        self.name = name
        self.label = "src/shopify_mcp/tools/" + name.replace(".", "/") + ".py"
        self.tree = ast.parse(source)
        self.parent: dict[ast.AST, ast.AST] = {}
        self.scope: dict[ast.AST, _Scope] = {}
        self.qualname: dict[ast.AST, str] = {self.tree: "<module>"}
        self.local_defs: dict[ast.AST, dict[str, _Func]] = {self.tree: {}}
        # Names a function binds itself (parameters and assignments).
        self.local_names: dict[ast.AST, set[str]] = {self.tree: set()}
        self._index(self.tree, self.tree, "")
        # Module-level bindings, in source order: the last one wins. The tools
        # use absolute imports only; a relative one binds a name that is not
        # the shared helper, so its fences fail (it errs toward failing).
        self.bindings: dict[str, str | _Func | None] = {}
        for stmt in self.tree.body:
            if isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    self.bindings[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    self.bindings[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.bindings[stmt.name] = stmt
            elif isinstance(stmt, ast.Assign):
                names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                self.bindings.update(dict.fromkeys(names))

    def _index(self, node: ast.AST, scope: _Scope, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            self.parent[child] = node
            self.scope[child] = scope
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                self.local_names[scope].add(child.id)
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                name = getattr(child, "name", "<lambda>")
                self.qualname[child] = prefix + name
                self.local_defs[child] = {}
                args = child.args
                self.local_names[child] = {
                    a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                }
                if not isinstance(child, ast.Lambda):
                    self.local_defs[scope][child.name] = child
                self._index(child, child, prefix + name + ".")
            else:
                self._index(child, scope, prefix)

    def function_of(self, node: ast.AST) -> str:
        return self.qualname[self.scope[node]]


def _is_tool(fn: _Scope) -> bool:
    decorators = getattr(fn, "decorator_list", [])
    return any(ast.unparse(d).startswith("server.tool") for d in decorators)


class _Scanner:
    def __init__(self, sources: Mapping[str, str]) -> None:
        self.modules = {name: _Module(name, src) for name, src in sorted(sources.items())}
        # Every call to a function defined in the scanned modules.
        self.callers: dict[ast.AST, list[tuple[_Module, ast.Call]]] = {}
        for mod in self.modules.values():
            for node in ast.walk(mod.tree):
                if isinstance(node, ast.Call):
                    target = self.resolve(mod, node.func, node)
                    if isinstance(target, tuple):
                        self.callers.setdefault(target[1], []).append((mod, node))
        self.uses: list[Use] = []

    # ---- name resolution ------------------------------------------------

    def resolve(
        self, mod: _Module, func: ast.expr, at: ast.AST
    ) -> tuple[_Module, _Func] | str | None:
        """A scanned def, a dotted name, or None when it cannot be named."""
        if isinstance(func, ast.Name):
            scope: ast.AST = mod.scope[at]
            while scope is not mod.tree:
                if func.id in mod.local_defs[scope]:
                    return mod, mod.local_defs[scope][func.id]
                if func.id in mod.local_names[scope]:
                    return None  # a local variable or parameter: not nameable
                scope = mod.scope[scope]
            binding = mod.bindings.get(func.id)
            if isinstance(binding, ast.FunctionDef | ast.AsyncFunctionDef):
                own = f"{PACKAGE}.{mod.name}.{func.id}"
                return own if own in _NAMED_CALLEES else (mod, binding)
            if func.id in mod.bindings and binding is None:
                return None
            dotted = binding or (f"builtins.{func.id}" if hasattr(builtins, func.id) else None)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = mod.bindings.get(func.value.id)
            dotted = f"{base}.{func.attr}" if isinstance(base, str) else None
        else:
            dotted = None
        if dotted is None or dotted in _NAMED_CALLEES:
            return dotted
        owner, _, attr = dotted.rpartition(".")
        target = self.modules.get(owner.removeprefix(PACKAGE + "."))
        if owner.startswith(PACKAGE + ".") and target is not None:
            binding = target.bindings.get(attr)
            if isinstance(binding, ast.FunctionDef | ast.AsyncFunctionDef):
                return target, binding
        return dotted

    def is_wrap(self, mod: _Module, node: ast.expr) -> bool:
        return isinstance(node, ast.Call) and self.resolve(mod, node.func, node) == WRAP

    # ---- the walk ---------------------------------------------------------

    def scan(self) -> None:
        for mod in self.modules.values():
            for node in ast.walk(mod.tree):
                key = _title_key(node)
                if key is not None:
                    self.origin = (mod, node, key)
                    self.visited: set[tuple[int, str, _Path, tuple[int, ...]]] = set()
                    self.follow(mod, node, (), [ast.unparse(node)], ())

    def end(self, mod, at, trail, dest, fence=None):
        omod, read, key = self.origin
        self.uses.append(
            Use(
                module=omod.name,
                function=omod.function_of(read),
                line=read.lineno,
                key=key,
                pattern=" > ".join(trail) + " => " + dest,
                dest=dest,
                site=f"{mod.label}:{at.lineno} {mod.function_of(at)}",
                fence=None
                if fence is None
                else (mod.name, mod.function_of(fence), (fence.lineno, fence.col_offset)),
            )
        )

    def follow(self, mod, node, path, trail, stack):
        """Classify the use of a title-holding node by its parent.

        path says where the title sits inside node: () is the text itself,
        a str is a dict key, an int a tuple index, "*" any list item.
        stack holds the calls the walk entered, so a return goes back there.
        """
        p = mod.parent[node]
        if isinstance(p, ast.keyword):
            self.call_arg(mod, mod.parent[p], node, p.arg, path, trail, stack)
        elif isinstance(p, ast.Call) and node is not p.func:
            self.call_arg(mod, p, node, None, path, trail, stack)
        elif isinstance(p, ast.IfExp):
            self.if_exp(mod, p, node, path, trail, stack)
        elif isinstance(p, ast.FormattedValue) and node is p.value:
            self.follow(mod, mod.parent[p], (), [*trail, "f-string"], stack)
        elif isinstance(p, ast.BinOp) and isinstance(p.op, ast.Add):
            self.follow(mod, p, path, [*trail, "+"], stack)
        elif isinstance(p, ast.BoolOp) and not self.in_test(mod, p):
            self.follow(mod, p, path, [*trail, "or-and"], stack)
        elif self.in_test(mod, node):
            self.end(mod, node, [*trail, "truth test"], PREDICATE)
        elif isinstance(p, ast.Compare) and self.in_test(mod, p):
            self.end(mod, node, [*trail, "compare"], PREDICATE)
        elif isinstance(p, ast.ListComp | ast.GeneratorExp) and node is p.elt:
            self.follow(mod, p, ("*", *path), [*trail, "[for]"], stack)
        elif isinstance(p, ast.comprehension) and node is p.iter:
            self.bind(mod, p.target, mod.parent[p], path[1:], [*trail, "for"], stack)
        elif isinstance(p, ast.For) and node is p.iter:
            self.bind(mod, p.target, mod.scope[p], path[1:], [*trail, "for"], stack)
        elif isinstance(p, ast.Dict) and node in p.values:
            key = p.keys[p.values.index(node)]
            k = key.value if isinstance(key, ast.Constant) else "*"
            self.follow(mod, p, (k, *path), [*trail, f"{{{k!r}}}"], stack)
        elif isinstance(p, ast.List):
            self.follow(mod, p, ("*", *path), [*trail, "[]"], stack)
        elif isinstance(p, ast.Tuple):
            i = p.elts.index(node)
            self.follow(mod, p, (i, *path), [*trail, f"(#{i})"], stack)
        elif isinstance(p, ast.Subscript) and node is p.value:
            k = p.slice.value if isinstance(p.slice, ast.Constant) else None
            self.select(mod, p, k, isinstance(p.slice, ast.Slice), path, trail, stack)
        elif isinstance(p, ast.Attribute):
            self.method(mod, p, path, trail, stack)
        elif isinstance(p, ast.Assign | ast.AnnAssign):
            targets = p.targets if isinstance(p, ast.Assign) else [p.target]
            for target in targets:
                self.bind(mod, target, mod.scope[p], path, trail, stack)
        elif isinstance(p, ast.Return):
            self.returned(mod, p, path, trail, stack)
        else:
            self.end(mod, node, [*trail, f"?{type(p).__name__}"], UNSUPPORTED)

    def in_test(self, mod, node):
        """True when node is used only for its truth value."""
        p = mod.parent[node]
        if isinstance(p, ast.If | ast.While | ast.IfExp) and node is p.test:
            return True
        if isinstance(p, ast.comprehension) and node in p.ifs:
            return True
        if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
            return True
        return isinstance(p, ast.BoolOp) and self.in_test(mod, p)

    def if_exp(self, mod, p, node, path, trail, stack):
        fence = p.body if self.is_wrap(mod, p.body) and len(p.body.args) == 1 else None
        guards = fence is not None and _norm(fence.args[0]) == _norm(p.test)
        if guards and (node is p.test or (node is p.orelse and _norm(node) == _norm(p.test))):
            # The guard test and the falsy branch of `wrap(t) if t else t`.
            self.end(mod, fence, [*trail, "fence guard"], FENCED, fence=fence)
        elif node is p.test:
            self.end(mod, node, [*trail, "truth test"], PREDICATE)
        else:
            self.follow(mod, p, path, [*trail, "if-else"], stack)

    def bind(self, mod, target, scope, path, trail, stack):
        if isinstance(target, ast.Name):
            self.alias(mod, scope, target.id, path, [*trail, f"={target.id}"], stack)
        elif isinstance(target, ast.Tuple) and path and isinstance(path[0], int):
            self.bind(mod, target.elts[path[0]], scope, path[1:], trail, stack)
        else:
            self.end(mod, target, [*trail, f"?bind {type(target).__name__}"], UNSUPPORTED)

    def alias(self, mod, scope, name, path, trail, stack):
        mark = (id(scope), name, path, tuple(id(entry[1]) for entry in stack))
        if mark in self.visited:
            return
        self.visited.add(mark)
        for load in _loads(scope, name):
            self.follow(mod, load, path, trail, stack)

    def select(self, mod, expr, k, is_slice, path, trail, stack):
        """x[k] or x.get(k) on a title-holding value."""
        if _title_key(expr) is not None:
            return  # a title read of its own: it has its own walk
        if not path or is_slice:
            self.follow(mod, expr, path, [*trail, "[:]"], stack)
        elif k is None or k == path[0] or path[0] == "*":
            self.follow(mod, expr, path[1:], [*trail, f"[{k!r}]"], stack)
        # Another constant key of a dict with constant keys holds no title:
        # the walk stops, clean.

    def method(self, mod, p, path, trail, stack):
        call = mod.parent[p]
        if isinstance(call, ast.Call) and call.func is p and p.attr == "get" and call.args:
            k = call.args[0].value if isinstance(call.args[0], ast.Constant) else None
            self.select(mod, call, k, False, path, trail, stack)
        elif isinstance(call, ast.Call) and call.func is p and p.attr == "append":
            return  # adding to a title-holding list moves no title
        else:
            self.end(mod, p, [*trail, f"?.{p.attr}"], UNSUPPORTED)

    def call_arg(self, mod, call, node, kw, path, trail, stack):
        target = self.resolve(mod, call.func, call)
        if target == WRAP and node in call.args:
            self.end(mod, call, [*trail, "wrap()"], FENCED, fence=call)
        elif target in PASSTHROUGH:
            name = str(target).rpartition(".")[2]
            self.follow(mod, call, path, [*trail, f"{name}()"], stack)
        elif target in SINKS:
            self.end(mod, call, [*trail, f"{target}()"], SINKS[str(target)])
        elif target == WRITE_GATE and kw in WRITE_GATE_KWARGS:
            self.end(mod, call, [*trail, f"write_gate({kw}=)"], WRITE_GATE_KWARGS[kw])
        elif isinstance(target, str) and target.startswith(SHOPIFY_OPS):
            self.end(mod, call, [*trail, f"ops.{target.rpartition('.')[2]}()"], SHOPIFY_CALL)
        elif isinstance(target, tuple):
            self.enter(mod, call, node, kw, target, path, trail, stack)
        elif isinstance(call.func, ast.Attribute) and call.func.attr == "join":
            self.follow(mod, call, (), [*trail, ".join()"], stack)
        elif (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "append"
            and isinstance(call.func.value, ast.Name)
        ):
            lst = call.func.value.id
            step = f"{lst}.append()"
            self.alias(mod, mod.scope[call], lst, ("*", *path), [*trail, step], stack)
        else:
            self.end(mod, call, [*trail, f"?call {ast.unparse(call.func)}"], UNSUPPORTED)

    def enter(self, mod, call, node, kw, target, path, trail, stack):
        """Map an argument to the helper's named parameter and follow it there."""
        cmod, fn = target
        args = fn.args
        positional = [a.arg for a in (*args.posonlyargs, *args.args)]
        by_name = {*positional[len(args.posonlyargs) :], *(a.arg for a in args.kwonlyargs)}
        if kw is not None:
            param = kw if kw in by_name else None
        else:
            i = call.args.index(node) if node in call.args else len(positional)
            param = positional[i] if i < len(positional) else None
        if param is None:
            # **kwargs, *args or a ** splat: no named parameter to follow.
            step = f"?{cmod.qualname[fn]}() takes it with no named parameter"
            self.end(mod, call, [*trail, step], UNSUPPORTED)
            return
        step = f"-> {cmod.qualname[fn]}({param}=)"
        self.alias(cmod, fn, param, path, [*trail, step], (*stack, (mod, call, fn)))

    def returned(self, mod, ret, path, trail, stack):
        fn = mod.scope[ret]
        if _is_tool(fn):
            self.end(mod, ret, [*trail, "tool return"], DISPLAY)
        elif stack and stack[-1][2] is fn:
            cmod, call, _fn = stack[-1]
            self.follow(cmod, call, path, [*trail, "return"], stack[:-1])
        else:
            mark = (id(fn), "<return>", path, tuple(id(entry[1]) for entry in stack))
            if mark in self.visited:
                return
            self.visited.add(mark)
            sites = self.callers.get(fn, [])
            if not sites:
                self.end(mod, ret, [*trail, "?return with no caller"], UNSUPPORTED)
            for cmod, call in sites:
                step = f"return to {cmod.function_of(call)}"
                self.follow(cmod, call, path, [*trail, step], stack)


def _loads(scope: ast.AST, name: str) -> Iterator[ast.Name]:
    """Loads of name in scope, also in nested scopes that do not rebind it."""
    children = [scope.body] if isinstance(scope, ast.Lambda) else ast.iter_child_nodes(scope)
    for child in children:
        yield from _loads_in(child, name)


def _loads_in(node: ast.AST, name: str) -> Iterator[ast.Name]:
    if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
        yield node
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = node.args
        if name in {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}:
            return
    for child in ast.iter_child_nodes(node):
        yield from _loads_in(child, name)


def _scan(sources: Mapping[str, str]) -> tuple[list[Use], dict[str, str]]:
    scanner = _Scanner(sources)
    scanner.scan()
    return scanner.uses, {m.name: m.label for m in scanner.modules.values()}


@functools.cache
def _real_scan() -> tuple[list[Use], dict[str, str]]:
    """The scan of the unmutated tree, once per test session."""
    return _scan(_real_sources())


def run_census(
    sources: Mapping[str, str],
    rows: Sequence[Row] = EXCEPTION_ROWS,
    fence_points: Mapping[str, tuple[str, str, int]] = FENCE_POINTS,
    fence_lines: Mapping[str, set[tuple[str, int]]] | None = None,
) -> Census:
    """The one entry point: scan the sources, check the rows and the fence ids.

    The real tree (the cached ``_real_sources()`` object) is scanned once; a
    mutated copy is always scanned again. ``fence_lines``, when given, holds
    the (module, line) of the ``wrap()`` call that fenced each id's value at
    run time; each mapped fence point must sit on one of those lines.
    """
    uses, labels = _real_scan() if sources is _real_sources() else _scan(sources)
    census = Census(uses=list(uses))

    # Fence sites: (module, function, n-th distinct title fence in it).
    fences: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for use in census.uses:
        if use.fence is not None:
            module, function, position = use.fence
            fences.setdefault((module, function), set()).add(position)
    for (module, function), positions in fences.items():
        for n, (line, _col) in enumerate(sorted(positions), start=1):
            census.fence_sites[(module, function, n)] = line

    def where(use: Use) -> str:
        return f"{use.site} (title read `{use.pattern.split(' > ')[0]}` at {labels[use.module]}:{use.line} in {use.function})"

    by_key: dict[tuple[str, str, str, str], list[Use]] = {}
    for use in census.uses:
        if use.dest == DISPLAY:
            census.problems.append(
                f"{where(use)}: unfenced display of a store title: {use.pattern}"
            )
        elif use.dest == UNSUPPORTED:
            census.problems.append(f"{where(use)}: unsupported use of a store title: {use.pattern}")
        elif use.dest != FENCED:
            by_key.setdefault((use.module, use.function, use.key, use.pattern), []).append(use)

    seen_rows: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (row.module, row.function, row.key, row.pattern)
        label = f"{labels.get(row.module, row.module)} {row.function}"
        if key in seen_rows:
            census.problems.append(f"{label}: duplicate exception row: {row.pattern}")
        seen_rows.add(key)
        if not row.reason.strip():
            census.problems.append(f"{label}: exception row has no reason: {row.pattern}")
        if row.dest not in ALLOWED_DESTS or not row.pattern.endswith("=> " + row.dest):
            census.problems.append(f"{label}: exception row has a bad destination {row.dest}")
        matched = by_key.pop(key, [])
        if not matched:
            census.problems.append(f"{label}: stale exception row (0 matches): {row.pattern}")
        elif len(matched) != row.count:
            at = ", ".join(sorted({u.site for u in matched}))
            census.problems.append(
                f"{label}: exception row matched {len(matched)} uses, expected {row.count}"
                f" (at {at}): {row.pattern}"
            )
    for matched in by_key.values():
        for use in matched:
            census.problems.append(
                f"{where(use)}: unclassified {use.dest} use, no exception row: {use.pattern}"
            )

    for fp, site in sorted(fence_points.items()):
        line = census.fence_sites.get(site)
        if line is None:
            census.problems.append(f"{fp}: APPENDIX_B fence point {site} maps to no FENCED use")
        elif fence_lines is not None and (site[0], line) not in fence_lines.get(fp, set()):
            seen = sorted(fence_lines.get(fp, set()))
            census.problems.append(
                f"{fp}: mapped to {site} (wrap() at line {line}), but its CASES rows"
                f" fence it at {seen}"
            )
    census.problems.sort()
    return census


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@functools.cache
def _real_sources() -> dict[str, str]:
    out = {}
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        rel = path.relative_to(TOOLS_DIR).with_suffix("")
        out[".".join(rel.parts)] = path.read_text(encoding="utf-8")
    return out


@functools.cache
def _observed_fence_lines() -> dict[str, set[tuple[str, int]]]:
    """Run every CASES row of test_title_fencing.py and record, for each fence
    id, the (module, line) of the wrap() call that fenced that id's distinct
    title value. This is evidence from run time, not from the census."""
    real_wrap = _untrusted.wrap
    calls: list[tuple[str, int, str]] = []

    def recording_wrap(text: str) -> str:
        caller = sys._getframe(1)
        calls.append((caller.f_code.co_filename, caller.f_lineno, text))
        return real_wrap(text)

    observed: dict[str, set[tuple[str, int]]] = {}
    with ExitStack() as patches:
        for name in _real_sources():
            module = importlib.import_module(f"{PACKAGE}.{name}")
            if getattr(module, "wrap", None) is real_wrap:
                patches.enter_context(mock.patch.object(module, "wrap", recording_wrap))
            if hasattr(module, "log_write"):
                patches.enter_context(
                    mock.patch.object(module, "log_write", lambda *_a, **_k: None)
                )
        for case in CASES:
            titles = _defaults(case)
            fp_of = {value: fp for fp, value in titles.items()}
            calls.clear()
            _run_case(case, titles)
            for filename, line, text in calls:
                if text in fp_of:
                    rel = Path(filename).relative_to(TOOLS_DIR).with_suffix("")
                    observed.setdefault(fp_of[text], set()).add((".".join(rel.parts), line))
    return observed


def _mutated(module: str, old: str, new: str) -> dict[str, str]:
    sources = dict(_real_sources())
    assert sources[module].count(old) == 1, f"{module}: {old!r} is not unique"
    sources[module] = sources[module].replace(old, new)
    return sources


def _assert_fails_naming(census: Census, function: str, kind: str) -> None:
    hits = [p for p in census.problems if function in p and kind in p]
    assert hits, f"no {kind!r} problem names {function}:\n" + "\n".join(census.problems)


def test_real_tree_has_no_unfenced_or_unclassified_title_use():
    census = run_census(_real_sources())
    assert census.problems == []


def test_census_agrees_with_appendix_b_by_fence_point_identity():
    assert len(APPENDIX_B) == 45
    assert set(FENCE_POINTS) == set(APPENDIX_B)
    assert len(set(FENCE_POINTS.values())) == len(FENCE_POINTS)
    observed = _observed_fence_lines()
    assert set(observed) == set(APPENDIX_B)
    census = run_census(_real_sources(), fence_lines=observed)
    assert census.problems == []
    missing = sorted(fp for fp, site in FENCE_POINTS.items() if site not in census.fence_sites)
    assert missing == []
    case_modules = {f.fp: c.module.__name__ for c in CASES for f in c.fences}
    for fp, (module, _function, _n) in FENCE_POINTS.items():
        assert f"{PACKAGE}.{module}".startswith(case_modules[fp]), fp


def test_an_id_mapped_to_the_wrong_fence_in_its_module_fails():
    swapped = dict(FENCE_POINTS)
    swapped["P09"], swapped["P10"] = FENCE_POINTS["P10"], FENCE_POINTS["P09"]
    census = run_census(_real_sources(), fence_points=swapped, fence_lines=_observed_fence_lines())
    _assert_fails_naming(census, "P09", "but its CASES rows fence it at")
    _assert_fails_naming(census, "P10", "but its CASES rows fence it at")


def test_every_swap_of_two_ids_in_one_module_fails():
    observed = _observed_fence_lines()
    ids = sorted(FENCE_POINTS)
    pairs = [
        (a, b)
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
        if FENCE_POINTS[a][0] == FENCE_POINTS[b][0]
    ]
    assert len(pairs) == 211
    for a, b in pairs:
        swapped = dict(FENCE_POINTS)
        swapped[a], swapped[b] = FENCE_POINTS[b], FENCE_POINTS[a]
        census = run_census(_real_sources(), fence_points=swapped, fence_lines=observed)
        assert census.problems, f"swapping {a} and {b} is not caught"


def test_every_helper_table_entry_names_a_real_function():
    for dotted in (WRAP, WRITE_GATE, *PASSTHROUGH, *SINKS):
        owner, _, attr = dotted.rpartition(".")
        assert callable(getattr(importlib.import_module(owner), attr)), dotted


def test_the_validator_row_holds_format_validation_diff_never_shows_the_old_title():
    old = "Vanish | Old" + chr(0x2013) + "Marker-7f3a"
    out = format_validation_diff(old, "Tee")
    assert "Marker-7f3a" not in out
    assert "Tee" in out


# ---- mutation (a): a new raw title render ---------------------------------


def test_a_new_raw_render_in_a_scratch_copy_of_a_tool_fails():
    source = _real_sources()["products"]
    tree = ast.parse(source)
    register = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register")
    tool = next(
        n for n in register.body if isinstance(n, ast.FunctionDef) and n.name == "get_product"
    )
    lines = source.splitlines(keepends=True)
    start, end = tool.decorator_list[0].lineno - 1, tool.end_lineno
    assert end is not None
    copy = "".join(lines[start:end])
    handle_line = "f\"Handle: {p['handle']}\\n\""
    assert copy.count(handle_line) == 1
    copy = copy.replace("def get_product(", "def get_product_scratch(").replace(
        handle_line, handle_line + "\n            f\"Raw: {p['title']}\\n\""
    )
    sources = dict(_real_sources())
    sources["products"] = "".join(lines[:end]) + "\n" + copy + "".join(lines[end:])
    assert "def get_product_scratch(" in sources["products"]
    census = run_census(sources)
    _assert_fails_naming(census, "register.get_product_scratch", "unfenced display")
    # The original tool and its fences are still there and still pass.
    assert ("products", "register.get_product", 2) in census.fence_sites


def test_a_new_raw_render_in_an_existing_tool_fails():
    old = "f\"Handle: {col['handle']}\\n\""
    sources = _mutated("collections", old, old + "\n            f\"Name: {col['title']}\\n\"")
    _assert_fails_naming(run_census(sources), "register.get_collection", "unfenced display")


# ---- mutation (b): wrap() removed at one fence point per module ----------

WRAP_REMOVALS = [
    ("products", "{wrap(col_title) if", "{(col_title) if", "register.get_product_collections"),
    (
        "inventory",
        '    title = wrap(variant["title"])',
        '    title = (variant["title"])',
        "_pair_prefix",
    ),
    (
        "catalog_hygiene",
        'wrap(v["variantTitle"])',
        '(v["variantTitle"])',
        "register.get_product_metafields",
    ),
    (
        "publications",
        "    shown_title = wrap(title) if title else title\n    meta",
        "    shown_title = (title) if title else title\n    meta",
        "_render_publications_report",
    ),
    (
        "collections",
        'col_title = wrap(col["title"])',
        'col_title = (col["title"])',
        "register._membership_mutation",
    ),
    ("media._list", "({wrap(title) if title", "({(title) if title", "_render_media_list"),
    (
        "discounts",
        "shown_title = wrap(title)",
        "shown_title = (title)",
        "register.get_discount_codes",
    ),
]


@pytest.mark.parametrize(
    ("module", "old", "new", "function"), WRAP_REMOVALS, ids=[m[0] for m in WRAP_REMOVALS]
)
def test_removing_wrap_at_one_fence_point_per_module_fails(module, old, new, function):
    census = run_census(_mutated(module, old, new))
    _assert_fails_naming(census, function, "unfenced display")


def test_removing_wrap_at_one_fence_point_also_loses_its_appendix_b_id():
    census = run_census(_mutated("media._list", "({wrap(title) if title", "({(title) if title"))
    _assert_fails_naming(census, "M01", "maps to no FENCED use")


# ---- the fence must be the shared helper ----------------------------------


def test_a_local_function_named_wrap_is_not_the_fence():
    old = "from shopify_mcp.tools._untrusted import with_reminder, wrap\n"
    new = (
        "from shopify_mcp.tools._untrusted import with_reminder\n\n\n"
        "def wrap(text: str) -> str:\n    return text\n"
    )
    census = run_census(_mutated("media._list", old, new))
    _assert_fails_naming(census, "_render_media_list", "unfenced display")


def test_a_wrap_imported_from_another_module_is_not_the_fence():
    old = "from shopify_mcp.tools._untrusted import with_reminder, wrap\n"
    new = (
        "from shopify_mcp.tools._untrusted import with_reminder\n"
        "from shopify_mcp.tools._scrub import cap as wrap\n"
    )
    census = run_census(_mutated("media._list", old, new))
    _assert_fails_naming(census, "_render_media_list", "unfenced display")


def test_a_local_variable_named_wrap_is_not_the_fence():
    old = '    title = product.get("title", "")\n'
    census = run_census(_mutated("media._list", old, "    wrap = str\n" + old))
    _assert_fails_naming(census, "_render_media_list", "unfenced display")
    _assert_fails_naming(census, "M01", "maps to no FENCED use")


# ---- mutation (c): raw-local reuse in a second display line --------------


def test_reusing_a_raw_title_local_in_a_second_display_line_fails():
    old = 'f"Product: {title} ({pid_display})",\n'
    sources = _mutated("catalog_hygiene", old, old + '            f"Again: {raw_title}",\n')
    mutated = sources["catalog_hygiene"]
    # The original fence, the title-key reads and the JSON use all stay.
    assert mutated.count("title = wrap(raw_title) if raw_title else") == 1
    assert mutated.count('raw_title = product_summary["title"]') == 1
    assert mutated.count('"title": product_node.get("title"),') == 2
    assert mutated.count("product=product_summary,") == 1
    census = run_census(sources)
    _assert_fails_naming(census, "register.get_product_metafields", "unfenced display")
    assert ("catalog_hygiene", "register.get_product_metafields", 1) in census.fence_sites
    json_rows = [r for r in EXCEPTION_ROWS if r.function == "register.get_product_metafields"]
    assert not [p for p in census.problems if any(r.pattern in p for r in json_rows)]


# ---- an unsupported use fails ---------------------------------------------


MEDIA_TITLE = '    title = product.get("title", "")\n'

UNSUPPORTED_USES = {
    "unknown callee": "    print(title)\n",
    "dict key": "    _k = {title: 1}\n",
    "attribute target": "    product.label = title\n",
    "get with no argument": "    _g = title.get()\n",
}


@pytest.mark.parametrize("snippet", UNSUPPORTED_USES.values(), ids=UNSUPPORTED_USES.keys())
def test_an_unsupported_use_of_a_title_value_fails(snippet):
    sources = _mutated("media._list", MEDIA_TITLE, MEDIA_TITLE + snippet)
    _assert_fails_naming(run_census(sources), "_render_media_list", "unsupported use")


def test_a_helper_that_returns_a_title_to_no_caller_fails():
    sources = dict(_real_sources())
    sources["media._list"] += "\n\ndef _scratch_title(product):\n    return product['title']\n"
    _assert_fails_naming(run_census(sources), "_scratch_title", "return with no caller")


UNCLASSIFIED_USES = {
    "length": ("    _n = len(title)\n", LENGTH),
    "not": ("    _b = not title\n", PREDICATE),
    "comprehension if": ('    _c = [ch for ch in "ab" if title]\n', PREDICATE),
    "comparison": ('    if title == "x":\n        pass\n', PREDICATE),
}


@pytest.mark.parametrize(
    ("snippet", "dest"), UNCLASSIFIED_USES.values(), ids=UNCLASSIFIED_USES.keys()
)
def test_a_new_permitted_use_without_a_row_fails_as_unclassified(snippet, dest):
    sources = _mutated("media._list", MEDIA_TITLE, MEDIA_TITLE + snippet)
    _assert_fails_naming(run_census(sources), "_render_media_list", f"unclassified {dest} use")


def test_a_nested_parameter_with_the_same_name_is_not_the_title():
    shadow = '    def _shadow(title: str) -> str:\n        return f"{title}"\n\n'
    sources = _mutated("media._list", MEDIA_TITLE, MEDIA_TITLE + shadow)
    assert run_census(sources).problems == []


# ---- container paths ------------------------------------------------------

COLLECTION_HEAD = "        head = (\n"

CONTAINER_RENDERS = {
    "dict read back by its key": ('_box = {"t": col["title"]}', "f\"Box: {_box['t']}\\n\""),
    "dict read back by get": ('_box = {"t": col["title"]}', "f\"Box: {_box.get('t')}\\n\""),
    "slice of the title": ("_box = 0", "f\"Cut: {col['title'][:5]}\\n\""),
}


def _with_head_line(prelude: str, line: str) -> dict[str, str]:
    new = f"        {prelude}\n{COLLECTION_HEAD}            {line}\n"
    return _mutated("collections", COLLECTION_HEAD, new)


@pytest.mark.parametrize(
    ("prelude", "line"), CONTAINER_RENDERS.values(), ids=CONTAINER_RENDERS.keys()
)
def test_a_title_rendered_through_a_container_path_fails(prelude, line):
    census = run_census(_with_head_line(prelude, line))
    _assert_fails_naming(census, "register.get_collection", "unfenced display")


def test_another_key_of_a_title_holding_dict_is_clean():
    sources = _with_head_line('_box = {"t": col["title"], "n": 1}', "f\"Box: {_box['n']}\\n\"")
    assert "_box['n']" in sources["collections"]
    assert run_census(sources).problems == []


def test_a_dict_with_a_computed_key_follows_every_key_read():
    prelude = 'key_name = "t"\n        _box = {key_name: col["title"]}'
    sources = _with_head_line(prelude, "f\"Box: {_box['label']}\\n\"")
    _assert_fails_naming(run_census(sources), "register.get_collection", "unfenced display")


HELPERS_WITHOUT_A_NAMED_PARAMETER = {
    "**kwargs": (
        "def _line(**fields):\n    return f\"Name: {fields['name']}\"\n",
        "_line(name=col['title'])",
    ),
    "*args": ('def _line(*parts):\n    return "".join(parts)\n', "_line(col['title'])"),
    "** splat": (
        'def _line(name):\n    return f"Name: {name}"\n',
        "_line(**{'name': col['title']})",
    ),
}


@pytest.mark.parametrize(
    ("helper", "call"),
    HELPERS_WITHOUT_A_NAMED_PARAMETER.values(),
    ids=HELPERS_WITHOUT_A_NAMED_PARAMETER.keys(),
)
def test_a_title_passed_to_a_helper_with_no_named_parameter_fails(helper, call):
    sources = _with_head_line("_box = 0", f'f"Name: {{{call}}}\\n"')
    sources["collections"] += "\n\n" + helper
    census = run_census(sources)
    _assert_fails_naming(
        census, "register.get_collection", "?_line() takes it with no named parameter"
    )


# ---- mutation (d): exception rows -----------------------------------------


def test_an_exception_row_with_no_reason_fails():
    rows = list(EXCEPTION_ROWS)
    rows[0] = replace(rows[0], reason="  ")
    census = run_census(_real_sources(), rows)
    _assert_fails_naming(census, rows[0].function, "no reason")


def test_an_exception_row_with_a_wrong_count_fails():
    rows = list(EXCEPTION_ROWS)
    rows[0] = replace(rows[0], count=rows[0].count + 1)
    census = run_census(_real_sources(), rows)
    _assert_fails_naming(census, rows[0].function, f"expected {rows[0].count}")


def test_a_stale_exception_row_fails():
    stale = replace(EXCEPTION_ROWS[0], pattern=EXCEPTION_ROWS[0].pattern + " (gone)")
    census = run_census(_real_sources(), [*EXCEPTION_ROWS, stale])
    _assert_fails_naming(census, stale.function, "stale exception row")


def test_a_duplicate_exception_row_fails():
    census = run_census(_real_sources(), [*EXCEPTION_ROWS, EXCEPTION_ROWS[0]])
    _assert_fails_naming(census, EXCEPTION_ROWS[0].function, "duplicate exception row")


def test_an_exception_row_cannot_permit_a_display_use():
    rows = list(EXCEPTION_ROWS)
    rows[0] = replace(rows[0], dest=DISPLAY)
    census = run_census(_real_sources(), rows)
    _assert_fails_naming(census, rows[0].function, "bad destination")
