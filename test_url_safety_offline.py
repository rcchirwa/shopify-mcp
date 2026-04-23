"""
Offline unit tests for tools/_url_safety.py.

Co-located with the SSRF guard so any new URL-accepting tool author can
discover both the helper and its regression suite from one place. The
upload-pipeline integration test that crosses into `_download_image` lives
in `test_media_offline.py` (under `test_upload_ssrf_*`).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_url_safety_offline.py -v
"""

import socket
from unittest.mock import patch

import pytest

from tools._url_safety import _reject_if_private_host


def _resolve_to(*ips):
    """Build a getaddrinfo-shaped return value for the given IPs."""
    return [(socket.AF_INET, 0, 0, "", (ip, 0)) for ip in ips]


# ---------- SSRF defense ----------


def test_ssrf_rejects_rfc1918_private():
    with (
        patch("tools._url_safety.socket.getaddrinfo", return_value=_resolve_to("10.0.0.5")),
        pytest.raises(RuntimeError) as exc,
    ):
        _reject_if_private_host("https://internal.corp/hero.jpg")
    msg = str(exc.value)
    assert "10.0.0.5" in msg and "SSRF" in msg


def test_ssrf_rejects_link_local_imds():
    """169.254.169.254 is the AWS/GCP IMDS endpoint — the textbook SSRF target."""
    with (
        patch("tools._url_safety.socket.getaddrinfo", return_value=_resolve_to("169.254.169.254")),
        pytest.raises(RuntimeError, match=r"169\.254\.169\.254"),
    ):
        _reject_if_private_host("https://metadata.example/token")


def test_ssrf_rejects_loopback():
    with (
        patch("tools._url_safety.socket.getaddrinfo", return_value=_resolve_to("127.0.0.1")),
        pytest.raises(RuntimeError),
    ):
        _reject_if_private_host("https://localhost.example/hero.jpg")


def test_ssrf_rejects_any_private_ip_in_multi_record_resolution():
    """If a host resolves to multiple IPs and ANY are private, reject. A
    host that returns a mix of public and private addresses is often a
    rebinding attempt."""
    with (
        patch(
            "tools._url_safety.socket.getaddrinfo",
            return_value=_resolve_to("93.184.216.34", "10.0.0.5"),
        ),
        pytest.raises(RuntimeError, match=r"10\.0\.0\.5"),
    ):
        _reject_if_private_host("https://mixed.example/hero.jpg")


def test_ssrf_accepts_public_ip():
    """example.com's canonical IP — must pass."""
    with patch("tools._url_safety.socket.getaddrinfo", return_value=_resolve_to("93.184.216.34")):
        _reject_if_private_host("https://cdn.example.com/hero.jpg")  # no raise


def test_ssrf_unresolvable_host_is_rejected():
    with (
        patch(
            "tools._url_safety.socket.getaddrinfo",
            side_effect=socket.gaierror("name resolution failed"),
        ),
        pytest.raises(RuntimeError, match="could not resolve host"),
    ):
        _reject_if_private_host("https://definitely-not-a-real-host.invalid/a.jpg")


# ---------- _reject_if_private_host edge shapes ----------


def test_reject_if_private_host_no_hostname_raises():
    """A URL with no hostname (e.g. `https:///path`) can't be resolved —
    refuse up front rather than passing through to getaddrinfo with None."""
    with pytest.raises(RuntimeError, match="no hostname"):
        _reject_if_private_host("https:///no-host")


def test_reject_if_private_host_skips_unparseable_ip_entries():
    """If getaddrinfo returns a malformed IP string (shouldn't happen in
    practice, but defensive), skip that entry rather than crashing."""
    # Mix a garbage entry with a public IP — must not raise.
    results = [
        (socket.AF_INET, 0, 0, "", ("not-an-ip", 0)),
        (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
    ]
    with patch("tools._url_safety.socket.getaddrinfo", return_value=results):
        _reject_if_private_host("https://example.com/a.jpg")  # must not raise


def test_reject_if_private_host_still_rejects_private_ip_after_unparseable_entry():
    """Regression guard for the loop semantics: the unparseable branch must
    `continue` to the next entry, not `break`/`return`. If someone refactors
    the try/except and short-circuits after the first garbage IP, a private
    IP listed AFTER it would silently slip through — exactly the SSRF
    defense this function exists to provide."""
    results = [
        (socket.AF_INET, 0, 0, "", ("not-an-ip", 0)),
        (socket.AF_INET, 0, 0, "", ("10.0.0.1", 0)),
    ]
    with (
        patch("tools._url_safety.socket.getaddrinfo", return_value=results),
        pytest.raises(RuntimeError) as exc,
    ):
        _reject_if_private_host("https://sneaky.example/a.jpg")
    msg = str(exc.value)
    assert "10.0.0.1" in msg and "SSRF" in msg
