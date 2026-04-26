"""FastAPI dependency helpers."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
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
    if not provided_key or not hmac.compare_digest(provided_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_api_key",
        )
