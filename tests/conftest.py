"""Shared pytest fixtures for feedgate-fetcher tests.

- `pg_container` — a Postgres 16 container booted once per test session
- `database_url` — an asyncpg-flavored connection string for the container
- `async_engine` — an `AsyncEngine` over `database_url` (session scope)
- `async_session_factory` — an `async_sessionmaker[AsyncSession]` (session scope)
- `async_session` — a per-test `AsyncSession` wrapped in a SAVEPOINT
  (isolation via rollback)

The `respx_mock` fixture is provided automatically by the `respx` package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from testcontainers.postgres import PostgresContainer

from feedgate.db import make_engine, make_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    """Boot a Postgres 16 container once per test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def database_url(pg_container: PostgresContainer) -> str:
    """asyncpg-flavored connection URL for the session's Postgres."""
    raw = pg_container.get_connection_url()
    # testcontainers gives us a psycopg-flavored URL; rewrite for asyncpg.
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session")
def apply_migrations(database_url: str) -> str:
    """Run Alembic `upgrade head` once per session.

    Returns the database URL so downstream fixtures can depend on this
    fixture to ensure migrations have been applied before they touch
    the schema.
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    return database_url


@pytest_asyncio.fixture(scope="session")
async def async_engine(apply_migrations: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(apply_migrations)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def async_session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(async_engine)


@pytest_asyncio.fixture
async def async_session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Per-test session. Rolls back at the end for isolation."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
