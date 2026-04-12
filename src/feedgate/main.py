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

from feedgate import retention
from feedgate.api import register_routers
from feedgate.config import get_settings
from feedgate.db import make_engine, make_session_factory
from feedgate.fetcher import scheduler
from feedgate.ssrf import SSRFGuardTransport

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build a wired-up FastAPI app.

    Engine, session_factory, and ``httpx.AsyncClient`` are created
    eagerly and attached to ``app.state`` so ASGI test clients that
    skip lifespan can still use every route and ``tick_once``. The
    lifespan is responsible only for (a) starting the scheduler task
    when enabled and (b) cleaning up resources at shutdown.
    """
    settings = get_settings()
    engine = make_engine(settings.database_url)
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
        try:
            yield
        finally:
            # Graceful drain: signal each background task to stop and
            # give it ``shutdown_drain_seconds`` to finish its current
            # iteration. Tasks that overrun get force-cancelled — that
            # path leaves feeds in claimed-but-not-committed state and
            # relies on the SKIP LOCKED lease TTL to release them, so
            # the drain budget should always be the preferred exit.
            drain_budget = settings.shutdown_drain_seconds
            for task, stop, name in (
                (scheduler_task, scheduler_stop, "scheduler"),
                (retention_task, retention_stop, "retention"),
            ):
                if task is None:
                    continue
                stop.set()
                try:
                    await asyncio.wait_for(task, timeout=drain_budget)
                except TimeoutError:
                    logger.warning(
                        "%s task did not drain within %.1fs; force-cancelling",
                        name,
                        drain_budget,
                    )
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                except Exception:
                    logger.exception("%s task raised during drain", name)
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="feedgate-fetcher", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.http_client = http_client
    app.state.fetch_interval_seconds = settings.fetch_interval_seconds
    app.state.fetch_user_agent = settings.fetch_user_agent
    app.state.fetch_max_bytes = settings.fetch_max_bytes
    app.state.fetch_total_budget_seconds = settings.fetch_total_budget_seconds
    app.state.fetch_max_entries_initial = settings.fetch_max_entries_initial
    app.state.fetch_concurrency = settings.fetch_concurrency
    app.state.fetch_per_host_concurrency = settings.fetch_per_host_concurrency
    app.state.shutdown_drain_seconds = settings.shutdown_drain_seconds
    app.state.fetch_claim_batch_size = settings.fetch_claim_batch_size
    app.state.fetch_claim_ttl_seconds = settings.fetch_claim_ttl_seconds
    app.state.retention_days = settings.retention_days
    app.state.retention_min_per_feed = settings.retention_min_per_feed
    app.state.retention_sweep_interval_seconds = settings.retention_sweep_interval_seconds
    app.state.broken_threshold = settings.broken_threshold
    app.state.dead_duration_days = settings.dead_duration_days
    app.state.broken_max_backoff_seconds = settings.broken_max_backoff_seconds
    app.state.backoff_jitter_ratio = settings.backoff_jitter_ratio
    app.state.dead_probe_interval_days = settings.dead_probe_interval_days
    register_routers(app)
    return app


# Run under uvicorn with:  uvicorn feedgate.main:create_app --factory
