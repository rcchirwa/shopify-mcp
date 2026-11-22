"""
No Shopify call in ``tools/`` may let its exception leave the tool (Story 10.118).

A tool that lets a Shopify exception escape shows the model
``Error executing tool <name>: <str(exc)>`` with ``isError=true``. The project's
convention is a text reply. This test walks every ``.py`` file under
``src/shopify_mcp/tools/`` and lists each Shopify call that nothing guards.

A "Shopify call" is any of:
  * ``ops.<name>(...)``;
  * ``<name ending in client>.execute / .paginate / .fetch_bytes(...)``;
  * ANY call that passes a name ending in ``client`` as an argument. This catches
    ``op["op"](client, ...)`` and helper calls such as ``_resolve_collection(client, ...)``.

A call is guarded when one of these holds:
  1. It sits in the body of a ``try`` that has a handler for ``Exception`` (or
     broader), inside the same function, and that handler does not raise.
  2. Its function, or an outer one, has ``@read_boundary(...)``. A separate
     test checks that the decorator sits under ``@server.tool``.
  3. It sits in a lambda passed as ``execute=`` to ``write_gate(...)``, or in a
     function of that name defined in the function that calls ``write_gate``.
     ``write_gate`` guards ``execute()``: ``test_write_tool.py`` and
     ``test_tool_error_replies.py`` check that guard, this test does not.
  4. The callee is in ``CALLEE_EXEMPT``.
  5. The enclosing function is in ``HELPER_EXEMPT``. A call by name to such a
     helper is itself checked, so the exemption's claim is tested.

Each exemption must match a real site, so a stale entry fails the test.
"""

import ast
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[3] / "src" / "shopify_mcp" / "tools"

# Callee name -> why a call to it cannot raise out of the tool. Read the code
# before you add one.
CALLEE_EXEMPT: dict[str, str] = {
    "poll_job": "never raises: it catches every error itself and returns it (client.py)",
    "register": "tool registration at import time; it makes no Shopify call",
    "_check_endpoint": "reads client._settings only (the webhook allowlist); no network call",
    "_resolve_product_gid": (
        "catalog_hygiene: returns (gid, error). It catches ValueError and Exception itself"
    ),
    "_resolve_taxonomy_category": (
        "catalog_hygiene: returns (category, alternates, error). Its one Shopify call is in "
        "try/except Exception"
    ),
    "_resolve_owner_gid_for_metafield": (
        "catalog_hygiene: returns (gid, type, error). Its only Shopify call is "
        "_resolve_product_gid, which catches every error"
    ),
    "_handle_append_failure_after_detach": (
        "catalog_hygiene: builds the reply itself. Its one Shopify call is in try/except Exception"
    ),
    "_stage_upload": "media/_upload: returns (target, error). Its call is in try/except Exception",
    "_attach_media": "media/_upload: returns (media, error). Its call is in try/except Exception",
    "_poll_media_ready": (
        "media/_upload: returns a status dict. Its call is in try/except Exception"
    ),
    "_maybe_reorder_new_media": (
        "media/_upload: returns a note. Its call is in try/except Exception"
    ),
    "_channel_write": (
        "publications: its one Shopify call (the mutation) is in try/except Exception and "
        "returns the error reply"
    ),
    "_invalidate_channels": "drops an in-memory cache entry; no network call",
}

