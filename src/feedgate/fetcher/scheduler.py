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
    ``tick_once`` on a timer. Intentionally thin and TDD-exempt per
    the plan; its correctness is covered by tick_once integration
    tests and F2 (walking-skeleton E2E).

The app reads its state from ``app.state``:
  * ``session_factory`` (sqlalchemy async_sessionmaker)
  * ``http_client`` (httpx.AsyncClient)
  * ``fetch_interval_seconds``, ``fetch_user_agent``, ``fetch_concurrency``
  * ``fetch_claim_batch_size``, ``fetch_claim_ttl_seconds``
  * full fetcher tunables (broken_threshold, dead_duration_days, ...)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.http import fetch_one
from feedgate.models import FeedStatus
from feedgate.models import Feed

logger = logging.getLogger(__name__)


def _host_key(url: str) -> str:
    """Extract a per-host throttle key from a feed URL.

    Returns the lowercased hostname so ``Example.com`` and
    ``example.com`` share a semaphore. Falls back to the empty string
    when the URL has no hostname (literal IP feeds keep their address
    as the key, which is the desired behavior — they still throttle
    against themselves)."""
    return (urlsplit(url).hostname or "").lower()


async def _process_feed(
    feed_id: int,
    app: FastAPI,
    sem: asyncio.Semaphore,
    host_sems: dict[str, asyncio.Semaphore],
    per_host_concurrency: int,
    now: datetime,
) -> None:
    """Open a fresh session, load the feed, run fetch_one, commit."""
    sf = app.state.session_factory
    http_client = app.state.http_client
    interval = app.state.fetch_interval_seconds
    ua = app.state.fetch_user_agent
    max_bytes = app.state.fetch_max_bytes
    max_entries_per_fetch = app.state.fetch_max_entries_per_fetch
    max_entries_initial = app.state.fetch_max_entries_initial
    total_budget = app.state.fetch_total_budget_seconds
    broken_threshold = app.state.broken_threshold
    dead_duration_days = app.state.dead_duration_days
    broken_max_backoff_seconds = app.state.broken_max_backoff_seconds
    backoff_jitter_ratio = app.state.backoff_jitter_ratio
    entry_frequency_min_interval_seconds = app.state.entry_frequency_min_interval_seconds
    entry_frequency_max_interval_seconds = app.state.entry_frequency_max_interval_seconds
    entry_frequency_factor = app.state.entry_frequency_factor

    async with sem, sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
        if feed is None:
            return
        # Per-host throttle: setdefault is atomic under cooperative
        # asyncio scheduling, so concurrent _process_feed coroutines
        # for the same host see the same semaphore. ``async with`` it
        # *inside* the global semaphore so we hold a global slot while
        # waiting on the host slot — this keeps tick semantics simple
        # (one acquire order, no deadlock potential).
        host_sem = host_sems.setdefault(
            _host_key(feed.effective_url),
            asyncio.Semaphore(per_host_concurrency),
        )
        try:
            async with host_sem:
                await fetch_one(
                    session,
                    http_client,
                    feed,
                    now=now,
                    interval_seconds=interval,
                    user_agent=ua,
                    max_bytes=max_bytes,
                    max_entries_per_fetch=max_entries_per_fetch,
                    max_entries_initial=max_entries_initial,
                    total_budget_seconds=total_budget,
                    broken_threshold=broken_threshold,
                    dead_duration_days=dead_duration_days,
                    broken_max_backoff_seconds=broken_max_backoff_seconds,
                    backoff_jitter_ratio=backoff_jitter_ratio,
                    entry_frequency_min_interval_seconds=entry_frequency_min_interval_seconds,
                    entry_frequency_max_interval_seconds=entry_frequency_max_interval_seconds,
                    entry_frequency_factor=entry_frequency_factor,
                )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("fatal error in _process_feed feed_id=%s", feed_id)


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


async def tick_once(app: FastAPI, *, now: datetime | None = None) -> None:
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

    if not feed_ids:
        return

    sem = asyncio.Semaphore(app.state.fetch_concurrency)
    host_sems: dict[str, asyncio.Semaphore] = {}
    per_host_concurrency = app.state.fetch_per_host_concurrency
    await asyncio.gather(
        *(_process_feed(fid, app, sem, host_sems, per_host_concurrency, now) for fid in feed_ids)
    )


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
