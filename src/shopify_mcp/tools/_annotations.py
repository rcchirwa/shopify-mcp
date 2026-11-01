"""MCP tool annotations: the four hint sets every registered tool declares (Story 10.73).

Each ``@server.tool(...)`` passes one of these as ``annotations=``. The table
that gives every tool's hint set and the reason for it is
``docs/tool-annotations.md``; ``tests/architecture/test_tool_annotations.py``
holds that table, README and the served ``tools/list`` hints equal.

Annotations are advisory hints. The MCP spec says clients must not rely on
them for security decisions, so they are not a control and close no SEC item.
The ``confirm`` gate inside each write tool is still the control.

Rules:

1. A ``confirm``-gated tool is ``readOnlyHint=False``. Without ``confirm=True``
   it only previews, but an annotation is static, so it describes the
   confirmed path — the worst case.
2. ``destructiveHint=True`` when the confirmed path can overwrite or remove
   data already in the store; ``False`` only when it can only add. Judged from
   the mutation the tool sends, not from its name.
3. ``idempotentHint=True`` when a repeat call with the same arguments leaves
   the store in the same state. ``False`` for a tool that creates a new
   resource: a repeat makes a second one, or is stopped only by a uniqueness
   refusal, and the hint does not promise a retry that rests on that refusal.
4. ``openWorldHint=True`` for every tool: each one calls the Shopify Admin API.
5. Read tools also state ``destructiveHint=False`` and ``idempotentHint=True``,
   so no client falls back to the spec defaults (destructive, not idempotent).
"""

from mcp.types import ToolAnnotations

# Reads the store and changes nothing.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

# Creates a new resource; changes no existing data; a repeat is not a no-op.
CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)

# Adds to existing data and removes nothing; a repeat converges.
ADD = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

# Can replace or remove existing data; a repeat converges.
OVERWRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)
