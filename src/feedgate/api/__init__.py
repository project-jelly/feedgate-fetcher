"""FastAPI HTTP API.

Router registration and the per-request DB session dependency live
here. The router modules themselves (feeds, entries, health) define
the actual endpoint handlers.

The session factory is read from ``app.state.session_factory``, which
callers (``main.py`` lifespan for production, the test fixtures for
pytest) must set before the app handles any request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a request-scoped AsyncSession."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def register_routers(app: FastAPI) -> None:
    """Mount all routers onto ``app``."""
    from feedgate.api import entries, feeds, health

    app.include_router(health.router)
    app.include_router(feeds.router)
    app.include_router(entries.router)
