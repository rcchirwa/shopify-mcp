"""
URL safety helpers shared across URL-accepting tools.

Currently provides the SSRF guard used by `tools.media._download_image`.
Any new tool that accepts a caller-supplied URL and dereferences it
server-side MUST call `_reject_if_private_host` before issuing the request.
"""

import ipaddress
import socket
from urllib.parse import urlparse


def _reject_if_private_host(url: str) -> None:
    """Raise RuntimeError if the URL's hostname resolves to a non-public IP
    (RFC1918 private, loopback, link-local, multicast, reserved, unspecified).

    Bounded SSRF defense: without this, a prompt-injected caller could
    target 169.254.169.254 (cloud IMDS), 10/8 / 172.16/12 / 192.168/16
    internals, or localhost via any `https://` URL. The confirm/preview gate
    and `image/*` MIME filter narrow the exfil surface but don't close it —
    this closes it at the network boundary. TOCTOU against DNS rebinding
    is out of scope; that fix requires pinning the resolved IP through the
    request, which is not worth the complexity for this threat model.
    """
    host = urlparse(url).hostname
    if not host:
        raise RuntimeError("URL has no hostname")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise RuntimeError(f"could not resolve host {host!r}: {e}") from e
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise RuntimeError(
                f"host {host!r} resolves to non-public IP {ip_str} "
                f"— blocked to prevent SSRF to internal resources"
            )
