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

from fastapi import Depends, FastAPI, HTTPException, Request, status
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


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that enforces X-Api-Key authentication.

    When ``app.state.api_key`` is empty (the default) auth is disabled
    and every request passes through. When set, the ``x-api-key``
    header must match exactly or the request is rejected with 401.
    """
    configured_key: str = getattr(request.app.state, "api_key", "")
    if not configured_key:
        return  # auth disabled
    provided_key = request.headers.get("x-api-key", "")
    if not provided_key or provided_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_api_key",
        )


def register_routers(app: FastAPI) -> None:
    """Mount all routers onto ``app``."""
    from feedgate.api import entries, feeds, health

    app.include_router(health.router)
    app.include_router(feeds.router, dependencies=[Depends(require_api_key)])
    app.include_router(entries.router, dependencies=[Depends(require_api_key)])
