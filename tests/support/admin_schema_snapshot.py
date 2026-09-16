"""Generate and discover the Admin API schema snapshot the contract suite checks (Story 9.16).

Three pieces, shared by the offline contract test
(``tests/unit/shopify/test_admin_schema_contract.py``) and the live refresh
runner (``tests/live/test_admin_schema_snapshot.py``) so the two can never
disagree about which documents exist or how the snapshot is trimmed:

- :func:`discover_documents` sweeps every module under ``src/shopify_mcp`` for
  module-level UPPER_CASE string constants that parse into a GraphQL operation.
  Repo-wide by construction, never a file list: operations live outside
  ``shopify/queries`` too (``tools/media/_graphql.py``, ``client.py``).
- :func:`builder_documents` is the explicit registry for documents assembled at
  call time, which no constant sweep can see.
- :func:`generate_sdl` trims a full introspected schema down to exactly what
  those documents reach, as deterministic SDL.

**Deprecated arguments and input fields must be in the introspection.** The
standard introspection query omits them unless ``input_value_deprecation=True``.
Leaving them out is what made ``productUpdate(input:)`` look removed on
2026-01 when it is only deprecated. Shopify still accepts it, and a snapshot
without it rejects it: a false drift report. :data:`INTROSPECTION_OPTIONS` is
the one place the runner reads its introspection flags from.
"""

import ast
import importlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLError,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLType,
    GraphQLUnionType,
    OperationDefinitionNode,
    TypeInfo,
    TypeInfoVisitor,
    Visitor,
    get_named_type,
    parse,
    type_from_ast,
    visit,
)
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    InlineFragmentNode,
    VariableDefinitionNode,
)
from graphql.type import specified_scalar_types
from graphql.utilities.print_schema import print_block, print_deprecated, print_input_value

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "unit" / "shopify" / "admin_schema_snapshot.graphql"

# Entry points: importing them starts or configures the server.
_EXCLUDED_MODULES = frozenset({"shopify_mcp.__main__", "shopify_mcp.server"})

INTROSPECTION_OPTIONS: dict[str, bool] = {
    "descriptions": False,
    "input_value_deprecation": True,
}

