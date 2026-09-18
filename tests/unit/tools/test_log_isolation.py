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

# Deliberately NOT one of test_media.py's 111/222/333 ids. The live audit trail
# already carries historical fixture lines from those (200 occurrences of
# MediaImage/333 in the main checkout as of 2026-09-18), so keying the
# "did not reach the live log" assertion on a shared id would fail on the old
# pollution this story does not clean up, instead of on a redirect that leaked.
# This id appears nowhere else, so finding it in the live log means this run put
# it there. `product=123` is shared and cannot serve as the fingerprint.
GUARD_MEDIA = "gid://shopify/MediaImage/109300"


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

    tools = _media_tools(
        [
            {
                "product": {
                    "id": PRODUCT_GID,
                    "title": "Hoodie",
                    "media": {
                        "nodes": [
                            {
                                "id": GUARD_MEDIA,
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
                    "deletedMediaIds": [GUARD_MEDIA],
                    "product": {"id": PRODUCT_GID},
                    "mediaUserErrors": [],
                }
            },
        ]
    )

    out = tools["delete_product_media"](product_id="123", media_ids=[GUARD_MEDIA], confirm=True)
    assert out.startswith("CONFIRMED —"), out

    redirected = Path(_log.LOG_FILE)
    assert redirected.exists(), (
        f"no audit line was written anywhere ({redirected} absent) — the tool "
        "stopped logging, so this test would otherwise pass vacuously"
    )
    line = redirected.read_text(encoding="utf-8")
    assert "delete_product_media" in line
    assert GUARD_MEDIA in line, f"the captured line does not name this test's media id: {line!r}"

    # Assert on content, not on size. The live shopify-gss server appends to
    # this same file, so comparing before/after bytes would fail whenever a
    # real store write happened to land mid-run — and would blame this test for
    # it. What actually matters is that THIS test's line is not in there.
    if live.exists():
        assert GUARD_MEDIA not in live.read_text(encoding="utf-8"), (
            f"this test's fixture media id {GUARD_MEDIA} reached the live audit "
            f"trail {live}; the redirect is not holding"
        )
