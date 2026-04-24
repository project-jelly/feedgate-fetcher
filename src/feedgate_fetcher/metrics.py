"""Prometheus metrics — RED + USE + state gauges."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from feedgate_fetcher.models import Entry, Feed, FeedStatus

logger = structlog.get_logger()

# ── RED: fetch pipeline ──────────────────────────────────────────────────────
FETCH_TOTAL = Counter(
    "feedgate_fetch_total",
    "Feed fetch attempts by result",
    ["result"],  # success | not_modified | rate_limited | error
)
FETCH_ERROR_TOTAL = Counter(
    "feedgate_fetch_error_total",
    "Feed fetch errors by error code",
    ["error_code"],
)
FETCH_DURATION = Histogram(
    "feedgate_fetch_duration_seconds",
    "Time spent on a single feed fetch",
    ["result"],
    buckets=[0.5, 1, 2, 5, 10, 15, 20, 30],
)

# ── RED: API ─────────────────────────────────────────────────────────────────
API_REQUESTS_TOTAL = Counter(
    "feedgate_api_requests_total",
    "HTTP API requests by method, path template, and status code",
    ["method", "path", "status_code"],
)
API_DURATION = Histogram(
    "feedgate_api_request_duration_seconds",
    "HTTP API request duration",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

# ── USE: scheduler saturation ────────────────────────────────────────────────
SCHEDULER_DUE_FEEDS = Gauge(
    "feedgate_scheduler_due_feeds",
    "Feeds currently due for fetching (queue depth)",
)

# ── USE: DB connection pool ──────────────────────────────────────────────────
DB_POOL_CHECKEDOUT = Gauge(
    "feedgate_db_pool_checkedout",
    "DB connections currently checked out (in use)",
)
DB_POOL_OVERFLOW = Gauge(
    "feedgate_db_pool_overflow",
    "DB connections using overflow capacity",
)

# ── State gauges (background collector) ──────────────────────────────────────
FEEDS_BY_STATUS = Gauge(
    "feedgate_feeds_total",
    "Number of feeds by lifecycle status",
    ["status"],
)
ENTRIES_TOTAL = Gauge(
    "feedgate_entries_total",
    "Total number of stored entries",
)

# ── Retention ────────────────────────────────────────────────────────────────
RETENTION_DELETED_TOTAL = Counter(
    "feedgate_retention_deleted_total",
    "Entries deleted by retention sweep",
)


async def _collect_state(
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    """Query DB for state gauges and update pool gauges."""
    async with session_factory() as session:
        for status in (FeedStatus.ACTIVE, FeedStatus.BROKEN, FeedStatus.DEAD):
            count = (
                await session.execute(select(func.count()).where(Feed.status == status))
            ).scalar_one()
            FEEDS_BY_STATUS.labels(status=status.value).set(count)

        entry_count = (
            await session.execute(select(func.count()).select_from(Entry))
        ).scalar_one()
        ENTRIES_TOTAL.set(entry_count)

        now = datetime.now(UTC)
        due_count = (
            await session.execute(
                select(func.count()).where(
                    Feed.status != FeedStatus.DEAD,
                    Feed.next_fetch_at <= now,
                )
            )
        ).scalar_one()
        SCHEDULER_DUE_FEEDS.set(due_count)

    pool = engine.pool
    checkedout = pool.checkedout()  # type: ignore[attr-defined]
    DB_POOL_CHECKEDOUT.set(checkedout)
    DB_POOL_OVERFLOW.set(max(0, checkedout - pool.size()))  # type: ignore[attr-defined]


async def run_collector(
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    *,
    interval_seconds: int = 15,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that refreshes state gauges every interval_seconds."""
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await _collect_state(session_factory, engine)
        except Exception:
            logger.debug("metrics collection failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
