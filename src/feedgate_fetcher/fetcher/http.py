"""Fetch-and-upsert pipeline for a single feed.

``fetch_one`` performs one HTTP GET, parses the body, upserts entries,
and updates the feed's timer and lifecycle fields. One attempt per call
(no tenacity retry); the scheduler drives re-tries via ``next_fetch_at``.

On success:
  * ``last_successful_fetch_at`` and ``last_attempt_at`` set to ``now``
  * ``consecutive_failures = 0``, ``last_error_code = None``
  * ``status`` flipped to ``active`` if it was ``broken``
  * ETag / Last-Modified cached for conditional requests on next fetch

On failure:
  * ``last_attempt_at`` set to ``now``
  * ``consecutive_failures += 1``, ``last_error_code`` set
  * Lifecycle transitions: active → broken at threshold,
    broken → dead after ``dead_duration_days`` without success,
    HTTP 410 → dead immediately
  * Exception is swallowed — the scheduler tick continues
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
import structlog
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate_fetcher.feed_state import mark_fetch_failure, mark_fetch_success, transition_feed
from feedgate_fetcher.fetcher.errors import (
    NotAFeedError,
    ResponseTooLargeError,
    classify_error,
)
from feedgate_fetcher.fetcher.fallback import (
    FallbackError,
    FallbackResponse,
    fetch_via_impersonation,
)
from feedgate_fetcher.fetcher.parser import parse_feed
from feedgate_fetcher.fetcher.policy import (
    compute_next_fetch_at,
    is_not_a_feed_content_type,
    parse_cache_hint,
    parse_retry_after,
)
from feedgate_fetcher.fetcher.upsert import upsert_entries
from feedgate_fetcher.metrics import observe_fetch
from feedgate_fetcher.models import Entry, ErrorCode, Feed, FeedStatus
from feedgate_fetcher.ssrf import validate_public_url

logger = structlog.get_logger()

_DOMAINS_NEEDING_FALLBACK: set[str] = set()

__all__ = [
    "NotAFeedError",
    "ResponseTooLargeError",
    "fetch_one",
]


async def fetch_one(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    feed: Feed,
    *,
    now: datetime,
    interval_seconds: int,
    user_agent: str,
    max_bytes: int,
    max_entries_per_fetch: int,
    max_entries_initial: int,
    total_budget_seconds: float,
    broken_threshold: int,
    dead_duration_days: int,
    broken_max_backoff_seconds: int,
    backoff_jitter_ratio: float,
    entry_frequency_min_interval_seconds: int,
    entry_frequency_max_interval_seconds: int,
    entry_frequency_factor: int,
) -> None:
    _t0 = time.perf_counter()
    feed.last_attempt_at = now
    _server_hint: int | None = None

    cutoff = now - timedelta(days=7)
    weekly_entry_count_result = await session.execute(
        select(func.count()).where(
            Entry.feed_id == feed.id,
            Entry.fetched_at >= cutoff,
        )
    )
    weekly_entry_count: int = weekly_entry_count_result.scalar_one()

    try:
        # Pre-flight SSRF check on the feed's effective URL. This catches
        # late-binding DNS rebinding (a hostname that was public when
        # registered but now resolves to ``10.x``) before any socket is
        # opened. The HTTP transport runs the same check on every
        # redirect hop, so a 302 → private IP is also blocked.
        await validate_public_url(feed.effective_url)

        conditional_headers: dict[str, str] = {}
        if feed.etag:
            conditional_headers["If-None-Match"] = feed.etag
        elif feed.last_modified:
            conditional_headers["If-Modified-Since"] = feed.last_modified
        request_headers = {"User-Agent": user_agent, **conditional_headers}
        host = urlsplit(feed.effective_url).hostname or ""

        # Hard total wall-clock budget for the entire fetch — guards
        # against slow-loris streams that drip bytes just under the
        # per-chunk read timeout. ``asyncio.timeout`` raises
        # ``TimeoutError`` which ``_classify_error`` maps to
        # ``ErrorCode.TIMEOUT``.
        response_headers: httpx.Headers
        body: bytes
        if host in _DOMAINS_NEEDING_FALLBACK:
            cached_fallback_response = await fetch_via_impersonation(
                feed.effective_url,
                headers=request_headers,
                timeout_seconds=total_budget_seconds,
                max_bytes=max_bytes,
            )
            response_headers = cached_fallback_response.headers
            body = cached_fallback_response.content
        else:
            async with (
                asyncio.timeout(total_budget_seconds),
                http_client.stream(
                    "GET",
                    feed.effective_url,
                    headers=request_headers,
                    follow_redirects=True,
                ) as response,
            ):
                # 304 Not Modified — feed unchanged. Schedule the next fetch
                # and return early without touching consecutive_failures or status.
                if response.status_code == 304:
                    feed.last_successful_fetch_at = now
                    if feed.status == FeedStatus.BROKEN:
                        transition_feed(feed, FeedStatus.ACTIVE, reason="http_304_recovery")
                        feed.status = FeedStatus.ACTIVE
                        feed.consecutive_failures = 0
                        feed.last_error_code = None
                    _server_hint = parse_cache_hint(response.headers, now=now)
                    feed.next_fetch_at = compute_next_fetch_at(
                        feed,
                        now=now,
                        base_interval_seconds=interval_seconds,
                        broken_threshold=broken_threshold,
                        broken_max_backoff_seconds=broken_max_backoff_seconds,
                        backoff_jitter_ratio=backoff_jitter_ratio,
                        server_hint_seconds=_server_hint,
                        weekly_entry_count=weekly_entry_count,
                        entry_frequency_min_interval_seconds=entry_frequency_min_interval_seconds,
                        entry_frequency_max_interval_seconds=entry_frequency_max_interval_seconds,
                        entry_frequency_factor=entry_frequency_factor,
                    )
                    observe_fetch("not_modified", _t0)
                    return

                # 429 Rate Limited is NOT a circuit-breaker failure
                # (spec/feed.md). Honor Retry-After, record the code,
                # leave consecutive_failures and status alone, and bail
                # out of fetch_one early. We do NOT update
                # last_successful_fetch_at because we didn't succeed.
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("retry-after"), now=now)
                    wait_seconds = retry_after if retry_after is not None else 0
                    # Floor at base interval — don't hammer the upstream
                    # faster than our normal poll rate even if it says "10s".
                    wait_seconds = max(wait_seconds, interval_seconds)
                    feed.last_error_code = ErrorCode.RATE_LIMITED
                    feed.next_fetch_at = now + timedelta(seconds=wait_seconds)
                    observe_fetch("rate_limited", _t0)
                    return

                fallback_response: FallbackResponse | None = None
                if response.status_code == 403:
                    try:
                        fallback_response = await fetch_via_impersonation(
                            feed.effective_url,
                            headers=request_headers,
                            timeout_seconds=total_budget_seconds,
                            max_bytes=max_bytes,
                        )
                    except FallbackError:
                        response.raise_for_status()
                    else:
                        _DOMAINS_NEEDING_FALLBACK.add(host)

                if fallback_response is None:
                    response.raise_for_status()
                    permanent_moved = any(h.status_code == 301 for h in response.history)
                    if permanent_moved:
                        final_url = str(response.url)
                        if final_url != feed.effective_url:
                            feed.effective_url = final_url

                    response_headers = response.headers
                    body_parts: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        body_parts.append(chunk)
                        size += len(chunk)
                        if size > max_bytes:
                            raise ResponseTooLargeError(f"body exceeded {max_bytes} bytes")
                    body = b"".join(body_parts)
                else:
                    response_headers = fallback_response.headers
                    body = fallback_response.content

        ct = response_headers.get("content-type")
        if is_not_a_feed_content_type(ct):
            raise NotAFeedError(f"unexpected content-type: {ct}")

        parsed = await parse_feed(body)
        entries_to_upsert = parsed.entries
        if entries_to_upsert:
            if len(entries_to_upsert) > max_entries_per_fetch:
                entries_to_upsert = entries_to_upsert[:max_entries_per_fetch]
            has_existing_entries = (
                await session.execute(select(exists().where(Entry.feed_id == feed.id)))
            ).scalar_one()
            # Initial-fetch cap: a brand-new feed that advertises hundreds
            # of entries (OpenAI emits ~909, Hugging Face ~762) gets
            # truncated to the top N most-recent, matching what Feedly
            # does in production. Subsequent fetches ignore the cap —
            # the delta is almost always small and ON CONFLICT dedups.
            if not has_existing_entries and len(entries_to_upsert) > max_entries_initial:
                entries_to_upsert = entries_to_upsert[:max_entries_initial]
            await upsert_entries(session, feed.id, entries_to_upsert, now=now)

        mark_fetch_success(feed, now=now, title=parsed.title)

        new_etag = response_headers.get("etag")
        new_last_modified = response_headers.get("last-modified")
        if new_etag is not None:
            feed.etag = new_etag
        if new_last_modified is not None:
            feed.last_modified = new_last_modified
        _server_hint = parse_cache_hint(response_headers, now=now)
        if parsed.ttl_seconds is not None and parsed.ttl_seconds > 0:
            _server_hint = max(_server_hint or 0, parsed.ttl_seconds) or None
        observe_fetch("success", _t0)
    except Exception as exc:
        code = classify_error(exc)
        mark_fetch_failure(
            feed,
            now=now,
            code=code,
            broken_threshold=broken_threshold,
            dead_duration_days=dead_duration_days,
        )
        logger.warning(
            "fetch_one failed feed_id=%s url=%s code=%s err=%r",
            feed.id,
            feed.effective_url,
            code,
            exc,
        )
        observe_fetch("error", _t0, error_code=code)

    # Schedule the next fetch based on the final status. Active feeds
    # use base_interval_seconds; broken feeds use exponential backoff
    # with ±jitter; dead feeds are filtered out by the scheduler so
    # their next_fetch_at is effectively unused but still set for
    # consistency.
    feed.next_fetch_at = compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=interval_seconds,
        broken_threshold=broken_threshold,
        broken_max_backoff_seconds=broken_max_backoff_seconds,
        backoff_jitter_ratio=backoff_jitter_ratio,
        server_hint_seconds=_server_hint,
        weekly_entry_count=weekly_entry_count,
        entry_frequency_min_interval_seconds=entry_frequency_min_interval_seconds,
        entry_frequency_max_interval_seconds=entry_frequency_max_interval_seconds,
        entry_frequency_factor=entry_frequency_factor,
    )


# Backwards-compatible aliases for existing tests that import private
# names from this module. New code should import from
# feedgate_fetcher.fetcher.errors / .policy directly.
_classify_error = classify_error
_compute_next_fetch_at = compute_next_fetch_at
_parse_retry_after = parse_retry_after
