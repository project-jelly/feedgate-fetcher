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
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.parser import parse_feed
from feedgate.fetcher.upsert import upsert_entries
from feedgate.models import Feed

logger = logging.getLogger(__name__)


def _classify_error(exc: BaseException) -> str:
    """Map a fetch exception to a short error code."""
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
) -> None:
    next_at = now + timedelta(seconds=interval_seconds)
    feed.last_attempt_at = now
    feed.next_fetch_at = next_at

    try:
        response = await http_client.get(
            feed.effective_url,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )
        response.raise_for_status()
        body = response.content

        parsed = await parse_feed(body)
        if parsed.entries:
            await upsert_entries(session, feed.id, parsed.entries, now=now)

        if parsed.title:
            feed.title = parsed.title
        feed.last_successful_fetch_at = now
        feed.last_error_code = None
        feed.consecutive_failures = 0
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
