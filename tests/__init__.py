"""Test suite for shopify-mcp, mirroring the source layout under ``tests/unit``.

Every directory in this tree carries an ``__init__.py``, and that is load-bearing
rather than stylistic. Mirroring the source layout produces nine duplicate test
basenames (``unit/tools/test_products.py`` vs
``unit/shopify/operations/test_products.py``, and so on for the other seven
migrated domains, plus ``unit/tools/test_webhooks.py`` vs
``live/test_webhooks.py``). Under pytest's default *prepend* import mode, two
same-named modules in non-package directories collide on ``sys.modules`` and
pytest aborts the run with an "import file mismatch" collection error. Making
each directory a package qualifies the module names (``tests.unit.tools.
test_products``) and the collision disappears.

``tests/architecture/test_layout.py`` guards this invariant so a new directory
added without an ``__init__.py`` fails as an assertion instead of as a
collection error.
"""
