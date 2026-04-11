"""scheduler.tick_once integration test (Phase 4 WP 4.2).

Seeds multiple active feeds, mocks their URLs, runs a single tick,
and verifies every feed got fetched and its entries stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


@pytest.mark.asyncio
async def test_tick_once_skips_non_due_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Non-dead feeds whose ``next_fetch_at`` is in the future must
    be skipped so that the exponential backoff on broken feeds is
    actually honored."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    due_url = "http://t.test/due/feed"
    future_url = "http://t.test/future/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        session.add(
            Feed(
                url=due_url,
                effective_url=due_url,
                next_fetch_at=now - timedelta(seconds=5),  # due
            )
        )
        session.add(
            Feed(
                url=future_url,
                effective_url=future_url,
                next_fetch_at=now + timedelta(hours=1),  # not due
            )
        )
        await session.commit()

    # Only the due URL is mocked — if tick_once wrongly tried the
    # future feed, respx would raise unmatched-request.
    respx_mock.get(due_url).mock(
        return_value=Response(
            200,
            content=_atom_with(due_url + "/1", "due post"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        due_feed = (await session.execute(select(Feed).where(Feed.url == due_url))).scalar_one()
        future_feed = (
            await session.execute(select(Feed).where(Feed.url == future_url))
        ).scalar_one()

    # Due feed was fetched
    assert due_feed.last_successful_fetch_at is not None
    # Future feed was NOT fetched — last_attempt_at still untouched
    assert future_feed.last_attempt_at is None


@pytest.mark.asyncio
async def test_tick_once_probes_stale_dead_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Dead feeds whose ``last_attempt_at`` is older than the probe
    interval must be fetched. A successful probe returns the feed
    to ``active`` via fetch_one's success path."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    dead_url = "http://t.test/stale-dead/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    stale_attempt = now - timedelta(days=8)  # > 7 day probe interval

    async with sf() as session:
        session.add(
            Feed(
                url=dead_url,
                effective_url=dead_url,
                status="dead",
                last_attempt_at=stale_attempt,
                last_error_code="http_4xx",
                consecutive_failures=50,
            )
        )
        await session.commit()

    respx_mock.get(dead_url).mock(
        return_value=Response(
            200,
            content=_atom_with(dead_url + "/1", "revived"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        revived = (await session.execute(select(Feed).where(Feed.url == dead_url))).scalar_one()

    assert revived.status == "active"  # probe succeeded -> resurrection
    assert revived.consecutive_failures == 0
    assert revived.last_error_code is None
    assert revived.last_successful_fetch_at is not None


@pytest.mark.asyncio
async def test_tick_once_skips_recently_probed_dead_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Dead feeds whose ``last_attempt_at`` is WITHIN the probe
    interval must be skipped entirely. No request should be issued."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    dead_url = "http://t.test/fresh-dead/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    fresh_attempt = now - timedelta(hours=12)  # well under 7 days

    async with sf() as session:
        session.add(
            Feed(
                url=dead_url,
                effective_url=dead_url,
                status="dead",
                last_attempt_at=fresh_attempt,
                last_error_code="http_410",
                consecutive_failures=1,
            )
        )
        await session.commit()

    # Deliberately no mock — if tick_once hit this URL, respx would
    # raise unmatched-request. The assertion is "tick returns cleanly
    # and the feed state is untouched".
    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        still_dead = (await session.execute(select(Feed).where(Feed.url == dead_url))).scalar_one()

    assert still_dead.status == "dead"
    assert still_dead.last_attempt_at == fresh_attempt  # untouched
    assert still_dead.last_error_code == "http_410"
