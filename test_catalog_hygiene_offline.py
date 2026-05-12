"""
Offline unit tests for tools/catalog_hygiene.py.

Wave 0 ships the module as an empty `register(server, client)` skeleton —
Stories 9.1-9.7 plug their @server.tool() functions in. These tests pin the
skeleton's contract (callable, registers zero tools) so the coverage gate
stays at 100% and a future story doesn't accidentally break the entry-point
signature.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_catalog_hygiene_offline.py -v
"""

from _testing import CapturingServer, FakeClient
from tools import catalog_hygiene


def test_register_is_callable_and_returns_none():
    srv = CapturingServer()
    fc = FakeClient([])
    assert catalog_hygiene.register(srv, fc) is None


def test_register_adds_no_tools_in_wave_0():
    # Wave 0 ships scaffolding only — tool count must stay at 0 in this module
    # until Stories 9.1-9.7 plug their @server.tool() functions in.
    srv = CapturingServer()
    fc = FakeClient([])
    catalog_hygiene.register(srv, fc)
    assert srv.tools == {}
    assert fc.calls == []
