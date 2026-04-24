"""Tests for optional API key authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate_fetcher.api import register_routers
from feedgate_fetcher.config import Settings


@pytest_asyncio.fixture
async def auth_app(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """App with api_key configured."""
    settings = Settings()
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.api_key = "test-secret"
    app.state.api_entries_max_feed_ids = settings.api_entries_max_feed_ids
    app.state.api_entries_default_limit = settings.api_entries_default_limit
    app.state.api_entries_max_limit = settings.api_entries_max_limit
    app.state.api_feeds_max_limit = settings.api_feeds_max_limit
    register_routers(app)
    return app


@pytest_asyncio.fixture
async def auth_client(
    auth_app: FastAPI,
    truncate_tables: None,
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=auth_app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_missing_key_returns_401(auth_client: AsyncClient) -> None:
    """POST /v1/feeds without X-Api-Key must return 401."""
    response = await auth_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_wrong_key_returns_401(auth_client: AsyncClient) -> None:
    """POST /v1/feeds with a wrong X-Api-Key must return 401."""
    response = await auth_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
        headers={"X-Api-Key": "wrong-key"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_correct_key_passes(auth_client: AsyncClient) -> None:
    """POST /v1/feeds with the correct X-Api-Key must not return 401."""
    response = await auth_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
        headers={"X-Api-Key": "test-secret"},
    )
    assert response.status_code != 401


@pytest.mark.asyncio
async def test_health_no_key_required(auth_app: FastAPI) -> None:
    """GET /health must return 200 even when auth is enabled and no key is sent."""
    async with AsyncClient(
        transport=ASGITransport(app=auth_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_auth_when_key_empty(api_client: AsyncClient) -> None:
    """When api_key is empty, /v1/feeds works without any key header."""
    response = await api_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
    )
    # 201 Created or 409 Conflict are both fine — anything except 401
    assert response.status_code != 401
