"""FastAPI app factory + lifespan wiring.

``create_app()`` builds a FastAPI instance whose ``app.state`` is fully
populated **synchronously** (so tests over ``ASGITransport`` work
without running the ASGI lifespan at all). The lifespan is used only
for the scheduler background task and end-of-process resource cleanup.

Gated state:
  * ``FEEDGATE_SCHEDULER_ENABLED=false`` disables the background
    scheduler task entirely. Tests set this and drive ticks manually
    via ``scheduler.tick_once(app)`` to avoid racing with the
    background loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from feedgate_fetcher import metrics as _metrics
from feedgate_fetcher.api import register_exception_handlers, register_routers
from feedgate_fetcher.config import get_settings
from feedgate_fetcher.fetcher import retention, scheduler
from feedgate_fetcher.logging_config import configure_logging
from feedgate_fetcher.ssrf import SSRFGuardTransport

logger = logging.getLogger(__name__)


def make_engine(
    database_url: str,
    *,
    pool_size: int = 8,
    max_overflow: int = 4,
    pool_timeout: int = 30,
    pool_recycle: int = 1800,
) -> AsyncEngine:
    """Create an async engine. URL must use the asyncpg driver."""
    return create_async_engine(
        database_url,
        future=True,
        echo=False,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
    )


def make_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def _drain_background_task(
    task: asyncio.Task[None] | None,
    stop_event: asyncio.Event,
    *,
    name: str,
    drain_seconds: float,
) -> None:
    """Cooperatively drain a background loop on lifespan shutdown.

    Sets ``stop_event``, waits up to ``drain_seconds`` for the task to
    finish its current iteration and exit cleanly, and force-cancels
    if the budget runs out. Force-cancel is the *fallback* path — it
    leaves any in-flight ``fetch_one`` claims dangling until the SKIP
    LOCKED lease TTL elapses, so the budget should be sized to avoid
    hitting it on healthy shutdowns.

    Extracted from the lifespan body specifically so unit tests can
    exercise the timeout/force-cancel branch without spinning up a
    full FastAPI app.
    """
    if task is None:
        return
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=drain_seconds)
    except TimeoutError:
        logger.warning(
            "%s task did not drain within %.1fs; force-cancelling",
            name,
            drain_seconds,
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    except Exception:
        logger.exception("%s task raised during drain", name)


def create_app() -> FastAPI:
    """Build a wired-up FastAPI app.

    Engine, session_factory, and ``httpx.AsyncClient`` are created
    eagerly and attached to ``app.state`` so ASGI test clients that
    skip lifespan can still use every route and ``tick_once``. The
    lifespan is responsible only for (a) starting the scheduler task
    when enabled and (b) cleaning up resources at shutdown.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    limiter = Limiter(key_func=get_remote_address, default_limits=[settings.api_rate_limit])
    engine = make_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    session_factory = make_session_factory(engine)
    # Wrap the default transport with the SSRF guard so that *every*
    # outbound request — including any redirect httpx follows on its
    # own — re-validates the destination URL. Pre-validation in
    # fetch_one only catches the initial host; the transport guard is
    # what keeps a 302 to ``http://169.254.169.254/`` from leaking out.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.fetch_connect_timeout_seconds,
            read=settings.fetch_read_timeout_seconds,
            write=settings.fetch_write_timeout_seconds,
            pool=settings.fetch_pool_timeout_seconds,
        ),
        transport=SSRFGuardTransport(httpx.AsyncHTTPTransport()),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler_task: asyncio.Task[None] | None = None
        retention_task: asyncio.Task[None] | None = None
        scheduler_stop = asyncio.Event()
        retention_stop = asyncio.Event()
        if settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(scheduler.run(app, stop_event=scheduler_stop))
            logger.info(
                "scheduler started, interval=%ss",
                settings.fetch_interval_seconds,
            )
        else:
            logger.info("scheduler disabled via FEEDGATE_SCHEDULER_ENABLED=false")
        if settings.retention_enabled:
            retention_task = asyncio.create_task(retention.run(app, stop_event=retention_stop))
            logger.info(
                "retention sweeper started, interval=%ss days=%s min_per_feed=%s",
                settings.retention_sweep_interval_seconds,
                settings.retention_days,
                settings.retention_min_per_feed,
            )
        else:
            logger.info("retention disabled via FEEDGATE_RETENTION_ENABLED=false")
        collector_stop = asyncio.Event()
        collector_task = asyncio.create_task(
            _metrics.run_collector(session_factory, engine, stop_event=collector_stop)
        )
        try:
            yield
        finally:
            # Graceful drain: see ``_drain_background_task`` docstring.
            drain_budget = settings.shutdown_drain_seconds
            await _drain_background_task(
                scheduler_task,
                scheduler_stop,
                name="scheduler",
                drain_seconds=drain_budget,
            )
            await _drain_background_task(
                retention_task,
                retention_stop,
                name="retention",
                drain_seconds=drain_budget,
            )
            await _drain_background_task(
                collector_task,
                collector_stop,
                name="metrics_collector",
                drain_seconds=5.0,
            )
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="feedgate-fetcher", lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    app.state.session_factory = session_factory
    app.state.http_client = http_client
    app.state.fetch_interval_seconds = settings.fetch_interval_seconds
    app.state.fetch_user_agent = settings.fetch_user_agent
    app.state.fetch_max_bytes = settings.fetch_max_bytes
    app.state.fetch_total_budget_seconds = settings.fetch_total_budget_seconds
    app.state.fetch_max_entries_initial = settings.fetch_max_entries_initial
    app.state.fetch_max_entries_per_fetch = settings.fetch_max_entries_per_fetch
    app.state.fetch_concurrency = settings.fetch_concurrency
    app.state.fetch_per_host_concurrency = settings.fetch_per_host_concurrency
    app.state.shutdown_drain_seconds = settings.shutdown_drain_seconds
    app.state.fetch_claim_batch_size = settings.fetch_claim_batch_size
    app.state.fetch_claim_ttl_seconds = settings.fetch_claim_ttl_seconds
    app.state.entry_frequency_min_interval_seconds = settings.entry_frequency_min_interval_seconds
    app.state.entry_frequency_max_interval_seconds = settings.entry_frequency_max_interval_seconds
    app.state.entry_frequency_factor = settings.entry_frequency_factor
    app.state.retention_days = settings.retention_days
    app.state.retention_min_per_feed = settings.retention_min_per_feed
    app.state.retention_sweep_interval_seconds = settings.retention_sweep_interval_seconds
    app.state.retention_batch_size = settings.retention_batch_size
    app.state.broken_threshold = settings.broken_threshold
    app.state.dead_duration_days = settings.dead_duration_days
    app.state.broken_max_backoff_seconds = settings.broken_max_backoff_seconds
    app.state.backoff_jitter_ratio = settings.backoff_jitter_ratio
    app.state.dead_probe_interval_days = settings.dead_probe_interval_days
    app.state.api_entries_max_feed_ids = settings.api_entries_max_feed_ids
    app.state.api_entries_default_limit = settings.api_entries_default_limit
    app.state.api_entries_max_limit = settings.api_entries_max_limit
    app.state.api_feeds_max_limit = settings.api_feeds_max_limit
    app.state.api_key = settings.api_key
    register_routers(app)
    register_exception_handlers(app)
    return app


# Run under uvicorn with:  uvicorn feedgate_fetcher.main:create_app --factory
