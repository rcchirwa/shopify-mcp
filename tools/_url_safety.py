"""
URL safety helpers shared across URL-accepting tools.

Provides the SSRF guard applied by `ShopifyClient.fetch_bytes` (the single
chokepoint for caller-supplied URL fetches; the image-download path in
`tools.media._upload` routes through it). Any new tool that accepts a
caller-supplied URL and dereferences it server-side MUST go through
`fetch_bytes` — or, if it cannot, call `_reject_if_private_host` itself before
issuing the request.
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
    this closes it at the network boundary.

    Accepted risk — DNS rebinding / TOCTOU (SEC-03, security audit 2026-07-04):
    the check resolves the host and rejects non-public IPs, but the resolved
    IP is *not* pinned through to the request. A host that resolves public at
    check time and private at fetch (connect) time can slip past this guard.
    Attacker-controlled DNS is the baseline for this guard — the caller (a
    prompt-injectable model) supplies the URL, so pointing it at an
    attacker-owned domain is assumed, not a barrier; the real obstacle is
    winning the TOCTOU race between the getaddrinfo check here and requests'
    re-resolution at connect time. Closing it means pinning the validated IP
    into the connection while preserving the original hostname for TLS SNI and
    certificate validation — a custom requests/urllib3 adapter that is easy to
    get subtly wrong and carries ongoing maintenance cost.

    Decision (Story 10.43): formally accept the risk. Rationale — this is a
    Low-severity finding on a *local stdio* MCP server, and defense-in-depth
    already narrows it: `fetch_bytes` refuses redirects by default (the
    image-download caller passes `allow_redirects=False`) and hard-caps the
    body, and the media-upload caller filters to `image/*`
    (`tools/media/_upload.py`) behind a confirm/preview gate. The adapter
    complexity is not proportionate at this threat level. Reopen trigger: **if
    this process is ever deployed where internal/metadata endpoints (e.g.
    169.254.169.254 IMDS, RFC1918 hosts) are egress-reachable — any cloud VM,
    container, or CI runner, regardless of network ingress** — implement
    IP-pinning (see TECH_DEBT.md → SEC-03).
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