# (path relative to tools/, function) -> why every caller guards the call. The
# caller's own call passes ``client``, so it is a Shopify call and is checked
# on its own.
HELPER_EXEMPT: dict[tuple[str, str], str] = {
    ("_product_resolver.py", "_lookup_by_handle"): (
        "its docstring says exceptions propagate; every caller passes client, so each call is "
        "checked on its own"
    ),
    ("_product_resolver.py", "_resolve_product"): (
        "its docstring says exceptions propagate; every caller passes client, so each call is "
        "checked on its own"
    ),
    ("collections.py", "_resolve_collection"): (
        "get_collection is under read_boundary; the three write tools guard each call"
    ),
    ("media/_upload.py", "_download_image"): (
        "the caller wraps it in try/except Exception and labels it stage=download"
    ),
    ("publications.py", "_read_channels"): "every caller is in try/except Exception (checked)",
    ("publications.py", "_load_channels"): "every caller is in try/except Exception (checked)",
    ("publications.py", "_ensure_channels"): "every caller is in try/except Exception (checked)",
    ("publications.py", "_resolve_names"): "every caller is in try/except Exception (checked)",
    ("publications.py", "_resolve_ids"): "every caller is in try/except Exception (checked)",
    ("publications.py", "_resolve_target_nodes"): (
        "a closure over client; its three callers are in try/except Exception"
    ),
    ("publications.py", "_resolve_product_gid_and_meta"): (
        "every caller is in try/except Exception (checked)"
    ),
    ("publications.py", "_resolve_collection_gid_and_meta"): (
        "every caller is in try/except Exception (checked)"
    ),
}

_CLIENT_METHODS = {"execute", "paginate", "fetch_bytes"}
_BROAD = {"Exception", "BaseException"}


def _is_client_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id.endswith("client")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("client")
    return False


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ast.unparse(func)


def _is_shopify_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "ops":
            return True
        if func.attr in _CLIENT_METHODS and _is_client_name(func.value):
            return True
    args = [*call.args, *(kw.value for kw in call.keywords)]
    return any(_is_client_name(a) for a in args)


def _catches_everything(handler: ast.ExceptHandler) -> bool:
    """A handler for ``Exception`` (or broader) that does not raise again."""
    if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
        return False
    kind = handler.type
    if kind is None:
        return True
    names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    return any(isinstance(n, ast.Name) and n.id in _BROAD for n in names)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _has_read_boundary(fn: ast.AST) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(
        isinstance(d, ast.Call) and _callee_name(d) == "read_boundary" for d in fn.decorator_list
    )


def _execute_bodies(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> set[ast.AST]:
    """Functions and lambdas passed as ``execute=`` to ``write_gate(...)``.

    A name is matched only to a function defined directly in the body of the
    function that calls ``write_gate``, so an ``_execute`` in another tool of
    the same file does not count.
    """
    found: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _callee_name(node) == "write_gate"):
            continue
        chain = _enclosing_functions(node, parents)
        scope = chain[0] if chain else tree
        for kw in node.keywords:
            if kw.arg != "execute":
                continue
            if isinstance(kw.value, ast.Lambda):
                found.add(kw.value)
            elif isinstance(kw.value, ast.Name):
                found.update(
                    fn
                    for fn in getattr(scope, "body", [])
                    if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
                    and fn.name == kw.value.id
                )
    return found


