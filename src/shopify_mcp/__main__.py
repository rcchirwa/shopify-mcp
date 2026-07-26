"""``python -m shopify_mcp`` — the module-form entry point.

Replaces the pre-move ``python shopify_mcp.py``, which stopped being runnable
when the server moved under ``src/`` (Story 10.47 / FS-3). Equivalent to the
``shopify-mcp`` console script; both call ``server.main()``.

Excluded from coverage alongside ``server.py`` ([tool.coverage.run] omit): the
entry points are verified by launching them, not by unit tests. That carve-out
is pinned to exactly these two modules by
``tests/architecture/test_src_layout.py::test_coverage_omits_only_the_entry_points``.
"""

from shopify_mcp.server import main

if __name__ == "__main__":
    main()