_HEADER_RE = re.compile(r"^# api_version: (\S+)  captured: (\d{4}-\d{2}-\d{2})$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _is_operation_document(text: str) -> bool:
    try:
        document = parse(text)
    except GraphQLError:
        return False
    return any(isinstance(d, OperationDefinitionNode) for d in document.definitions)


def _module_level_assignments(path: Path) -> set[str]:
    """Names ASSIGNED at module level, i.e. defined here rather than imported.

    This is what dedupes re-exports: ``shopify/operations/*`` and ``tools/*``
    import the ``shopify/queries/*`` constants, so the same document is visible
    from up to three modules but is assigned in exactly one."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def discover_documents(src_root: Path = SRC_ROOT, package: str = "shopify_mcp") -> dict[str, str]:
    """Return ``{"<defining module>.<NAME>": document}`` for every constant operation.

    One entry per distinct document text, labelled by the module that assigns
    it. ``src_root`` must be on ``sys.path`` (it is for the installed package;
    the synthetic-module test prepends its ``tmp_path``)."""
    by_text: dict[str, str] = {}
    for path in sorted((src_root / package).rglob("*.py")):
        parts = path.relative_to(src_root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module_name = ".".join(parts)
        if module_name in _EXCLUDED_MODULES:
            continue
        module = importlib.import_module(module_name)
        for name in sorted(_module_level_assignments(path)):
            if not name.isupper():
                continue
            value = getattr(module, name, None)
            if isinstance(value, str) and _is_operation_document(value):
                by_text.setdefault(value, f"{module_name}.{name}")
    return {label: text for text, label in by_text.items()}


def builder_documents() -> dict[str, str]:
    """The documents built at call time, labelled ``<module>.<builder>[<variant>]``.

    Every module-level ``_build_*`` function in ``shopify_mcp.shopify.queries``
    must appear here; the contract suite fails on one that does not."""
    from shopify_mcp.shopify.queries import catalog_hygiene as q

    documents: dict[str, str] = {}
    for builder in (
        q._build_get_product_metafields_query,
        q._build_get_product_and_variant_metafields_query,
        q._build_get_product_variant_metafields_page_query,
    ):
        for mode in ("keys", "namespace", "none"):
            documents[f"{builder.__module__}.{builder.__name__}[{mode}]"] = builder(mode)

    # One document covering both resolution shapes the builder can emit.
    batch, _variables = q._build_batch_resolve_query(
        [
            {"mode": "gid", "gid": "gid://shopify/Metafield/1"},
            {
                "mode": "triple",
                "ownerId": "gid://shopify/Product/1",
                "ownerType": "PRODUCT",
                "namespace": "custom",
                "key": "k",
            },
        ]
    )
    batch_builder = q._build_batch_resolve_query
    documents[f"{batch_builder.__module__}.{batch_builder.__name__}[gid+triple]"] = batch
    return documents


def all_documents() -> dict[str, str]:
    """Every operation the package can send: constants plus builder outputs."""
    return {**discover_documents(), **builder_documents()}


# ---------------------------------------------------------------------------
# Snapshot header
# ---------------------------------------------------------------------------


def render_snapshot(api_version: str, captured: str, body: str) -> str:
    """The committed file: a header naming the SERVED version and capture date, then the SDL."""
    return (
        "# Trimmed Admin GraphQL API schema. GENERATED: do not hand-edit.\n"
        f"# api_version: {api_version}  captured: {captured}\n"
        "# Refresh: REFRESH_ADMIN_SCHEMA_SNAPSHOT=1 pytest tests/live/test_admin_schema_snapshot.py\n"
        "# (see README, 'Refreshing the Admin API schema snapshot').\n"
        "\n" + body
    )


def read_snapshot_header(text: str) -> tuple[str, str]:
    """Return ``(api_version, captured_date)`` from a snapshot's header."""
    match = _HEADER_RE.search(text)
    if match is None:
        raise ValueError("snapshot has no '# api_version: …  captured: …' header line")
    return match.group(1), match.group(2)


def snapshot_body(text: str) -> str:
    """The snapshot with its header removed: the part :func:`generate_sdl` produces."""
    lines = text.splitlines(keepends=True)
    while lines and (lines[0].startswith("#") or lines[0] == "\n"):
        lines.pop(0)
    return "".join(lines)


# ---------------------------------------------------------------------------
# SDL generation
# ---------------------------------------------------------------------------


class _Reach:
    """What a set of documents reaches in a schema."""

    def __init__(self, schema: GraphQLSchema) -> None:
        self.schema = schema
        self.types: set[str] = set()
        self.fields: dict[str, set[str]] = {}

    def add_type(self, type_: GraphQLType) -> None:
        named = get_named_type(type_)
        if named.name in self.types:
            return
        self.types.add(named.name)
        if isinstance(named, GraphQLInputObjectType):
            # Input objects in full: a coerced payload is checked against the
            # real field set, never a placeholder.
            for field in named.fields.values():
                self.add_type(field.type)

    def add_field(self, parent: GraphQLNamedType, name: str) -> bool:
        assert isinstance(parent, (GraphQLObjectType, GraphQLInterfaceType))
        selected = self.fields.setdefault(parent.name, set())
        if name in selected:
            return False
        selected.add(name)
        self.add_type(parent)
        field = parent.fields[name]
        self.add_type(field.type)
        for arg in field.args.values():
            self.add_type(arg.type)
        return True


class _Collector(Visitor):
    def __init__(self, reach: _Reach, type_info: TypeInfo) -> None:
        super().__init__()
        self.reach = reach
        self.type_info = type_info

    def enter_field(self, node: FieldNode, *_args: Any) -> None:
        parent = self.type_info.get_parent_type()
        if node.name.value.startswith("__") or parent is None:
            return
        if self.type_info.get_field_def() is None:
            return  # not in the schema: left out, so the contract test reports it
        self.reach.add_field(parent, node.name.value)

    def enter_variable_definition(self, node: VariableDefinitionNode, *_args: Any) -> None:
        type_ = type_from_ast(self.reach.schema, node.type)
        if type_ is not None:
            self.reach.add_type(type_)

    def enter_inline_fragment(self, node: InlineFragmentNode, *_args: Any) -> None:
        self._type_condition()

    def enter_fragment_definition(self, node: FragmentDefinitionNode, *_args: Any) -> None:
        self._type_condition()

    def _type_condition(self) -> None:
        type_ = self.type_info.get_type()
        if type_ is not None:
            self.reach.add_type(type_)


def _placeholder_field(type_: GraphQLObjectType | GraphQLInterfaceType) -> str:
    """A field for a type reached only as a container (no field selected on it).

    SDL forbids an object or interface with no fields. ``id`` if it exists,
    else the first scalar/enum field by name, so the fill never pulls in a new
    type that would itself need filling."""
    if "id" in type_.fields:
        return "id"
    for name in sorted(type_.fields):
        if isinstance(
            get_named_type(type_.fields[name].type), (GraphQLScalarType, GraphQLEnumType)
        ):
            return name
    return sorted(type_.fields)[0]


def _close(reach: _Reach) -> None:
    """Grow the reach until it is a valid schema on its own.

    Repeats until nothing changes: an implementing type carries every field
    selected on an interface it implements; a union keeps at least one member;
    an object or interface keeps at least one field."""
    schema = reach.schema
    changed = True
    while changed:
        changed = False
        for name in sorted(reach.types):
            type_ = schema.get_type(name)
            if isinstance(type_, GraphQLUnionType):
                if not any(member.name in reach.types for member in type_.types):
                    reach.add_type(sorted(type_.types, key=lambda t: t.name)[0])
                    changed = True
            elif isinstance(type_, (GraphQLObjectType, GraphQLInterfaceType)):
                if not reach.fields.get(name):
                    changed |= reach.add_field(type_, _placeholder_field(type_))
                for interface in type_.interfaces:
                    if interface.name not in reach.types:
                        continue
                    for field in sorted(reach.fields.get(interface.name, ())):
                        changed |= reach.add_field(type_, field)


def _print_args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    return "(" + ", ".join(print_input_value(name, arg) for name, arg in args.items()) + ")"


def _print_type(reach: _Reach, type_: GraphQLNamedType) -> str:
    if isinstance(type_, GraphQLScalarType):
        return f"scalar {type_.name}"
    if isinstance(type_, GraphQLEnumType):
        values = [
            f"  {name}{print_deprecated(type_.values[name].deprecation_reason)}"
            for name in sorted(type_.values)
        ]
        return f"enum {type_.name}" + print_block(values)
    if isinstance(type_, GraphQLInputObjectType):
        fields = [
            f"  {print_input_value(name, type_.fields[name])}" for name in sorted(type_.fields)
        ]
        return f"input {type_.name}" + print_block(fields)
    if isinstance(type_, GraphQLUnionType):
        members = sorted(m.name for m in type_.types if m.name in reach.types)
        return f"union {type_.name} = " + " | ".join(members)
    assert isinstance(type_, (GraphQLObjectType, GraphQLInterfaceType))
    keyword = "type" if isinstance(type_, GraphQLObjectType) else "interface"
    interfaces = sorted(i.name for i in type_.interfaces if i.name in reach.types)
    implements = f" implements {' & '.join(interfaces)}" if interfaces else ""
    fields = []
    for name in sorted(reach.fields[type_.name]):
        field = type_.fields[name]
        fields.append(
            f"  {name}{_print_args(field.args)}: {field.type}"
            f"{print_deprecated(field.deprecation_reason)}"
        )
    return f"{keyword} {type_.name}{implements}" + print_block(fields)


def generate_sdl(schema: GraphQLSchema, documents: Iterable[str]) -> str:
    """Trim ``schema`` to what ``documents`` reach and print it as SDL.

    Kept: every selected (type, field) with its FULL argument signature
    (deprecated arguments included); every input object reached, with all its
    fields; enums in full; only the union members and interfaces that are
    themselves reached. Everything is sorted, so regenerating from an unchanged
    schema is a byte-identical no-op. The header is not part of this output."""
    reach = _Reach(schema)
    for root in (schema.query_type, schema.mutation_type):
        if root is not None:
            reach.add_type(root)
    for text in documents:
        type_info = TypeInfo(schema)
        visit(parse(text), TypeInfoVisitor(type_info, _Collector(reach, type_info)))
    _close(reach)

    blocks = []
    roots = [("query", schema.query_type), ("mutation", schema.mutation_type)]
    blocks.append(
        "schema" + print_block([f"  {op}: {root.name}" for op, root in roots if root is not None])
    )
    for name in sorted(reach.types):
        if name in specified_scalar_types:
            continue
        type_ = schema.get_type(name)
        assert type_ is not None
        blocks.append(_print_type(reach, type_))
    return "\n\n".join(blocks) + "\n"
