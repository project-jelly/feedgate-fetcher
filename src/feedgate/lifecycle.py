"""Feed lifecycle constants.

``FeedStatus`` and ``ErrorCode`` are the canonical string values used
across the ORM, API schemas, and fetcher. They are ``StrEnum`` so that
comparisons against plain strings still work (``FeedStatus.ACTIVE ==
"active"`` is ``True``) and JSON serialization is the same as before.

The spec of record is ``docs/spec/feed.md`` — adding a new value here
without updating that document will drift. ``last_error_code`` is also
user-visible, so adding/removing an ``ErrorCode`` member is an API
contract change.
"""

from __future__ import annotations

from enum import StrEnum


class FeedStatus(StrEnum):
    ACTIVE = "active"
    BROKEN = "broken"
    DEAD = "dead"


class ErrorCode(StrEnum):
    # Network / transport
    DNS = "dns"
    TCP_REFUSED = "tcp_refused"
    TLS_ERROR = "tls_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    # HTTP status classes
    HTTP_4XX = "http_4xx"
    HTTP_410 = "http_410"
    HTTP_5XX = "http_5xx"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    # Content / parsing
    NOT_A_FEED = "not_a_feed"
    PARSE_ERROR = "parse_error"
    REDIRECT_LOOP = "redirect_loop"
    TOO_LARGE = "too_large"
    # SSRF guard rejected the URL (private IP, bad scheme, etc.)
    BLOCKED = "blocked"
    # Fallback
    OTHER = "other"
