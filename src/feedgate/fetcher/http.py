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
import random
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.parser import parse_feed
from feedgate.fetcher.upsert import upsert_entries
from feedgate.lifecycle import ErrorCode, FeedStatus
from feedgate.models import Entry, Feed
from feedgate.ssrf import BlockedURLError, validate_public_url

logger = logging.getLogger(__name__)


class NotAFeedError(Exception):
    """Raised when a 200 OK response carries a Content-Type that is
    clearly not an RSS/Atom/XML feed (html, json, plain text)."""


class ResponseTooLargeError(Exception):
    """Raised when the streamed response body exceeds the configured
    size cap (``FETCH_MAX_BYTES``). Raised mid-stream so we never load
    the full oversized body into memory."""


def _compute_next_fetch_at(
    feed: Feed,
    *,
    now: datetime,
    base_interval_seconds: int,
    broken_threshold: int,
    broken_max_backoff_seconds: int,
    backoff_jitter_ratio: float,
) -> datetime:
    """Pick the next fetch instant based on the feed's current status.

    Active and dead feeds use the plain ``base_interval_seconds``
    (dead feeds are filtered out by the scheduler anyway, so the
    value is irrelevant for them, but we still set it for
    consistency). Broken feeds use exponential backoff:

        excess_failures = max(0, consecutive_failures - broken_threshold)
        factor = 2 ** excess_failures
        raw = base_interval_seconds * factor
        capped = min(raw, broken_max_backoff_seconds)
        jitter = uniform(-ratio, +ratio) * capped
        next = now + (capped + jitter) seconds

    The jitter prevents thundering-herd recovery when many feeds
    transition to broken together due to a shared upstream outage.
    """
    if feed.status != FeedStatus.BROKEN:
        return now + timedelta(seconds=base_interval_seconds)

    excess = max(0, feed.consecutive_failures - broken_threshold)
    factor = 2**excess
    raw_interval = base_interval_seconds * factor
    capped = min(raw_interval, broken_max_backoff_seconds)
    jitter_span = backoff_jitter_ratio * capped
    jitter = random.uniform(-jitter_span, jitter_span)
    return now + timedelta(seconds=capped + jitter)


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


def _parse_retry_after(header: str | None, *, now: datetime) -> int | None:
    """Parse ``Retry-After`` per RFC 7231 §7.1.3.

    Accepts either the integer-seconds form (``"120"``) or the
    HTTP-date form (``"Wed, 11 Apr 2026 07:30:00 GMT"``). Cloudflare,
    GitHub, and other large origins emit the date form in production,
    so supporting only seconds would silently drop the signal and
    hammer the upstream at base interval instead of honoring the
    requested delay.

    Returns the delay in seconds relative to ``now``, clamped at zero
    (a past date returns ``0``, not a negative number). Returns
    ``None`` when the header is absent or unparseable in either form.
    """
    if header is None:
        return None
    stripped = header.strip()
    # Integer-seconds form.
    try:
        return max(0, int(stripped))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231 §7.1.1.1). email.utils handles all three
    # legal date formats — IMF-fixdate, RFC 850, asctime.
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    # Dates without an explicit timezone are treated as UTC per RFC 7231.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta_seconds = (parsed - now).total_seconds()
    return max(0, int(delta_seconds))


def _classify_error(exc: BaseException) -> ErrorCode:
    """Map a fetch exception to a short error code."""
    if isinstance(exc, BlockedURLError):
        return ErrorCode.BLOCKED
    if isinstance(exc, NotAFeedError):
        return ErrorCode.NOT_A_FEED
    if isinstance(exc, ResponseTooLargeError):
        return ErrorCode.TOO_LARGE
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCode.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return ErrorCode.CONNECTION
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 410:
            return ErrorCode.HTTP_410
        if 400 <= status < 500:
            return ErrorCode.HTTP_4XX
        return ErrorCode.HTTP_5XX
    if isinstance(exc, httpx.HTTPError):
        return ErrorCode.HTTP_ERROR
    return ErrorCode.OTHER


async def fetch_one(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    feed: Feed,
    *,
    now: datetime,
    interval_seconds: int,
    user_agent: str,
    max_bytes: int,
    max_entries_initial: int,
    broken_threshold: int,
    dead_duration_days: int,
    broken_max_backoff_seconds: int,
    backoff_jitter_ratio: float,
) -> None:
    feed.last_attempt_at = now

    try:
        # Pre-flight SSRF check on the feed's effective URL. This catches
        # late-binding DNS rebinding (a hostname that was public when
        # registered but now resolves to ``10.x``) before any socket is
        # opened. The HTTP transport runs the same check on every
        # redirect hop, so a 302 → private IP is also blocked.
        await validate_public_url(feed.effective_url)
        async with http_client.stream(
            "GET",
            feed.effective_url,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        ) as response:
            # 429 Rate Limited is NOT a circuit-breaker failure
            # (spec/feed.md). Honor Retry-After, record the code,
            # leave consecutive_failures and status alone, and bail
            # out of fetch_one early. We do NOT update
            # last_successful_fetch_at because we didn't succeed.
            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("retry-after"), now=now)
                wait_seconds = retry_after if retry_after is not None else 0
                # Floor at base interval — don't hammer the upstream
                # faster than our normal poll rate even if it says "10s".
                wait_seconds = max(wait_seconds, interval_seconds)
                feed.last_error_code = ErrorCode.RATE_LIMITED
                feed.next_fetch_at = now + timedelta(seconds=wait_seconds)
                return

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
        if feed.status != FeedStatus.ACTIVE:
            _log_transition(feed, FeedStatus.ACTIVE, reason="fetch_succeeded")
            feed.status = FeedStatus.ACTIVE
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
        if code == ErrorCode.HTTP_410:
            if feed.status != FeedStatus.DEAD:
                _log_transition(feed, FeedStatus.DEAD, reason="http_410")
                feed.status = FeedStatus.DEAD
        else:
            # active -> broken on threshold
            if feed.status == FeedStatus.ACTIVE and feed.consecutive_failures >= broken_threshold:
                _log_transition(
                    feed,
                    FeedStatus.BROKEN,
                    reason=f"consecutive_failures>={broken_threshold}",
                )
                feed.status = FeedStatus.BROKEN
            # broken -> dead on time since last success (fall-through
            # allowed: if active just flipped to broken above AND the
            # feed already has no success for dead_duration_days, we
            # transition straight through to dead in the same call)
            if feed.status == FeedStatus.BROKEN:
                reference = feed.last_successful_fetch_at or feed.created_at
                if reference is not None and (
                    now - reference >= timedelta(days=dead_duration_days)
                ):
                    _log_transition(
                        feed,
                        FeedStatus.DEAD,
                        reason=f"no_success_for_>={dead_duration_days}d",
                    )
                    feed.status = FeedStatus.DEAD

    # Schedule the next fetch based on the final status. Active feeds
    # use base_interval_seconds; broken feeds use exponential backoff
    # with ±jitter; dead feeds are filtered out by the scheduler so
    # their next_fetch_at is effectively unused but still set for
    # consistency.
    feed.next_fetch_at = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=interval_seconds,
        broken_threshold=broken_threshold,
        broken_max_backoff_seconds=broken_max_backoff_seconds,
        backoff_jitter_ratio=backoff_jitter_ratio,
    )
