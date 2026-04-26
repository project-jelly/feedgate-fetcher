"""In-process asyncio scheduler for feedgate-fetcher.

  * ``tick_once(app)`` — the unit of work. Atomically reserves a
    batch of due feeds via ``SELECT ... FOR UPDATE SKIP LOCKED``,
    advances their ``next_fetch_at`` to ``now + claim_ttl_seconds``
    (and ``last_attempt_at = now`` to cover the dead-probe path),
    commits to release the row locks, and then calls ``fetch_one``
    on each in a fresh session. The lease mechanic is what lets N
    worker replicas run in parallel against a single Postgres
    without claiming the same feed twice — the SKIP LOCKED avoids
    the in-flight claim, the timestamp bump prevents re-claim after
    commit, and a crashed worker's rows become claimable again once
    the TTL elapses. See ``docs/spec/feed.md`` for the queue
    semantics.

  * ``run(app, stop_event)`` — the background loop that drives
    ``tick_once`` on a fixed interval. Stops cleanly when
    ``stop_event`` is set.

The app reads its state from ``app.state``:
  * ``session_factory`` (sqlalchemy async_sessionmaker)
  * ``http_client`` (httpx.AsyncClient)
  * ``fetch_interval_seconds``, ``fetch_user_agent``, ``fetch_concurrency``
  * ``fetch_claim_batch_size``, ``fetch_claim_ttl_seconds``
  * full fetcher tunables (broken_threshold, dead_duration_days, ...)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import FastAPI
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate_fetcher.fetcher.http import fetch_one
from feedgate_fetcher.models import Feed, FeedStatus

logger = structlog.get_logger()


@dataclass(frozen=True)
class SchedulerTickResult:
    """Outcome of one scheduler.tick_once invocation."""

    claimed: int
    processed: int
    fatal_errors: int
    duration_seconds: float


async def _process_feed(
    feed_id: int,
    app: FastAPI,
    sem: asyncio.Semaphore,
    now: datetime,
) -> bool:
    """Open a fresh session, load the feed, run fetch_one, commit."""
    sf = app.state.session_factory
    http_client = app.state.http_client

    async with sem, sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
        if feed is None:
            return True
        try:
            await fetch_one(
                session,
                http_client,
                feed,
                now=now,
                interval_seconds=app.state.fetch_interval_seconds,
                user_agent=app.state.fetch_user_agent,
                max_bytes=app.state.fetch_max_bytes,
                max_entries_per_fetch=app.state.fetch_max_entries_per_fetch,
                max_entries_initial=app.state.fetch_max_entries_initial,
                total_budget_seconds=app.state.fetch_total_budget_seconds,
                broken_threshold=app.state.broken_threshold,
                dead_duration_days=app.state.dead_duration_days,
                broken_max_backoff_seconds=app.state.broken_max_backoff_seconds,
                backoff_jitter_ratio=app.state.backoff_jitter_ratio,
                entry_frequency_min_interval_seconds=app.state.entry_frequency_min_interval_seconds,
                entry_frequency_max_interval_seconds=app.state.entry_frequency_max_interval_seconds,
                entry_frequency_factor=app.state.entry_frequency_factor,
            )
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.exception("fatal error in _process_feed feed_id=%s", feed_id)
            return False


async def _claim_due_feeds(
    session: AsyncSession,
    *,
    now: datetime,
    claim_batch_size: int,
    claim_ttl_seconds: int,
    dead_probe_interval_days: int,
) -> list[int]:
    """Reserve up to ``claim_batch_size`` feeds via SKIP LOCKED.

    Two classes of feeds are eligible:

      1. Non-dead feeds (active + broken) whose ``next_fetch_at`` is
         due. This respects the per-feed exponential backoff for
         broken feeds — they are only polled when their own schedule
         says so.
      2. Dead feeds whose ``last_attempt_at`` is older than
         ``dead_probe_interval_days`` (weekly probe, spec/feed.md).
         Dead feeds never had their backoff respected anyway (once
         dead you never come back via the normal path) — the probe
         is the only way they can get re-fetched.

    Every claimed row has both ``next_fetch_at`` bumped to
    ``now + claim_ttl_seconds`` AND ``last_attempt_at`` bumped to
    ``now``. The ``next_fetch_at`` bump covers the non-dead gate;
    the ``last_attempt_at`` bump covers the dead-probe gate. The
    caller MUST commit the session after this returns for the lease
    to become visible to other workers.
    """
    probe_cutoff = now - timedelta(days=dead_probe_interval_days)
    claim_predicate = or_(
        and_(
            Feed.status != FeedStatus.DEAD,
            Feed.next_fetch_at <= now,
        ),
        and_(
            Feed.status == FeedStatus.DEAD,
            or_(
                Feed.last_attempt_at.is_(None),
                Feed.last_attempt_at < probe_cutoff,
            ),
        ),
    )
    claim_ids_stmt = (
        select(Feed.id)
        .where(claim_predicate)
        .order_by(Feed.next_fetch_at)
        .limit(claim_batch_size)
        .with_for_update(skip_locked=True)
    )
    lease_until = now + timedelta(seconds=claim_ttl_seconds)
    update_stmt = (
        update(Feed)
        .where(Feed.id.in_(claim_ids_stmt))
        .values(next_fetch_at=lease_until, last_attempt_at=now)
        .returning(Feed.id)
    )
    claimed_ids = list((await session.execute(update_stmt)).scalars().all())
    return claimed_ids


async def tick_once(app: FastAPI, *, now: datetime | None = None) -> SchedulerTickResult:
    """Run one scheduler iteration against a Postgres-as-queue.

    Step 1 — claim: open a session, ``SELECT ... FOR UPDATE SKIP
    LOCKED`` a batch of due feeds, bump each row's ``next_fetch_at``
    and ``last_attempt_at`` as a crash-safe lease, commit. Other
    workers running in parallel will not see the claimed rows until
    the lease expires or ``fetch_one`` rewrites the timestamps.

    Step 2 — fetch: for each claimed id, open a fresh session and
    call ``fetch_one`` under a bounded semaphore. Each fetch is
    isolated, so one failing feed cannot poison the others.
    """
    started_at = time.perf_counter()
    now = now or datetime.now(UTC)
    sf = app.state.session_factory
    async with sf() as session:
        feed_ids = await _claim_due_feeds(
            session,
            now=now,
            claim_batch_size=app.state.fetch_claim_batch_size,
            claim_ttl_seconds=app.state.fetch_claim_ttl_seconds,
            dead_probe_interval_days=app.state.dead_probe_interval_days,
        )
        await session.commit()

    claimed = len(feed_ids)
    if not feed_ids:
        return SchedulerTickResult(
            claimed=0,
            processed=0,
            fatal_errors=0,
            duration_seconds=time.perf_counter() - started_at,
        )

    sem = asyncio.Semaphore(app.state.fetch_concurrency)
    outcomes = await asyncio.gather(*(_process_feed(fid, app, sem, now) for fid in feed_ids))
    processed = sum(1 for ok in outcomes if ok)
    fatal_errors = sum(1 for ok in outcomes if not ok)
    return SchedulerTickResult(
        claimed=claimed,
        processed=processed,
        fatal_errors=fatal_errors,
        duration_seconds=time.perf_counter() - started_at,
    )


async def run(
    app: FastAPI,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that calls ``tick_once`` every interval.

    Stops when ``stop_event`` is set. Exceptions from ``tick_once`` are
    logged and swallowed so a single bad tick cannot kill the loop.
    Consecutive failures trigger exponential backoff up to 300s.
    """
    interval = app.state.fetch_interval_seconds
    stop = stop_event or asyncio.Event()
    consecutive_errors = 0
    while not stop.is_set():
        try:
            await tick_once(app)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            logger.exception("scheduler tick raised; continuing")
        timeout = interval
        if consecutive_errors > 0:
            timeout = min(
                interval * (2 ** min(consecutive_errors - 1, 5)),
                300,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout)
        except TimeoutError:
            continue
