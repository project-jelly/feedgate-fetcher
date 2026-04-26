"""Prometheus metrics — RED + USE + state gauges."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import structlog
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from feedgate_fetcher.models import Entry, ErrorCode, Feed, FeedStatus

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
    buckets=[0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60],
)


def observe_fetch(
    result: str,
    started_at: float,
    *,
    error_code: ErrorCode | None = None,
) -> None:
    """Record FETCH_TOTAL/FETCH_DURATION (and FETCH_ERROR_TOTAL when applicable).

    `started_at` is a value from `time.perf_counter()` at the start of the fetch.
    `error_code` is required when `result == 'error'` (FETCH_ERROR_TOTAL also incs).
    """
    elapsed = time.perf_counter() - started_at
    FETCH_TOTAL.labels(result=result).inc()
    FETCH_DURATION.labels(result=result).observe(elapsed)
    if error_code is not None:
        FETCH_ERROR_TOTAL.labels(error_code=error_code).inc()


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
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5],
)

# ── USE: scheduler saturation ────────────────────────────────────────────────
SCHEDULER_TICK_TOTAL = Counter(
    "feedgate_scheduler_tick_total",
    "Number of scheduler.tick_once invocations by result",
    ["result"],
)
SCHEDULER_TICK_DURATION = Histogram(
    "feedgate_scheduler_tick_duration_seconds",
    "Wall-clock duration of one scheduler tick",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120],
)
SCHEDULER_LAST_TICK_UNIXTIME = Gauge(
    "feedgate_scheduler_last_tick_unixtime",
    "Unix timestamp of the last completed scheduler tick",
)
SCHEDULER_CLAIMED_FEEDS_TOTAL = Counter(
    "feedgate_scheduler_claimed_feeds_total",
    "Cumulative count of feeds claimed by the scheduler (sum of SchedulerTickResult.claimed)",
)
SCHEDULER_INFLIGHT_FETCHES = Gauge(
    "feedgate_scheduler_inflight_fetches",
    "Number of feeds currently being processed by _process_feed",
)
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
FEED_STATE_TRANSITION_TOTAL = Counter(
    "feedgate_feed_state_transition_total",
    "Feed lifecycle state transitions",
    ["from_status", "to_status", "reason"],
)
ENTRIES_TOTAL = Gauge(
    "feedgate_entries_total",
    "Total number of stored entries",
)
ACTIVE_FEED_MAX_AGE_SECONDS = Gauge(
    "feedgate_active_feed_max_age_seconds",
    "Maximum age in seconds of last_successful_fetch_at across active feeds",
)
ACTIVE_FEEDS_STALE_TOTAL = Gauge(
    "feedgate_active_feeds_stale_total",
    "Number of active feeds whose last_successful_fetch_at is older than threshold",
    ["threshold_seconds"],
)
METRICS_COLLECTOR_LAST_SUCCESS_UNIXTIME = Gauge(
    "feedgate_metrics_collector_last_success_unixtime",
    "Unix timestamp of the last successful metrics collection cycle",
)
METRICS_COLLECTOR_ERRORS_TOTAL = Counter(
    "feedgate_metrics_collector_errors_total",
    "Number of failed metrics collection cycles",
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

        entry_count = (await session.execute(select(func.count()).select_from(Entry))).scalar_one()
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

        active_reference = func.coalesce(Feed.last_successful_fetch_at, Feed.created_at)
        oldest_active_reference = (
            await session.execute(
                select(func.min(active_reference)).where(Feed.status == FeedStatus.ACTIVE)
            )
        ).scalar_one()
        if oldest_active_reference is None:
            ACTIVE_FEED_MAX_AGE_SECONDS.set(0)
        else:
            ACTIVE_FEED_MAX_AGE_SECONDS.set(
                max(0.0, (now - oldest_active_reference).total_seconds())
            )

        for threshold_seconds in (1800, 3600, 86400):
            stale_count = (
                await session.execute(
                    select(func.count()).where(
                        Feed.status == FeedStatus.ACTIVE,
                        active_reference < now - timedelta(seconds=threshold_seconds),
                    )
                )
            ).scalar_one()
            ACTIVE_FEEDS_STALE_TOTAL.labels(threshold_seconds=str(threshold_seconds)).set(
                stale_count
            )

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
            METRICS_COLLECTOR_LAST_SUCCESS_UNIXTIME.set_to_current_time()
        except Exception:
            METRICS_COLLECTOR_ERRORS_TOTAL.inc()
            logger.debug("metrics collection failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