def _inside_guarding_try(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    child, parent = node, parents.get(node)
    while parent is not None and not isinstance(parent, _FUNCTIONS):
        if (
            isinstance(parent, ast.Try)
            and child in parent.body
            and any(_catches_everything(h) for h in parent.handlers)
        ):
            return True
        child, parent = parent, parents.get(parent)
    return False


def _enclosing_functions(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    chain: list[ast.AST] = []
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, _FUNCTIONS):
            chain.append(parent)
        parent = parents.get(parent)
    return chain


def _scan() -> tuple[list[str], set[str], set[tuple[str, str]]]:
    """Return (unguarded sites, callee exemptions used, helper exemptions used).

    A call by name to a helper in ``HELPER_EXEMPT`` for the same file is also a
    Shopify call. So the exemption's claim ("every caller guards it") is
    checked, also for a closure such as ``_resolve_target_nodes`` that is
    called with no ``client`` argument.
    """
    unguarded: list[str] = []
    callees_used: set[str] = set()
    helpers_used: set[tuple[str, str]] = set()
    for path in sorted(_TOOLS.rglob("*.py")):
        rel = path.relative_to(_TOOLS).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _parents(tree)
        execute_bodies = _execute_bodies(tree, parents)
        exempt_here = {name for (file, name) in HELPER_EXEMPT if file == rel}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            calls_exempt_helper = isinstance(node.func, ast.Name) and node.func.id in exempt_here
            if not (_is_shopify_call(node) or calls_exempt_helper):
                continue
            chain = _enclosing_functions(node, parents)
            if _inside_guarding_try(node, parents):
                continue
            if any(_has_read_boundary(fn) for fn in chain):
                continue
            if any(fn in execute_bodies for fn in chain):
                continue
            callee = _callee_name(node)
            helper_names = {
                (rel, fn.name)
                for fn in chain
                if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            exempt_helpers = helper_names & HELPER_EXEMPT.keys()
            exempt_callee = callee in CALLEE_EXEMPT
            if exempt_callee:
                callees_used.add(callee)
            helpers_used |= exempt_helpers
            if exempt_callee or exempt_helpers:
                continue
            unguarded.append(f"{rel}:{node.lineno}: {ast.unparse(node.func)}(...)")
    return unguarded, callees_used, helpers_used


def test_every_shopify_call_in_tools_is_guarded() -> None:
    unguarded, _callees, _helpers = _scan()
    assert unguarded == [], (
        "These Shopify calls can let an exception leave the tool. Guard each one with "
        "try/except, read_boundary or write_gate:\n" + "\n".join(unguarded)
    )


def test_no_exemption_is_stale() -> None:
    _unguarded, callees_used, helpers_used = _scan()
    assert set(CALLEE_EXEMPT) - callees_used == set(), "stale CALLEE_EXEMPT entry"
    assert set(HELPER_EXEMPT) - helpers_used == set(), "stale HELPER_EXEMPT entry"


def test_every_exemption_gives_a_reason() -> None:
    assert all(reason.strip() for reason in CALLEE_EXEMPT.values())
    assert all(reason.strip() for reason in HELPER_EXEMPT.values())


def test_read_boundary_sits_only_on_read_only_tools() -> None:
    """A write tool must never use ``read_boundary``: it says "reading", and it
    would hide that a write may have landed."""
    offenders: list[str] = []
    for path in sorted(_TOOLS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _has_read_boundary(node):
                continue
            assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            tool_decorators = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call) and _callee_name(d) == "tool"
            ]
            read_only = any(
                kw.arg == "annotations" and ast.unparse(kw.value) == "READ_ONLY"
                for d in tool_decorators
                for kw in d.keywords
            )
            if not read_only:
                offenders.append(f"{path.name}:{node.lineno}: {node.name}")
            # Decorators apply bottom-up. read_boundary must sit UNDER the tool
            # decorator, or FastMCP registers the function without the boundary.
            names = [_callee_name(d) for d in node.decorator_list if isinstance(d, ast.Call)]
            if "tool" not in names or names.index("read_boundary") < names.index("tool"):
                offenders.append(f"{path.name}:{node.lineno}: {node.name} (decorator order)")
    assert offenders == []


def test_the_scanner_finds_each_kind_of_site() -> None:
    """The scanner must flag an unguarded call and pass a guarded one."""
    source = (
        "def tool(client):\n"
        "    ops.read(client)\n"
        "    client.execute('q')\n"
        "    helper(client)\n"
        "    try:\n"
        "        ops.ok(client)\n"
        "    except ValueError:\n"
        "        pass\n"
        "    try:\n"
        "        ops.guarded(client)\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        ops.reraised(client)\n"
        "    except Exception:\n"
        "        raise\n"
    )
    tree = ast.parse(source)
    parents = _parents(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_shopify_call(n)]
    unguarded = sorted(ast.unparse(c.func) for c in calls if not _inside_guarding_try(c, parents))
    assert unguarded == ["client.execute", "helper", "ops.ok", "ops.read", "ops.reraised"]


def test_execute_name_matches_only_in_the_calling_function() -> None:
    """An ``_execute`` in another tool of the same file is not trusted."""
    source = (
        "def tool_a(client):\n"
        "    def _execute():\n"
        "        return ops.write(client)\n"
        "    return write_gate(execute=_execute)\n"
        "def tool_b(client):\n"
        "    def _execute():\n"
        "        return ops.other(client)\n"
        "    return _execute()\n"
    )
    tree = ast.parse(source)
    bodies = _execute_bodies(tree, _parents(tree))
    assert [fn.lineno for fn in bodies] == [2]
