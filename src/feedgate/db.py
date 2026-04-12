"""Database engine and session factory.

Walking skeleton: minimal async engine + session_factory plumbing. Used by
FastAPI lifespan (to create at startup, dispose at shutdown), by tests
(fixtures in conftest.py), and by the fetcher (to open per-tick sessions).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session, commit on success, rollback on exception."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
