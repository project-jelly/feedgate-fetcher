"""Fetch-and-upsert pipeline for a single feed.

``fetch_one`` performs one HTTP GET, parses the body, upserts any
entries, and updates the feed's timer fields. Walking skeleton scope:

  * No retry / tenacity — one attempt.
  * No per-host rate limit, no ETag/If-Modified-Since conditional
    requests (left for a later PR).
  * No status-machine transitions — ``feeds.status`` stays
    ``'active'`` regardless of failure; we only record the error code.
  * No response-body size cap enforcement beyond httpx defaults.

On success:
  * ``last_successful_fetch_at`` and ``last_attempt_at`` both set to
    ``now``
  * ``next_fetch_at = now + interval_seconds``
  * ``consecutive_failures = 0``, ``last_error_code = None``
  * ``title`` refreshed from the parsed feed metadata if present

On failure:
  * ``last_attempt_at`` set to ``now``
  * ``next_fetch_at = now + interval_seconds`` (no backoff yet)
  * ``consecutive_failures += 1``
  * ``last_error_code`` set to a short string code
  * Exception is swallowed — the scheduler tick keeps going
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.parser import parse_feed
from feedgate.fetcher.upsert import upsert_entries
from feedgate.models import Entry, Feed

logger = logging.getLogger(__name__)


class NotAFeedError(Exception):
    """Raised when a 200 OK response carries a Content-Type that is
    clearly not an RSS/Atom/XML feed (html, json, plain text)."""


class ResponseTooLargeError(Exception):
    """Raised when the streamed response body exceeds the configured
    size cap (``FETCH_MAX_BYTES``). Raised mid-stream so we never load
    the full oversized body into memory."""


DEFAULT_FETCH_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB
DEFAULT_FETCH_MAX_ENTRIES_INITIAL: int = 50
DEFAULT_BROKEN_THRESHOLD: int = 3
DEFAULT_DEAD_DURATION_DAYS: int = 7


def _log_transition(feed: Feed, new_status: str, *, reason: str) -> None:
    """Emit a WARNING-level log entry for a feed lifecycle transition.

    INFO is currently swallowed by the default stdlib root logger
    configuration, so lifecycle moves go out at WARNING level to
    ensure operator visibility (see spec/feed.md "관찰 가능성").
    """
    logger.warning(
        "feed_id=%s url=%s state=%s->%s reason=%s",
        feed.id,
        feed.effective_url,
        feed.status,
        new_status,
        reason,
    )


# Content types that unambiguously indicate "this is not a feed".
# We intentionally do NOT maintain an allow-list because many feeds
# serve odd values like ``application/octet-stream`` (Python Insider)
# or omit the header entirely; feedparser handles those just fine.
NOT_A_FEED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/ld+json",
        "text/plain",
    }
)


def _is_not_a_feed_content_type(ct: str | None) -> bool:
    """Return True if the Content-Type header is clearly not a feed."""
    if not ct:
        return False
    base = ct.split(";", 1)[0].strip().lower()
    return base in NOT_A_FEED_CONTENT_TYPES


def _classify_error(exc: BaseException) -> str:
    """Map a fetch exception to a short error code."""
    if isinstance(exc, NotAFeedError):
        return "not_a_feed"
    if isinstance(exc, ResponseTooLargeError):
        return "too_large"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 410:
            return "http_410"
        if 400 <= status < 500:
            return "http_4xx"
        return "http_5xx"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return "other"


async def fetch_one(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    feed: Feed,
    *,
    now: datetime,
    interval_seconds: int,
    user_agent: str,
    max_bytes: int = DEFAULT_FETCH_MAX_BYTES,
    max_entries_initial: int = DEFAULT_FETCH_MAX_ENTRIES_INITIAL,
    broken_threshold: int = DEFAULT_BROKEN_THRESHOLD,
    dead_duration_days: int = DEFAULT_DEAD_DURATION_DAYS,
) -> None:
    next_at = now + timedelta(seconds=interval_seconds)
    feed.last_attempt_at = now
    feed.next_fetch_at = next_at

    try:
        async with http_client.stream(
            "GET",
            feed.effective_url,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        ) as response:
            response.raise_for_status()

            ct = response.headers.get("content-type")
            if _is_not_a_feed_content_type(ct):
                raise NotAFeedError(f"unexpected content-type: {ct}")

            body_parts: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                body_parts.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ResponseTooLargeError(f"body exceeded {max_bytes} bytes")
            body = b"".join(body_parts)

        parsed = await parse_feed(body)
        entries_to_upsert = parsed.entries
        if entries_to_upsert:
            existing_count = (
                await session.execute(
                    select(func.count()).select_from(Entry).where(Entry.feed_id == feed.id)
                )
            ).scalar_one()
            # Initial-fetch cap: a brand-new feed that advertises hundreds
            # of entries (OpenAI emits ~909, Hugging Face ~762) gets
            # truncated to the top N most-recent, matching what Feedly
            # does in production. Subsequent fetches ignore the cap —
            # the delta is almost always small and ON CONFLICT dedups.
            if existing_count == 0 and len(entries_to_upsert) > max_entries_initial:
                entries_to_upsert = entries_to_upsert[:max_entries_initial]
            await upsert_entries(session, feed.id, entries_to_upsert, now=now)

        if parsed.title:
            feed.title = parsed.title
        feed.last_successful_fetch_at = now
        feed.last_error_code = None
        feed.consecutive_failures = 0
        if feed.status != "active":
            _log_transition(feed, "active", reason="fetch_succeeded")
            feed.status = "active"
    except Exception as exc:
        code = _classify_error(exc)
        feed.last_error_code = code
        feed.consecutive_failures = feed.consecutive_failures + 1
        logger.warning(
            "fetch_one failed feed_id=%s url=%s code=%s err=%r",
            feed.id,
            feed.effective_url,
            code,
            exc,
        )

        # Lifecycle transitions (spec/feed.md — circuit breaker + 410)
        if code == "http_410":
            if feed.status != "dead":
                _log_transition(feed, "dead", reason="http_410")
                feed.status = "dead"
        else:
            # active -> broken on threshold
            if feed.status == "active" and feed.consecutive_failures >= broken_threshold:
                _log_transition(
                    feed,
                    "broken",
                    reason=f"consecutive_failures>={broken_threshold}",
                )
                feed.status = "broken"
            # broken -> dead on time since last success (fall-through
            # allowed: if active just flipped to broken above AND the
            # feed already has no success for dead_duration_days, we
            # transition straight through to dead in the same call)
            if feed.status == "broken":
                reference = feed.last_successful_fetch_at or feed.created_at
                if reference is not None and (
                    now - reference >= timedelta(days=dead_duration_days)
                ):
                    _log_transition(
                        feed,
                        "dead",
                        reason=f"no_success_for_>={dead_duration_days}d",
                    )
                    feed.status = "dead"
