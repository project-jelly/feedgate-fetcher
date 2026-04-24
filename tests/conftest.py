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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from testcontainers.postgres import PostgresContainer

from feedgate_fetcher.api import feeds as feeds_api
from feedgate_fetcher.api import register_routers
from feedgate_fetcher.config import Settings
from feedgate_fetcher.main import make_engine, make_session_factory

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
    raw: str = str(pg_container.get_connection_url())
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


@pytest_asyncio.fixture
async def truncate_tables(async_engine: AsyncEngine) -> None:
    """Wipe feeds and entries between API tests.

    The rollback-per-test isolation used by ``async_session`` does not
    work for API tests because FastAPI opens its own per-request
    session via the ``get_session`` dependency and commits it. So for
    any test that hits the HTTP app, truncate before running.
    """
    async with async_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE feeds, entries RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def api_app(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """A FastAPI app wired to the test DB session factory.

    Phase 5 ``main.create_app`` will build a similar object but also
    wire in a lifespan + scheduler. For Phase 3 tests we just need the
    routers and the session dependency; no background task.
    """
    settings = Settings()
    app = FastAPI()
    app.state.limiter = feeds_api.limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    app.state.session_factory = async_session_factory
    app.state.api_key = ""  # no auth in tests by default
    app.state.fetch_interval_seconds = settings.fetch_interval_seconds
    app.state.api_entries_max_feed_ids = settings.api_entries_max_feed_ids
    app.state.api_entries_default_limit = settings.api_entries_default_limit
    app.state.api_entries_max_limit = settings.api_entries_max_limit
    app.state.api_feeds_max_limit = settings.api_feeds_max_limit
    register_routers(app)
    return app


@pytest_asyncio.fixture
async def api_client(
    api_app: FastAPI,
    truncate_tables: None,
) -> AsyncIterator[AsyncClient]:
    """An httpx AsyncClient over ASGITransport into the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=api_app),
        base_url="http://test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def fetch_app(
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> AsyncIterator[FastAPI]:
    """A FastAPI app populated with state that ``scheduler.tick_once`` reads.

    Used by Phase 4 fetcher/scheduler tests. Owns a real
    ``httpx.AsyncClient`` — ``respx_mock`` patches its transport so no
    real HTTP happens. The scheduler itself is NOT started; tests call
    ``tick_once`` directly.
    """
    settings = Settings()
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.api_key = ""  # no auth in tests by default
    app.state.http_client = AsyncClient()
    app.state.fetch_interval_seconds = 60
    app.state.fetch_user_agent = "feedgate-fetcher/test"
    app.state.fetch_concurrency = settings.fetch_concurrency
    app.state.fetch_claim_batch_size = settings.fetch_claim_batch_size
    app.state.fetch_claim_ttl_seconds = settings.fetch_claim_ttl_seconds
    app.state.fetch_max_bytes = settings.fetch_max_bytes
    app.state.fetch_max_entries_per_fetch = settings.fetch_max_entries_per_fetch
    app.state.fetch_max_entries_initial = settings.fetch_max_entries_initial
    app.state.fetch_total_budget_seconds = settings.fetch_total_budget_seconds
    app.state.broken_threshold = settings.broken_threshold
    app.state.dead_duration_days = settings.dead_duration_days
    app.state.broken_max_backoff_seconds = settings.broken_max_backoff_seconds
    app.state.backoff_jitter_ratio = settings.backoff_jitter_ratio
    app.state.dead_probe_interval_days = settings.dead_probe_interval_days
    app.state.entry_frequency_min_interval_seconds = settings.entry_frequency_min_interval_seconds
    app.state.entry_frequency_max_interval_seconds = settings.entry_frequency_max_interval_seconds
    app.state.entry_frequency_factor = settings.entry_frequency_factor
    try:
        yield app
    finally:
        await app.state.http_client.aclose()
