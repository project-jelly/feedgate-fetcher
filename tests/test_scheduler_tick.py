"""scheduler.tick_once integration test (Phase 4 WP 4.2).

Seeds multiple active feeds, mocks their URLs, runs a single tick,
and verifies every feed got fetched and its entries stored.
"""

from __future__ import annotations

import pytest
import respx
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.fetcher import scheduler
from feedgate.models import Entry, Feed


def _atom_with(guid: str, title: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed for {title}</title>
  <id>http://t.test/{guid}</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>{title}</title>
    <id>{guid}</id>
    <link href="{guid}"/>
    <published>2026-04-10T00:00:00Z</published>
    <content>body of {title}</content>
  </entry>
</feed>
""".encode()


@pytest.mark.asyncio
async def test_tick_once_fetches_all_active_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory

    # Seed three active feeds and one "inactive" feed that should be skipped.
    feed_urls = [
        "http://t.test/a/feed",
        "http://t.test/b/feed",
        "http://t.test/c/feed",
    ]
    inactive_url = "http://t.test/x/feed"

    async with sf() as session:
        for u in feed_urls:
            session.add(Feed(url=u, effective_url=u))
        session.add(
            Feed(
                url=inactive_url,
                effective_url=inactive_url,
                status="dead",
            )
        )
        await session.commit()

    # Mock the three active feed URLs
    for url in feed_urls:
        guid = url + "/post-1"
        respx_mock.get(url).mock(
            return_value=Response(
                200,
                content=_atom_with(guid, f"post for {url}"),
                headers={"Content-Type": "application/atom+xml"},
            )
        )
    # Inactive URL should never be called — don't mock it.

    await scheduler.tick_once(fetch_app)

    # All three active feeds should have 1 entry each.
    async with sf() as session:
        entry_count_total = int(
            (await session.execute(select(func.count()).select_from(Entry))).scalar_one()
        )
        assert entry_count_total == 3

        # Each active feed has its last_successful_fetch_at set.
        result = await session.execute(
            select(
                Feed.url,
                Feed.last_successful_fetch_at,
                Feed.status,
                Feed.consecutive_failures,
            )
        )
        by_url = {row.url: row for row in result}

    for u in feed_urls:
        row = by_url[u]
        assert row.last_successful_fetch_at is not None
        assert row.status == "active"
        assert row.consecutive_failures == 0

    inactive = by_url[inactive_url]
    assert inactive.last_successful_fetch_at is None
    assert inactive.status == "dead"


@pytest.mark.asyncio
async def test_tick_once_with_no_active_feeds_is_noop(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    # No feeds in the DB. Should not raise or issue any requests.
    await scheduler.tick_once(fetch_app)


@pytest.mark.asyncio
async def test_tick_once_continues_when_one_feed_fails(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    ok_url = "http://t.test/ok/feed"
    bad_url = "http://t.test/bad/feed"

    async with sf() as session:
        session.add(Feed(url=ok_url, effective_url=ok_url))
        session.add(Feed(url=bad_url, effective_url=bad_url))
        await session.commit()

    respx_mock.get(ok_url).mock(
        return_value=Response(
            200,
            content=_atom_with(ok_url + "/1", "ok post"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )
    respx_mock.get(bad_url).mock(return_value=Response(500))

    await scheduler.tick_once(fetch_app)

    async with sf() as session:
        ok = (await session.execute(select(Feed).where(Feed.url == ok_url))).scalar_one()
        bad = (await session.execute(select(Feed).where(Feed.url == bad_url))).scalar_one()

    assert ok.last_successful_fetch_at is not None
    assert ok.last_error_code is None
    assert ok.consecutive_failures == 0

    assert bad.last_successful_fetch_at is None
    assert bad.last_error_code == "http_5xx"
    assert bad.consecutive_failures == 1
