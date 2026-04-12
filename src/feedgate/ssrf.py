"""SSRF protection — block URLs that target non-public addresses.

Three layers, all funneled through ``validate_public_url``:

  1. **Scheme** must be ``http`` or ``https``. ``file://``, ``gopher://``,
     ``ftp://`` are rejected outright.
  2. **IP literal** in the URL host (``http://10.0.0.1/``,
     ``http://[::1]/``) is checked against the blocked set without DNS.
  3. **Hostname** is resolved via ``loop.getaddrinfo`` and **every**
     returned address is checked. If any one resolves to a private,
     loopback, link-local, reserved, multicast, or unspecified address
     the URL is rejected.

The hostname resolution check is the load-bearing one: it catches
DNS rebinding (a public hostname whose A record points to ``10.x``)
and is re-run by the HTTP transport guard on every redirect hop, so a
``302 → http://169.254.169.254/`` cannot escape it either.

Resolution failures (NXDOMAIN, no A record) are deliberately *allowed*
through. Treating a flaky resolver as "blocked" would silently drop
legitimate registrations, and an unresolvable host fails at fetch time
anyway — it cannot reach an internal target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


class BlockedURLError(ValueError):
    """Raised when a URL targets a non-public address or unsupported scheme."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve(host: str) -> list[str]:
    """Async DNS resolution. Returns the list of address strings, or
    an empty list on resolver failure (NXDOMAIN, no A record, etc.).

    Indirected through this helper so tests can monkeypatch a single
    function instead of stubbing ``socket.getaddrinfo`` globally.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return [info[4][0] for info in infos]


async def validate_public_url(url: str, *, resolve: bool = True) -> None:
    """Reject URLs that target non-public addresses.

    ``resolve=False`` skips the DNS step and only checks scheme + IP
    literals. Use it on hot paths where DNS latency or test-environment
    DNS dependencies are a concern (e.g. the ``POST /v1/feeds``
    endpoint), and rely on the fetcher's own pre-flight call (which
    runs with ``resolve=True``) to catch hostnames that resolve to a
    blocked address.

    Raises :class:`BlockedURLError` on violation.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"unsupported scheme: {scheme!r}")

    host = parts.hostname
    if not host:
        raise BlockedURLError("missing host")

    # Literal IP — check directly without DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise BlockedURLError(f"blocked address: {host}")
        return

    if not resolve:
        return

    addrs = await _resolve(host)
    for addr in addrs:
        # getaddrinfo can hand back scoped v6 like "fe80::1%lo0".
        try:
            resolved = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise BlockedURLError(f"{host} resolves to blocked address {addr}")


class SSRFGuardTransport(httpx.AsyncBaseTransport):
    """``httpx.AsyncBaseTransport`` wrapper that re-validates every
    request URL — including redirect follow-ups — through
    :func:`validate_public_url`.

    The point of guarding at the transport layer (rather than only
    pre-validating in ``fetch_one``) is that ``httpx.AsyncClient`` calls
    ``handle_async_request`` once per HTTP exchange, so a ``302`` to
    ``http://10.0.0.1/`` re-enters this method with the new URL and
    gets blocked before the socket is opened.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await validate_public_url(str(request.url))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
