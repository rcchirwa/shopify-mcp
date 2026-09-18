"""
Guard: the offline suite must never append to the repo's real audit trail.

Story 10.93. `tools/_log.py` resolves LOG_FILE to the repo root of whatever
checkout runs the tests — in the main checkout, the same `aon_mcp_log.txt` the
live `shopify-gss` server appends real Shopify writes to. Eleven tool modules
bind `log_write` by name (`from shopify_mcp.tools._log import log_write`), so
patching one module's binding never covers another's: before this story, a full
offline run left 63 fixture lines in that file, from 37 tests in
test_publications.py, 21 in test_media.py and 5 in test_products.py.

tests/conftest.py therefore redirects `_log.LOG_FILE` itself — the single place
the path is decided, which every call site reaches through, including modules
added later. These two tests pin that redirect and fail if it is removed.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_log_isolation.py -v
"""

from pathlib import Path

from shopify_mcp.tools import _log, media
from tests.support import CapturingServer, FakeClient

PRODUCT_GID = "gid://shopify/Product/123"
MEDIA_A = "gid://shopify/MediaImage/111"


def _live_log() -> Path:
    """The audit file production writes to, located independently of LOG_FILE.

    Derived from the module's own path — the same `parents[3]` walk `_log.py`
    uses — because LOG_FILE is redirected while these tests run and so cannot
    be asked where production would have pointed.
    """
    return Path(_log.__file__).resolve().parents[3] / "aon_mcp_log.txt"


def _media_tools(responses):
    srv = CapturingServer()
    media.register(srv, FakeClient(responses))
    return srv.tools


def test_audit_log_is_redirected_outside_the_repo_during_tests():
    """The autouse redirect is active, so no test can reach the real trail."""
    redirected = Path(_log.LOG_FILE).resolve()
    repo_root = _live_log().parent

    assert redirected != _live_log().resolve(), (
        "_log.LOG_FILE still points at the live audit trail; the conftest "
        "redirect is missing or was overridden"
    )
    assert repo_root not in redirected.parents, (
        f"_log.LOG_FILE ({redirected}) is still inside the repo at {repo_root}; "
        "a redirect that lands in the checkout can still be committed by accident"
    )


def test_a_real_tool_write_is_captured_by_the_redirect_not_the_live_log():
    """An unpatched call site logs for real — into the redirect, not the trail.

    `delete_product_media` calls `log_write` through `tools/media/_delete.py`'s
    own binding, which no per-suite fixture patches. Asserting the line landed
    in the redirect is what keeps this test honest: without it, the test would
    pass just as happily if the tool had stopped logging altogether.
    """
    live = _live_log()
    size_before = live.stat().st_size if live.exists() else None

    tools = _media_tools(
        [
            {
                "product": {
                    "id": PRODUCT_GID,
                    "title": "Hoodie",
                    "media": {
                        "nodes": [
                            {
                                "id": MEDIA_A,
                                "alt": "",
                                "mediaContentType": "IMAGE",
                                "status": "READY",
                                "preview": {"image": {"url": "https://cdn.shopify.com/a.jpg"}},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            },
            {
                "productDeleteMedia": {
                    "deletedMediaIds": [MEDIA_A],
                    "product": {"id": PRODUCT_GID},
                    "mediaUserErrors": [],
                }
            },
        ]
    )

    out = tools["delete_product_media"](product_id="123", media_ids=[MEDIA_A], confirm=True)
    assert out.startswith("CONFIRMED —"), out

    redirected = Path(_log.LOG_FILE)
    assert redirected.exists(), (
        f"no audit line was written anywhere ({redirected} absent) — the tool "
        "stopped logging, so this test would otherwise pass vacuously"
    )
    assert "delete_product_media" in redirected.read_text(encoding="utf-8")

    size_after = live.stat().st_size if live.exists() else None
    assert size_after == size_before, (
        f"the confirmed write reached the live audit trail {live}: "
        f"{size_before} -> {size_after} bytes"
    )
