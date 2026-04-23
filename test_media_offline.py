"""
Offline unit tests for tools/media.py.

Uses a fake client to exercise read-path response unwrap, write-path
preview/confirm branches, and error-surfacing at each upload stage without
hitting Shopify. HTTP helpers (requests.get / requests.put) and time.sleep
are stubbed so the tests are deterministic and fast.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_media_offline.py -v
"""

import os
import socket
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from tools import media
from tools.media import _reject_if_private_host
from tools.media import (
    GET_PRODUCT_MEDIA,
    STAGED_UPLOADS_CREATE,
    PRODUCT_CREATE_MEDIA,
    PRODUCT_REORDER_MEDIA,
    PRODUCT_UPDATE_MEDIA,
    PRODUCT_DELETE_MEDIA,
)


PRODUCT_GID = "gid://shopify/Product/123"
MEDIA_A = "gid://shopify/MediaImage/111"
MEDIA_B = "gid://shopify/MediaImage/222"
MEDIA_C = "gid://shopify/MediaImage/333"


class CapturingServer:
    """Stand-in for FastMCP that records decorated tool functions."""
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    """Scripted responses for client.execute(). Supports an optional
    per-call exception hook so tests can simulate network failures."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables=None):
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("FakeClient: unexpected extra execute() call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeHTTPResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}
        self.text = text

    def iter_content(self, chunk_size=65536):
        # Yield in two chunks to exercise the accumulator branch.
        if not self._content:
            return iter([])
        mid = max(1, len(self._content) // 2)
        return iter([self._content[:mid], self._content[mid:]])


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    media.register(srv, fc)
    return srv.tools, fc


def _media_node(mid, alt="", status="READY", kind="IMAGE", preview_url=None):
    return {
        "id": mid,
        "alt": alt,
        "mediaContentType": kind,
        "status": status,
        "preview": {"image": {"url": preview_url or f"https://cdn.shopify.com/{mid}.jpg"}},
    }


def _product_media_read(nodes, pid="123", title="Hoodie", has_next=False):
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "media": {"nodes": nodes, "pageInfo": {"hasNextPage": has_next}},
    }}


# ---------- list_product_media ----------

def test_list_product_media_formats_output():
    tools, fc = _build([_product_media_read([
        _media_node(MEDIA_A, alt="front"),
        _media_node(MEDIA_B, alt=""),
    ])])
    out = tools["list_product_media"](product_id="123")
    assert "Media for product 123" in out
    assert "2 item(s)" in out
    assert MEDIA_A in out and "'front'" in out
    assert MEDIA_B in out
    assert "IMAGE" in out and "status=READY" in out
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA
    assert fc.calls[0][1] == {"id": PRODUCT_GID}


def test_list_product_media_empty():
    tools, fc = _build([_product_media_read([])])
    out = tools["list_product_media"](product_id="123")
    assert "0 item(s)" in out
    assert "(no media)" in out


def test_list_product_media_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["list_product_media"](product_id="999")
    assert out == "No product found with id 999."


def test_list_product_media_warns_at_page_cap():
    """hasNextPage=True surfaces a truncation warning."""
    tools, fc = _build([_product_media_read(
        [_media_node(MEDIA_A)], has_next=True,
    )])
    out = tools["list_product_media"](product_id="123")
    assert "WARNING" in out and "100" in out


def test_list_product_media_accepts_full_gid_passthrough():
    """Callers may pass a full GID instead of a numeric id."""
    tools, fc = _build([_product_media_read([])])
    tools["list_product_media"](product_id=PRODUCT_GID)
    assert fc.calls[0][1] == {"id": PRODUCT_GID}


# ---------- upload_product_image — input validation ----------

def test_upload_rejects_non_https_source():
    tools, fc = _build([])
    out = tools["upload_product_image"](
        product_id="123", source="http://example.com/a.jpg", confirm=True,
    )
    assert out.startswith("Error at stage=input:"), out
    assert fc.calls == []


def test_upload_rejects_local_file_path():
    tools, fc = _build([])
    out = tools["upload_product_image"](
        product_id="123", source="/tmp/hero.jpg", confirm=True,
    )
    assert out.startswith("Error at stage=input:"), out
    assert fc.calls == []


def test_upload_rejects_missing_source():
    tools, fc = _build([])
    out = tools["upload_product_image"](
        product_id="123", source="", confirm=True,
    )
    assert out.startswith("Error at stage=input:"), out
    assert fc.calls == []


def test_upload_rejects_negative_position():
    tools, fc = _build([_product_media_read([])])
    out = tools["upload_product_image"](
        product_id="123",
        source="https://cdn.example.com/a.jpg",
        position=-1,
        confirm=False,
    )
    assert out.startswith("Error at stage=input:"), out


# ---------- upload_product_image — preview ----------

def test_upload_preview_mode_only_reads():
    tools, fc = _build([_product_media_read([
        _media_node(MEDIA_A), _media_node(MEDIA_B),
    ])])
    out = tools["upload_product_image"](
        product_id="123",
        source="https://cdn.example.com/hero.jpg",
        alt="new hero",
        position=1,
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert "confirm=True" in out
    assert "position 1 (featured)" in out
    # Only the read should have happened in preview mode.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA


def test_upload_preview_shows_append_when_no_position():
    tools, fc = _build([_product_media_read([_media_node(MEDIA_A)])])
    out = tools["upload_product_image"](
        product_id="123",
        source="https://cdn.example.com/a.jpg",
        confirm=False,
    )
    assert "append to end" in out


# ---------- upload_product_image — execute happy path ----------

def _staged_ok(url="https://staged.shopify-gcs.example/signed",
               resource_url="https://shopify-cdn.example/resource"):
    return {"stagedUploadsCreate": {
        "stagedTargets": [{
            "url": url,
            "resourceUrl": resource_url,
            "parameters": [
                {"name": "content_type", "value": "image/jpeg"},
                {"name": "acl", "value": "private"},
            ],
        }],
        "userErrors": [],
    }}


def _create_media_ok(mid=MEDIA_C, alt="", status="PROCESSING", preview_url=None):
    return {"productCreateMedia": {
        "media": [_media_node(mid, alt=alt, status=status, preview_url=preview_url)],
        "mediaUserErrors": [],
    }}


def _create_media_err(field, message):
    return {"productCreateMedia": {
        "media": [],
        "mediaUserErrors": [{"field": field, "message": message}],
    }}


def _reorder_ok(done=True, job_id="gid://shopify/Job/j1"):
    return {"productReorderMedia": {
        "job": {"id": job_id, "done": done},
        "mediaUserErrors": [],
        "userErrors": [],
    }}


def _make_http_response(status=200, content=b"fakejpgbytes", content_type="image/jpeg"):
    return FakeHTTPResponse(
        status_code=status,
        content=content,
        headers={"Content-Type": content_type, "Content-Length": str(len(content))},
    )


def test_upload_execute_happy_path_append():
    """End-to-end: download → stage → attach → poll (READY on first try), no reorder."""
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),  # initial read
        _staged_ok(),                                                       # stagedUploadsCreate
        _create_media_ok(mid=MEDIA_C, status="PROCESSING"),                 # productCreateMedia
        _product_media_read([                                               # poll (READY)
            _media_node(MEDIA_A), _media_node(MEDIA_B),
            _media_node(MEDIA_C, status="READY", preview_url="https://cdn.shopify.com/new.jpg"),
        ]),
    ])

    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)), \
         patch("tools.media.time.sleep"):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/hero.jpg",
            alt="Smoke hero",
            confirm=True,
        )

    assert out.startswith("CONFIRMED —"), out
    assert f"Media ID   : {MEDIA_C}" in out
    assert "Status     : READY" in out
    assert "https://cdn.shopify.com/new.jpg" in out
    # Call sequence: read, stagedUploadsCreate, productCreateMedia, poll-read.
    assert [c[0] for c in fc.calls] == [
        GET_PRODUCT_MEDIA,
        STAGED_UPLOADS_CREATE,
        PRODUCT_CREATE_MEDIA,
        GET_PRODUCT_MEDIA,
    ]
    # stagedUploadsCreate input shape check: PUT, IMAGE, size as string.
    _, staged_vars = fc.calls[1]
    assert staged_vars["input"][0]["httpMethod"] == "PUT"
    assert staged_vars["input"][0]["resource"] == "IMAGE"
    assert isinstance(staged_vars["input"][0]["fileSize"], str)
    # productCreateMedia passes the resourceUrl from the staged target.
    _, attach_vars = fc.calls[2]
    assert attach_vars["productId"] == PRODUCT_GID
    assert attach_vars["media"] == [{
        "alt": "Smoke hero",
        "mediaContentType": "IMAGE",
        "originalSource": "https://shopify-cdn.example/resource",
    }]


def test_upload_execute_reorder_when_non_append_position():
    """position=1 with 2 existing media triggers a reorder after attach."""
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),
        _staged_ok(),
        _create_media_ok(mid=MEDIA_C, status="READY"),
        _product_media_read([
            _media_node(MEDIA_A), _media_node(MEDIA_B),
            _media_node(MEDIA_C, status="READY"),
        ]),
        _reorder_ok(done=True),
    ])

    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)), \
         patch("tools.media.time.sleep"):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/hero.jpg",
            position=1,
            confirm=True,
        )

    assert "CONFIRMED —" in out
    # The 5th call must be the reorder mutation with 0-indexed string position.
    assert fc.calls[4][0] == PRODUCT_REORDER_MEDIA
    reorder_vars = fc.calls[4][1]
    assert reorder_vars["id"] == PRODUCT_GID
    assert reorder_vars["moves"] == [{"id": MEDIA_C, "newPosition": "0"}]


def test_upload_execute_skips_reorder_when_position_equals_append():
    """position matching current_count + 1 is treated as append — no reorder."""
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A)]),
        _staged_ok(),
        _create_media_ok(mid=MEDIA_C, status="READY"),
        _product_media_read([
            _media_node(MEDIA_A), _media_node(MEDIA_C, status="READY"),
        ]),
    ])

    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)), \
         patch("tools.media.time.sleep"):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/a.jpg",
            position=2,
            confirm=True,
        )

    assert out.startswith("CONFIRMED —"), out
    assert [c[0] for c in fc.calls] == [
        GET_PRODUCT_MEDIA, STAGED_UPLOADS_CREATE, PRODUCT_CREATE_MEDIA, GET_PRODUCT_MEDIA,
    ]  # no reorder call


# ---------- upload_product_image — error surfacing by stage ----------

def test_upload_download_http_error_labels_download_stage():
    tools, fc = _build([_product_media_read([])])
    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get",
               return_value=FakeHTTPResponse(status_code=404)):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/missing.jpg",
            confirm=True,
        )
    assert out.startswith("Error at stage=download:"), out
    assert "404" in out


def test_upload_non_image_content_type_labels_download_stage():
    tools, fc = _build([_product_media_read([])])
    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=FakeHTTPResponse(
             status_code=200, content=b"<html>", headers={"Content-Type": "text/html"},
         )):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/page.jpg",
            confirm=True,
        )
    assert out.startswith("Error at stage=download:"), out
    assert "MIME" in out or "mime" in out.lower()


def test_upload_attach_user_errors_labelled_attach_stage():
    tools, fc = _build([
        _product_media_read([]),
        _staged_ok(),
        _create_media_err("media", "unsupported format"),
    ])
    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/hero.jpg",
            confirm=True,
        )
    assert out.startswith("Error at stage=attach:"), out
    assert "unsupported format" in out


def test_upload_staged_target_put_failure_labels_stage_upload():
    tools, fc = _build([
        _product_media_read([]),
        _staged_ok(),
    ])
    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put",
               return_value=FakeHTTPResponse(status_code=500, text="oops")):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/hero.jpg",
            confirm=True,
        )
    assert out.startswith("Error at stage=stage_upload:"), out


def test_upload_processing_timeout_returns_success_with_note():
    """Poll timing out with status=PROCESSING must return CONFIRMED + note,
    not an error. Storefront renders PROCESSING media in most cases."""
    tools, fc = _build([
        _product_media_read([]),
        _staged_ok(),
        _create_media_ok(mid=MEDIA_C, status="PROCESSING"),
        # Poll keeps returning PROCESSING. Give several responses so the loop
        # can iterate until its budget is exhausted (patched sleep fast-fwds).
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
        _product_media_read([_media_node(MEDIA_C, status="PROCESSING")]),
    ])

    # Advance monotonic by 3s per tick so the timeout (15s) fires after ~5 reads.
    tick = {"t": 0.0}
    def fake_monotonic():
        tick["t"] += 3.0
        return tick["t"]

    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)), \
         patch("tools.media.time.sleep"), \
         patch("tools.media.time.monotonic", side_effect=fake_monotonic):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/hero.jpg",
            confirm=True,
        )

    assert out.startswith("CONFIRMED —"), out
    assert "Status     : PROCESSING" in out
    assert "still PROCESSING" in out


# ---------- reorder_product_media ----------

def test_reorder_requires_moves():
    tools, fc = _build([])
    out = tools["reorder_product_media"](product_id="123", moves=[])
    assert out.startswith("Error:"), out
    assert fc.calls == []


def test_reorder_rejects_malformed_moves():
    tools, fc = _build([])
    out = tools["reorder_product_media"](
        product_id="123",
        moves=[{"id": MEDIA_A, "newPosition": 0}],  # 0 is invalid (1-indexed)
        confirm=True,
    )
    assert out.startswith("Error:"), out
    assert fc.calls == []


def test_reorder_preview_rejects_unknown_ids():
    tools, fc = _build([_product_media_read([_media_node(MEDIA_A)])])
    out = tools["reorder_product_media"](
        product_id="123",
        moves=[{"id": "gid://shopify/MediaImage/99999", "newPosition": 1}],
        confirm=False,
    )
    assert out.startswith("Error:"), out
    assert "99999" in out


def test_reorder_preview_does_not_execute():
    tools, fc = _build([_product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)])])
    out = tools["reorder_product_media"](
        product_id="123",
        moves=[{"id": MEDIA_B, "newPosition": 1}],
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert "confirm=True" in out
    # Only the read; no reorder issued in preview mode.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA


def test_reorder_execute_converts_to_zero_indexed_string():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B), _media_node(MEDIA_C)]),
        _reorder_ok(done=True),
    ])
    out = tools["reorder_product_media"](
        product_id="123",
        moves=[
            {"id": MEDIA_C, "newPosition": 1},
            {"id": MEDIA_A, "newPosition": 3},
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    _, reorder_vars = fc.calls[1]
    assert reorder_vars["moves"] == [
        {"id": MEDIA_C, "newPosition": "0"},
        {"id": MEDIA_A, "newPosition": "2"},
    ]


def test_reorder_polls_job_when_not_done():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),
        _reorder_ok(done=False, job_id="gid://shopify/Job/abc"),
        # poll_job uses JOB_STATUS_QUERY; we return a node with done=True.
        {"node": {"id": "gid://shopify/Job/abc", "done": True}},
    ])
    with patch("tools.media.time.sleep"):
        out = tools["reorder_product_media"](
            product_id="123",
            moves=[{"id": MEDIA_B, "newPosition": 1}],
            confirm=True,
        )
    assert "done=True" in out
    assert "Job        : abc" in out


def test_reorder_surfaces_media_user_errors():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),
        {"productReorderMedia": {
            "job": None,
            "mediaUserErrors": [{"field": "moves", "message": "bad position"}],
            "userErrors": [],
        }},
    ])
    out = tools["reorder_product_media"](
        product_id="123",
        moves=[{"id": MEDIA_B, "newPosition": 1}],
        confirm=True,
    )
    assert out.startswith("Error at stage=reorder:"), out
    assert "bad position" in out


# ---------- update_product_media ----------

def test_update_media_preview_shows_old_and_new_alt():
    tools, fc = _build([_product_media_read([
        _media_node(MEDIA_A, alt="old alt"),
    ])])
    out = tools["update_product_media"](
        product_id="123",
        media_id=MEDIA_A,
        alt="new alt",
        confirm=False,
    )
    assert out.startswith("PREVIEW —")
    assert "'old alt'" in out and "'new alt'" in out
    assert len(fc.calls) == 1


def test_update_media_no_op_detection():
    tools, fc = _build([_product_media_read([
        _media_node(MEDIA_A, alt="same"),
    ])])
    out = tools["update_product_media"](
        product_id="123",
        media_id=MEDIA_A,
        alt="same",
        confirm=False,
    )
    assert "no-op" in out


def test_update_media_rejects_unattached_media():
    tools, fc = _build([_product_media_read([_media_node(MEDIA_A)])])
    out = tools["update_product_media"](
        product_id="123",
        media_id="gid://shopify/MediaImage/99999",
        alt="x",
        confirm=False,
    )
    assert out.startswith("Error:"), out
    assert "99999" in out


def test_update_media_execute_mutation_shape():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A, alt="")]),
        {"productUpdateMedia": {
            "media": [{"id": MEDIA_A, "alt": "new"}],
            "mediaUserErrors": [],
        }},
    ])
    out = tools["update_product_media"](
        product_id="123", media_id=MEDIA_A, alt="new", confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    _, vars_ = fc.calls[1]
    assert vars_ == {
        "productId": PRODUCT_GID,
        "media": [{"id": MEDIA_A, "alt": "new"}],
    }


def test_update_media_user_errors_surfaced():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A)]),
        {"productUpdateMedia": {
            "media": [],
            "mediaUserErrors": [{"field": "alt", "message": "too long"}],
        }},
    ])
    out = tools["update_product_media"](
        product_id="123", media_id=MEDIA_A, alt="x" * 500, confirm=True,
    )
    assert out.startswith("Error at stage=update:"), out
    assert "too long" in out


# ---------- delete_product_media ----------

def test_delete_media_preview_does_not_execute():
    tools, fc = _build([_product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)])])
    out = tools["delete_product_media"](
        product_id="123", media_ids=[MEDIA_A], confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert "confirm=True" in out
    assert len(fc.calls) == 1


def test_delete_media_happy_path():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),
        {"productDeleteMedia": {
            "deletedMediaIds": [MEDIA_A],
            "product": {"id": PRODUCT_GID},
            "mediaUserErrors": [],
        }},
    ])
    out = tools["delete_product_media"](
        product_id="123", media_ids=[MEDIA_A], confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert "Deleted (1)" in out
    _, vars_ = fc.calls[1]
    assert vars_ == {"productId": PRODUCT_GID, "mediaIds": [MEDIA_A]}


def test_delete_media_filters_unattached_ids():
    """Ids not on the product are noted and excluded from the mutation."""
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A)]),
        {"productDeleteMedia": {
            "deletedMediaIds": [MEDIA_A],
            "product": {"id": PRODUCT_GID},
            "mediaUserErrors": [],
        }},
    ])
    out = tools["delete_product_media"](
        product_id="123",
        media_ids=[MEDIA_A, "gid://shopify/MediaImage/99999"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    assert "Not attached" in out and "99999" in out
    _, vars_ = fc.calls[1]
    assert vars_["mediaIds"] == [MEDIA_A]  # unattached id excluded


def test_delete_media_no_matches_is_no_op():
    tools, fc = _build([_product_media_read([_media_node(MEDIA_A)])])
    out = tools["delete_product_media"](
        product_id="123",
        media_ids=["gid://shopify/MediaImage/99999"],
        confirm=True,
    )
    assert "no-op" in out
    # Only the read — no delete mutation issued.
    assert len(fc.calls) == 1


def test_delete_media_user_errors_surfaced():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A)]),
        {"productDeleteMedia": {
            "deletedMediaIds": [],
            "product": {"id": PRODUCT_GID},
            "mediaUserErrors": [{"field": "mediaIds", "message": "locked"}],
        }},
    ])
    out = tools["delete_product_media"](
        product_id="123", media_ids=[MEDIA_A], confirm=True,
    )
    assert out.startswith("Error at stage=delete:"), out
    assert "locked" in out


def test_delete_media_dedupes_input_ids():
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A)]),
        {"productDeleteMedia": {
            "deletedMediaIds": [MEDIA_A],
            "product": {"id": PRODUCT_GID},
            "mediaUserErrors": [],
        }},
    ])
    tools["delete_product_media"](
        product_id="123", media_ids=[MEDIA_A, MEDIA_A, MEDIA_A], confirm=True,
    )
    _, vars_ = fc.calls[1]
    assert vars_["mediaIds"] == [MEDIA_A]


# ---------- FAILED processing status ----------

def test_upload_failed_processing_returns_error_with_cleanup_hint():
    """Media attached but Shopify marks it FAILED: must return an error, not
    a CONFIRMED success — the media is attached and the operator needs to
    delete it. The error message includes the media_id and a suggested
    delete_product_media call."""
    tools, fc = _build([
        _product_media_read([]),                                # initial read
        _staged_ok(),                                           # stagedUploadsCreate
        _create_media_ok(mid=MEDIA_C, status="PROCESSING"),     # productCreateMedia
        _product_media_read([                                   # poll: FAILED
            _media_node(MEDIA_C, status="FAILED"),
        ]),
    ])
    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)), \
         patch("tools.media.time.sleep"):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/broken.jpg",
            confirm=True,
        )

    assert out.startswith("Error at stage=process:"), out
    assert MEDIA_C in out
    assert "delete_product_media" in out  # cleanup hint for operator
    # Reorder must NOT have been attempted on a failed media.
    assert PRODUCT_REORDER_MEDIA not in [c[0] for c in fc.calls]


def test_upload_failed_processing_still_reorder_when_position_set():
    """Even when a caller passes position, we short-circuit on FAILED before
    the reorder stage — reorder on a failed media is pointless."""
    tools, fc = _build([
        _product_media_read([_media_node(MEDIA_A)]),
        _staged_ok(),
        _create_media_ok(mid=MEDIA_C, status="PROCESSING"),
        _product_media_read([
            _media_node(MEDIA_A), _media_node(MEDIA_C, status="FAILED"),
        ]),
    ])
    with patch("tools.media._reject_if_private_host", return_value=None), \
         patch("tools.media.requests.get", return_value=_make_http_response()), \
         patch("tools.media.requests.put", return_value=FakeHTTPResponse(status_code=200)), \
         patch("tools.media.time.sleep"):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/broken.jpg",
            position=1,
            confirm=True,
        )
    assert out.startswith("Error at stage=process:"), out
    assert PRODUCT_REORDER_MEDIA not in [c[0] for c in fc.calls]


# ---------- SSRF defense (_reject_if_private_host) ----------

def _resolve_to(*ips):
    """Build a getaddrinfo-shaped return value for the given IPs."""
    return [(socket.AF_INET, 0, 0, "", (ip, 0)) for ip in ips]


def test_ssrf_rejects_rfc1918_private():
    with patch("tools.media.socket.getaddrinfo",
               return_value=_resolve_to("10.0.0.5")):
        try:
            _reject_if_private_host("https://internal.corp/hero.jpg")
        except RuntimeError as e:
            assert "10.0.0.5" in str(e) and "SSRF" in str(e)
        else:
            raise AssertionError("expected RuntimeError for RFC1918 host")


def test_ssrf_rejects_link_local_imds():
    """169.254.169.254 is the AWS/GCP IMDS endpoint — the textbook SSRF target."""
    with patch("tools.media.socket.getaddrinfo",
               return_value=_resolve_to("169.254.169.254")):
        try:
            _reject_if_private_host("https://metadata.example/token")
        except RuntimeError as e:
            assert "169.254.169.254" in str(e)
        else:
            raise AssertionError("expected RuntimeError for link-local host")


def test_ssrf_rejects_loopback():
    with patch("tools.media.socket.getaddrinfo",
               return_value=_resolve_to("127.0.0.1")):
        try:
            _reject_if_private_host("https://localhost.example/hero.jpg")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError for loopback host")


def test_ssrf_rejects_any_private_ip_in_multi_record_resolution():
    """If a host resolves to multiple IPs and ANY are private, reject. A
    host that returns a mix of public and private addresses is often a
    rebinding attempt."""
    with patch("tools.media.socket.getaddrinfo",
               return_value=_resolve_to("93.184.216.34", "10.0.0.5")):
        try:
            _reject_if_private_host("https://mixed.example/hero.jpg")
        except RuntimeError as e:
            assert "10.0.0.5" in str(e)
        else:
            raise AssertionError("expected RuntimeError when any resolved IP is private")


def test_ssrf_accepts_public_ip():
    """example.com's canonical IP — must pass."""
    with patch("tools.media.socket.getaddrinfo",
               return_value=_resolve_to("93.184.216.34")):
        _reject_if_private_host("https://cdn.example.com/hero.jpg")  # no raise


def test_ssrf_unresolvable_host_is_rejected():
    with patch("tools.media.socket.getaddrinfo",
               side_effect=socket.gaierror("name resolution failed")):
        try:
            _reject_if_private_host("https://definitely-not-a-real-host.invalid/a.jpg")
        except RuntimeError as e:
            assert "could not resolve host" in str(e)
        else:
            raise AssertionError("expected RuntimeError on DNS failure")


def test_upload_ssrf_private_host_labels_download_stage():
    """End-to-end: an SSRF-private URL is rejected inside `_download_image`,
    which bubbles up to the caller as `Error at stage=download:`."""
    tools, fc = _build([_product_media_read([])])
    with patch("tools.media.socket.getaddrinfo",
               return_value=_resolve_to("10.0.0.5")):
        out = tools["upload_product_image"](
            product_id="123",
            source="https://internal.corp/hero.jpg",
            confirm=True,
        )
    assert out.startswith("Error at stage=download:"), out
    assert "SSRF" in out and "10.0.0.5" in out
