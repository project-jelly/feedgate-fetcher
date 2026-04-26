"""Fetch error types and classification helpers."""

from __future__ import annotations

import socket
import ssl

import httpx

from feedgate_fetcher.fetcher.fallback import FallbackError
from feedgate_fetcher.models import ErrorCode
from feedgate_fetcher.ssrf import BlockedURLError


class NotAFeedError(Exception):
    """Raised when a 200 OK response carries a Content-Type that is
    clearly not an RSS/Atom/XML feed (html, json, plain text)."""


class ResponseTooLargeError(Exception):
    """Raised when the streamed response body exceeds the configured
    size cap (``FETCH_MAX_BYTES``). Raised mid-stream so we never load
    the full oversized body into memory."""


def classify_error(exc: BaseException) -> ErrorCode:
    """Map a fetch exception to a short error code."""
    if isinstance(exc, BlockedURLError):
        return ErrorCode.BLOCKED
    if isinstance(exc, NotAFeedError):
        return ErrorCode.NOT_A_FEED
    if isinstance(exc, ResponseTooLargeError):
        return ErrorCode.TOO_LARGE
    if isinstance(exc, FallbackError):
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, ResponseTooLargeError):
            return ErrorCode.TOO_LARGE
        return ErrorCode.OTHER
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return ErrorCode.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return _classify_connect_cause(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 410:
            return ErrorCode.HTTP_410
        if 400 <= status < 500:
            return ErrorCode.HTTP_4XX
        return ErrorCode.HTTP_5XX
    if isinstance(exc, httpx.TooManyRedirects):
        return ErrorCode.REDIRECT_LOOP
    if isinstance(exc, httpx.HTTPError):
        return ErrorCode.HTTP_ERROR
    return ErrorCode.OTHER


def _classify_connect_cause(exc: httpx.ConnectError) -> ErrorCode:
    """Classify connect failures by traversing cause/context chain."""
    current: BaseException | None = exc
    seen: set[int] = set()

    for _ in range(8):
        if current is None:
            break
        if isinstance(current, ssl.SSLError):
            return ErrorCode.TLS_ERROR
        if isinstance(current, socket.gaierror):
            return ErrorCode.DNS
        if isinstance(current, ConnectionRefusedError):
            return ErrorCode.TCP_REFUSED

        current_id = id(current)
        if current_id in seen:
            break
        seen.add(current_id)

        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None

    return ErrorCode.CONNECTION
