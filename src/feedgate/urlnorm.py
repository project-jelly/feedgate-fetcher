"""URL normalization per docs/spec/feed.md.

Minimum rules applied:
  * scheme lowercase
  * host lowercase (and IDN → punycode)
  * default ports (:80, :443) stripped
  * trailing slash on non-root paths removed
  * root "/" collapsed to empty (so "http://x.com/" == "http://x.com")
  * fragment (#...) removed
  * query preserved as-is (some feeds use query params to filter)

This is intentionally a small hand-rolled function — rfc3986 does not
strip default ports or remove fragments, and a dozen lines of
``urllib.parse`` covers our needs exactly.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _to_idna(host: str) -> str:
    """Convert an IDN host to its punycode form, lowercased on failure."""
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host.lower()


def normalize_url(raw: str) -> str:
    parts = urlsplit(raw.strip())

    scheme = parts.scheme.lower()
    host = _to_idna(parts.hostname or "")

    port = parts.port
    if port is not None and DEFAULT_PORTS.get(scheme) == port:
        port = None

    netloc = host
    if parts.username:
        cred = parts.username
        if parts.password:
            cred = f"{cred}:{parts.password}"
        netloc = f"{cred}@{netloc}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""

    return urlunsplit((scheme, netloc, path, parts.query, ""))
