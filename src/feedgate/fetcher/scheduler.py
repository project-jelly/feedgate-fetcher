"""In-process asyncio scheduler for feedgate-fetcher.

Walking skeleton design:

  * ``tick_once(app)`` — the unit of work. Lists every feed currently
    in status ``'active'`` and calls ``fetch_one`` on each under a
    per-feed session. A global ``asyncio.Semaphore`` bounds concurrency.
    ``tick_once`` is the **TDD-covered** entry point.

  * ``run(app, interval_seconds, stop_event)`` — the background loop
    that drives ``tick_once`` on a timer. Intentionally thin and
    **TDD-exempt** per the plan (policy in ``.omc/plans/ralplan-
    feedgate-walking-skeleton.md`` WP 4.3).

The app reads its state from ``app.state``:
  * ``session_factory`` (sqlalchemy async_sessionmaker)
  * ``http_client`` (httpx.AsyncClient)
  * ``fetch_interval_seconds`` (int)
  * ``fetch_user_agent`` (str)
  * ``fetch_concurrency`` (int, optional, default 4)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import and_, or_, select

from feedgate.fetcher.http import fetch_one
from feedgate.lifecycle import FeedStatus
from feedgate.models import Feed

logger = logging.getLogger(__name__)


async def _process_feed(
    feed_id: int,
    app: FastAPI,
    sem: asyncio.Semaphore,
    now: datetime,
) -> None:
    """Open a fresh session, load the feed, run fetch_one, commit."""
    sf = app.state.session_factory
    http_client = app.state.http_client
    interval = app.state.fetch_interval_seconds
    ua = app.state.fetch_user_agent
    max_bytes = app.state.fetch_max_bytes
    max_entries_initial = app.state.fetch_max_entries_initial
    broken_threshold = app.state.broken_threshold
    dead_duration_days = app.state.dead_duration_days
    broken_max_backoff_seconds = app.state.broken_max_backoff_seconds
    backoff_jitter_ratio = app.state.backoff_jitter_ratio

    async with sem, sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
        if feed is None:
            return
        try:
            await fetch_one(
                session,
                http_client,
                feed,
                now=now,
                interval_seconds=interval,
                user_agent=ua,
                max_bytes=max_bytes,
                max_entries_initial=max_entries_initial,
                broken_threshold=broken_threshold,
                dead_duration_days=dead_duration_days,
                broken_max_backoff_seconds=broken_max_backoff_seconds,
                backoff_jitter_ratio=backoff_jitter_ratio,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("fatal error in _process_feed feed_id=%s", feed_id)


async def tick_once(app: FastAPI, *, now: datetime | None = None) -> None:
    """Run one scheduler iteration.

    Two classes of feeds are picked up on each tick:

      1. Non-dead feeds (active + broken) whose ``next_fetch_at`` is
         due. This respects the per-feed exponential backoff for
         broken feeds — they are only polled when their own schedule
         says so.
      2. Dead feeds whose ``last_attempt_at`` is older than
         ``dead_probe_interval_days`` (weekly probe, spec/feed.md).
         Dead feeds never had their backoff respected anyway (once
         dead you never come back via the normal path) — the probe
         is the only way they can get re-fetched.
    """
    now = now or datetime.now(UTC)
    probe_cutoff = now - timedelta(days=app.state.dead_probe_interval_days)

    sf = app.state.session_factory
    async with sf() as session:
        stmt = select(Feed.id).where(
            or_(
                # Non-dead, due for fetch
                and_(
                    Feed.status != FeedStatus.DEAD,
                    Feed.next_fetch_at <= now,
                ),
                # Dead, eligible for a weekly probe
                and_(
                    Feed.status == FeedStatus.DEAD,
                    or_(
                        Feed.last_attempt_at.is_(None),
                        Feed.last_attempt_at < probe_cutoff,
                    ),
                ),
            )
        )
        result = await session.execute(stmt)
        feed_ids = [row[0] for row in result.all()]

    if not feed_ids:
        return

    sem = asyncio.Semaphore(app.state.fetch_concurrency)
    await asyncio.gather(*(_process_feed(fid, app, sem, now) for fid in feed_ids))


async def run(
    app: FastAPI,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that calls ``tick_once`` every interval.

    TDD-exempt per WP 4.3 — the loop body is intentionally ~10 LOC and
    its correctness is covered by the integration tests of ``tick_once``
    and by F2 (walking-skeleton E2E). The stop_event parameter is a
    plain ``asyncio.Event``; ``None`` means "run until cancelled".
    """
    interval = app.state.fetch_interval_seconds
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await tick_once(app)
        except Exception:
            logger.exception("scheduler tick raised; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
