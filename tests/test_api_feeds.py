"""/v1/feeds endpoints — POST, GET (list + single), DELETE.

Covers plan WPs 3.1 (POST), 3.2 (POST idempotency), 3.3 (GET list),
3.4 (GET single), 3.5 (DELETE cascade).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


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
