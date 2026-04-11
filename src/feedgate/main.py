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

from feedgate.api import register_routers
from feedgate.config import get_settings
from feedgate.db import make_engine, make_session_factory
from feedgate.fetcher import scheduler

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4


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
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.fetch_timeout_seconds),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler_task: asyncio.Task[None] | None = None
        if settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(scheduler.run(app))
            logger.info(
                "scheduler started, interval=%ss",
                settings.fetch_interval_seconds,
            )
        else:
            logger.info("scheduler disabled via FEEDGATE_SCHEDULER_ENABLED=false")
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await scheduler_task
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="feedgate-fetcher", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.http_client = http_client
    app.state.fetch_interval_seconds = settings.fetch_interval_seconds
    app.state.fetch_user_agent = settings.fetch_user_agent
    app.state.fetch_concurrency = DEFAULT_CONCURRENCY
    register_routers(app)
    return app


# Run under uvicorn with:  uvicorn feedgate.main:create_app --factory
