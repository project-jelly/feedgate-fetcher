"""/v1/entries endpoint — keyset pagination, feed_ids filter, bounds.

Covers plan WP 3.6.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.models import Entry, Feed


@pytest_asyncio.fixture
async def seeded_feed(
    api_app: FastAPI,
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> int:
    """Create a feed with 5 entries and return its id.

    Published timestamps are 1 hour apart, most-recent first in the
    desired keyset order.
    """
    async with async_session_factory() as session:
        feed = Feed(url="http://seed.test/feed", effective_url="http://seed.test/feed")
        session.add(feed)
        await session.flush()
        feed_id = feed.id

        base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        for i in range(5):
            session.add(
                Entry(
                    feed_id=feed_id,
                    guid=f"guid-{i}",
                    url=f"http://seed.test/posts/{i}",
                    title=f"Post {i}",
                    content=f"Content {i}",
                    author="seed",
                    published_at=base + timedelta(hours=i),
                    fetched_at=base,
                    content_updated_at=base,
                )
            )
        await session.commit()
        return feed_id


@pytest.mark.asyncio
async def test_list_entries_requires_feed_ids(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/entries")
    # Missing required query param -> 422 from FastAPI validation
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_entries_returns_sorted_desc_by_published_at(
    api_client: AsyncClient, seeded_feed: int
) -> None:
    resp = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed), "limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 5
    # published_at DESC ordering -> guid-4 first, guid-0 last
    guids = [e["guid"] for e in body["items"]]
    assert guids == [f"guid-{i}" for i in (4, 3, 2, 1, 0)]
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_entries_keyset_cursor_advances(
    api_client: AsyncClient, seeded_feed: int
) -> None:
    first = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed), "limit": 2})
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1["items"]) == 2
    assert [e["guid"] for e in page1["items"]] == ["guid-4", "guid-3"]
    assert page1["next_cursor"] is not None

    second = await api_client.get(
        "/v1/entries",
        params={
            "feed_ids": str(seeded_feed),
            "limit": 2,
            "cursor": page1["next_cursor"],
        },
    )
    assert second.status_code == 200
    page2 = second.json()
    assert [e["guid"] for e in page2["items"]] == ["guid-2", "guid-1"]
    # Final page should have 1 item
    third = await api_client.get(
        "/v1/entries",
        params={
            "feed_ids": str(seeded_feed),
            "limit": 2,
            "cursor": page2["next_cursor"],
        },
    )
    assert third.status_code == 200
    page3 = third.json()
    assert [e["guid"] for e in page3["items"]] == ["guid-0"]
    assert page3["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_entries_response_fields(api_client: AsyncClient, seeded_feed: int) -> None:
    resp = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed), "limit": 1})
    assert resp.status_code == 200
    entry = resp.json()["items"][0]
    for field in (
        "id",
        "guid",
        "feed_id",
        "url",
        "title",
        "content",
        "author",
        "published_at",
        "fetched_at",
        "content_updated_at",
    ):
        assert field in entry, f"missing {field}"


@pytest.mark.asyncio
async def test_list_entries_invalid_cursor_returns_400(
    api_client: AsyncClient, seeded_feed: int
) -> None:
    resp = await api_client.get(
        "/v1/entries",
        params={"feed_ids": str(seeded_feed), "cursor": "not-valid-base64!"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_entries_invalid_feed_ids_returns_400(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.get("/v1/entries", params={"feed_ids": "not,a,number"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_entries_cursor_walks_null_to_nonnull_region(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> None:
    async with async_session_factory() as session:
        feed = Feed(url="http://cursor.test/feed", effective_url="http://cursor.test/feed")
        session.add(feed)
        await session.flush()
        feed_id = feed.id

        expected_ids: list[int] = []

        for i in range(3):
            entry = Entry(
                feed_id=feed_id,
                guid=f"null-guid-{i}",
                url=f"http://cursor.test/null/{i}",
                title=f"Null {i}",
                content=f"Null content {i}",
                author="seed",
                published_at=None,
                fetched_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
                content_updated_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
            )
            session.add(entry)
            await session.flush()
            expected_ids.append(entry.id)

        for i, pub in enumerate(
            (
                datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 2, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 3, 0, 0, 0, tzinfo=UTC),
            )
        ):
            entry = Entry(
                feed_id=feed_id,
                guid=f"dated-guid-{i}",
                url=f"http://cursor.test/dated/{i}",
                title=f"Dated {i}",
                content=f"Dated content {i}",
                author="seed",
                published_at=pub,
                fetched_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
                content_updated_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
            )
            session.add(entry)
            await session.flush()
            expected_ids.append(entry.id)

        await session.commit()

    collected_ids: list[int] = []
    cursor: str | None = None

    while True:
        params: dict[str, str | int] = {"feed_ids": str(feed_id), "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await api_client.get("/v1/entries", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        collected_ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert Counter(collected_ids) == Counter(expected_ids)
    assert len(collected_ids) == 6


@pytest.mark.asyncio
async def test_delete_feed_cascades_entries(api_client: AsyncClient, seeded_feed: int) -> None:
    # Delete the seeded feed
    delete_resp = await api_client.delete(f"/v1/feeds/{seeded_feed}")
    assert delete_resp.status_code == 204

    resp = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed)})
    assert resp.status_code == 200
    assert resp.json()["items"] == []
