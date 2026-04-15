"""/v1/feeds endpoints — POST, GET (list + single), DELETE.

Covers plan WPs 3.1 (POST), 3.2 (POST idempotency), 3.3 (GET list),
3.4 (GET single), 3.5 (DELETE cascade).
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.models import Feed


@pytest.mark.asyncio
async def test_post_feed_creates_row_with_full_lifecycle_fields(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.post(
        "/v1/feeds",
        json={"url": "http://example.com/feed.xml"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["id"] > 0
    assert body["url"] == "http://example.com/feed.xml"
    assert body["effective_url"] == "http://example.com/feed.xml"
    assert body["title"] is None
    assert body["status"] == "active"
    assert body["last_successful_fetch_at"] is None
    assert body["last_attempt_at"] is None
    assert body["last_error_code"] is None
    assert "created_at" in body


@pytest.mark.asyncio
async def test_post_feed_normalizes_url(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/v1/feeds",
        json={"url": "HTTP://Example.COM:80/feed.xml#foo"},
    )
    assert resp.status_code == 201
    assert resp.json()["url"] == "http://example.com/feed.xml"


@pytest.mark.asyncio
async def test_post_feed_is_idempotent(api_client: AsyncClient) -> None:
    first = await api_client.post(
        "/v1/feeds",
        json={"url": "http://example.com/feed.xml"},
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    # Second POST with the same URL should return the same row with 200.
    second = await api_client.post(
        "/v1/feeds",
        json={"url": "http://example.com/feed.xml"},
    )
    assert second.status_code == 200
    assert second.json()["id"] == first_id


@pytest.mark.asyncio
async def test_create_feed_concurrent_idempotent(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    url = "http://concurrent.test/feed.xml"
    responses = await asyncio.gather(
        *[api_client.post("/v1/feeds", json={"url": url}) for _ in range(5)]
    )

    statuses = [resp.status_code for resp in responses]
    assert all(code in {200, 201} for code in statuses), statuses
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) >= 4, statuses

    ids = [resp.json()["id"] for resp in responses]
    assert len(set(ids)) == 1, ids

    async with async_session_factory() as session:
        feeds = (await session.execute(select(Feed).where(Feed.url == url))).scalars().all()
    assert len(feeds) == 1


@pytest.mark.asyncio
async def test_get_feed_list_returns_created_feeds(
    api_client: AsyncClient,
) -> None:
    await api_client.post("/v1/feeds", json={"url": "http://a.test/feed"})
    await api_client.post("/v1/feeds", json={"url": "http://b.test/feed"})

    resp = await api_client.get("/v1/feeds")
    assert resp.status_code == 200
    body = resp.json()
    urls = {item["url"] for item in body["items"]}
    assert "http://a.test/feed" in urls
    assert "http://b.test/feed" in urls


@pytest.mark.asyncio
async def test_get_feed_single_returns_feed(api_client: AsyncClient) -> None:
    created = await api_client.post("/v1/feeds", json={"url": "http://x.test/feed"})
    feed_id = created.json()["id"]

    resp = await api_client.get(f"/v1/feeds/{feed_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == feed_id
    assert resp.json()["url"] == "http://x.test/feed"


@pytest.mark.asyncio
async def test_get_feed_missing_returns_404(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/feeds/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_feed_removes_it(api_client: AsyncClient) -> None:
    created = await api_client.post("/v1/feeds", json={"url": "http://delete.test/feed"})
    feed_id = created.json()["id"]

    delete_resp = await api_client.delete(f"/v1/feeds/{feed_id}")
    assert delete_resp.status_code == 204

    follow_up = await api_client.get(f"/v1/feeds/{feed_id}")
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_feed_is_idempotent(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.delete("/v1/feeds/424242")
    # The plan leaves the exact code for missing IDs to spec; we
    # implement "idempotent delete" → 204.
    assert resp.status_code == 204


# ---- status filter + manual reactivation -----------------------------------


async def _seed_feed(
    sf: async_sessionmaker[AsyncSession],
    *,
    url: str,
    status: str,
    consecutive_failures: int = 0,
    last_error_code: str | None = None,
) -> int:
    async with sf() as session:
        feed = Feed(
            url=url,
            effective_url=url,
            status=status,
            consecutive_failures=consecutive_failures,
            last_error_code=last_error_code,
        )
        session.add(feed)
        await session.commit()
        return feed.id


@pytest.mark.asyncio
async def test_list_feeds_status_filter_returns_only_matching(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_feed(async_session_factory, url="http://f.test/a", status="active")
    await _seed_feed(async_session_factory, url="http://f.test/b", status="broken")
    await _seed_feed(async_session_factory, url="http://f.test/c", status="dead")
    await _seed_feed(async_session_factory, url="http://f.test/d", status="active")

    resp = await api_client.get("/v1/feeds", params={"status": "dead"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["url"] == "http://f.test/c"
    assert items[0]["status"] == "dead"

    resp = await api_client.get("/v1/feeds", params={"status": "active"})
    urls = {item["url"] for item in resp.json()["items"]}
    assert urls == {"http://f.test/a", "http://f.test/d"}


@pytest.mark.asyncio
async def test_list_feeds_invalid_status_returns_422(
    api_client: AsyncClient,
) -> None:
    """Invalid ``?status=`` values are rejected by FastAPI/Pydantic query
    validation and surface as HTTP 422, matching the rest of the API
    (see test_entries_feed_ids_required)."""
    resp = await api_client.get("/v1/feeds", params={"status": "zombie"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reactivate_dead_feed_flips_to_active_and_resets_counters(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _seed_feed(
        async_session_factory,
        url="http://f.test/dead-revive",
        status="dead",
        consecutive_failures=42,
        last_error_code="http_4xx",
    )

    resp = await api_client.post(f"/v1/feeds/{feed_id}/reactivate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == feed_id
    assert body["status"] == "active"
    assert body["last_error_code"] is None

    async with async_session_factory() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
    assert feed.status == "active"
    assert feed.consecutive_failures == 0
    assert feed.last_error_code is None


@pytest.mark.asyncio
async def test_reactivate_broken_feed_also_works(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Manual reactivation is useful for broken feeds too — it
    short-circuits the exponential backoff."""
    feed_id = await _seed_feed(
        async_session_factory,
        url="http://f.test/broken-revive",
        status="broken",
        consecutive_failures=8,
        last_error_code="http_5xx",
    )

    resp = await api_client.post(f"/v1/feeds/{feed_id}/reactivate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    # consecutive_failures is internal (ADR 003, not in FeedResponse),
    # so verify via direct DB read.
    async with async_session_factory() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
    assert feed.consecutive_failures == 0


@pytest.mark.asyncio
async def test_reactivate_missing_feed_returns_404(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.post("/v1/feeds/999999/reactivate")
    assert resp.status_code == 404
