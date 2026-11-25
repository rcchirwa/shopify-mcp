"""Find every client.paginate() call in src/ and check its query (Story 10.120).

`discover_paginate_calls` reads the source with `ast`, so the table of call
sites is derived, never written by hand. It looks for the SUBJECT, not one
spelling of it: every attribute named `paginate` and every string constant
"paginate", under any receiver name. A reference it cannot turn into a row is
returned as a problem that names its file and line, so a call site is never
skipped in silence.

`check_paginate_query` walks the query the way client.paginate() reads the
response. It uses walk_field and field_names from graphql_shape, and
expand_walked_spreads for a connection that lives in a named fragment.
"""

import ast
import importlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import graphql

import shopify_mcp
from tests.support.graphql_shape import expand_walked_spreads, field_names, walk_field

SRC_ROOT = Path(shopify_mcp.__file__).parent


@dataclass(frozen=True)
class CallSite:
    """One client.paginate() call: where it is, and what it passes."""

    where: str
    query_name: str
    query_text: str
    connection_path: list[str]

    @property
    def label(self) -> str:
        return f"{self.query_name} ({self.where})"


def discover_paginate_calls(root: Path, package: str) -> tuple[list[CallSite], list[str]]:
    """Return a row for each paginate call under `root`, and a problem for each other reference.

    `root` is the directory of `package`. A row needs a call whose first
    argument is a module-level name bound to a string, and whose
    `connection_path` keyword is a literal list of strings.
    """
    sites: list[CallSite] = []
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        parts = path.relative_to(root).with_suffix("").parts
        module_name = (
            ".".join((package, *parts[:-1]))
            if parts[-1] == "__init__"
            else ".".join((package, *parts))
        )
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        shadowed = _locally_bound_names(tree)
        relative = path.relative_to(root.parent)
        for node in ast.walk(tree):
            where = f"{relative}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Constant) and node.value == "paginate":
                problems.append(f"{where}: string 'paginate' (a dynamic lookup has no row)")
            elif isinstance(node, ast.Attribute) and node.attr == "paginate":
                if id(node) not in called:
                    problems.append(f"{where}: .paginate is referenced but not called here")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "paginate"
            ):
                site = _site(node, where, module_name, shadowed.get(id(node), set()))
                if isinstance(site, CallSite):
                    sites.append(site)
                else:
                    problems.append(f"{where}: {site}")
    return sites, problems


def _locally_bound_names(tree: ast.Module) -> dict[int, set[str]]:
    """Map each call inside a function to the names that function binds locally.

    A parameter or a local assignment with the same name as a module constant
    hides that constant. Reading the module attribute would then check the wrong
    text, so such a name gets no row.
    """
    bound: dict[int, set[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        args = fn.args
        names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        names |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        names |= {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                bound.setdefault(id(node), set()).update(names)
    return bound


def _site(call: ast.Call, where: str, module_name: str, local_names: set[str]) -> CallSite | str:
    """Turn one paginate call into a row, or say why it cannot be one."""
    if not call.args or not isinstance(call.args[0], ast.Name):
        return "the query is not a module-level name"
    name = call.args[0].id
    if name in local_names:
        return f"{name} is bound inside the function, so it is not the module-level constant"
    text = getattr(importlib.import_module(module_name), name, None)
    if not isinstance(text, str):
        return f"{name} is not a string in {module_name}"
    keyword = next((k for k in call.keywords if k.arg == "connection_path"), None)
    try:
        path = ast.literal_eval(keyword.value) if keyword is not None else None
    except ValueError:
        path = None
    if not (isinstance(path, list) and path and all(isinstance(p, str) for p in path)):
        return "connection_path is not a literal list of strings"
    return CallSite(where, name, text, path)


def check_paginate_query(text: str, connection_path: list[str], where: str) -> None:
    """Fail unless `text` gives paginate() what it reads at `connection_path`.

    Checked: every step of the path; `nodes` and `pageInfo { hasNextPage
    endCursor }` on the connection, each once and unaliased; `first: $first`
    and `after: $after` on the connection, each once; `$first` and `$after`
    each declared once on the operation. The declared types are not checked:
    some queries declare `$first: Int!` and some `$first: Int = 50`.
    """
    root = expand_walked_spreads(text, [*connection_path, "pageInfo"], where)
    connection = walk_field(root, connection_path, where)
    assert connection.selection_set is not None, f"{where}: the connection has no selection set"
    names = field_names(connection.selection_set, where)
    for key in ("nodes", "pageInfo"):
        assert key in names, f"{where}: no field {key!r} on {'.'.join(connection_path)}"
    page_info = walk_field(connection.selection_set, ["pageInfo"], where)
    assert page_info.selection_set is not None, f"{where}: pageInfo has no selection set"
    leaves = field_names(page_info.selection_set, f"{where} pageInfo")
    for key in ("hasNextPage", "endCursor"):
        assert key in leaves, f"{where}: pageInfo does not select {key}"
    for arg in ("first", "after"):
        bound = [a for a in connection.arguments or () if a.name.value == arg]
        assert len(bound) == 1 and isinstance(bound[0].value, graphql.VariableNode), (
            f"{where}: the connection must pass {arg}: ${arg} once"
        )
        assert bound[0].value.name.value == arg, (
            f"{where}: the connection must pass {arg}: ${arg}, not "
            f"{arg}: ${bound[0].value.name.value}"
        )
    (operation,) = [
        d for d in graphql.parse(text).definitions if isinstance(d, graphql.OperationDefinitionNode)
    ]
    declared = Counter(v.variable.name.value for v in operation.variable_definitions or ())
    for var in ("first", "after"):
        assert declared[var] == 1, f"{where}: ${var} is declared {declared[var]} times"
