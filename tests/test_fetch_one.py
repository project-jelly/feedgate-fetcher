"""fetch_one unit tests (Phase 4 WP 4.1).

Happy path and one failure case. respx mocks the transport so no real
HTTP is issued.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.fetcher.http import fetch_one
from feedgate.models import Entry, Feed

ATOM_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Feed</title>
  <id>http://t.test/feed</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>Alpha</title>
    <id>http://t.test/posts/alpha</id>
    <link href="http://t.test/posts/alpha"/>
    <published>2026-04-10T00:00:00Z</published>
    <content>Alpha body</content>
  </entry>
  <entry>
    <title>Beta</title>
    <id>http://t.test/posts/beta</id>
    <link href="http://t.test/posts/beta"/>
    <published>2026-04-10T01:00:00Z</published>
    <content>Beta body</content>
  </entry>
</feed>
"""


async def _create_feed(session_factory: async_sessionmaker[AsyncSession], url: str) -> int:
    async with session_factory() as session:
        feed = Feed(url=url, effective_url=url)
        session.add(feed)
        await session.commit()
        return feed.id


async def _load_feed(
    session_factory: async_sessionmaker[AsyncSession], feed_id: int
) -> dict[str, object]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    Feed.title,
                    Feed.status,
                    Feed.last_successful_fetch_at,
                    Feed.last_attempt_at,
                    Feed.last_error_code,
                    Feed.next_fetch_at,
                    Feed.consecutive_failures,
                ).where(Feed.id == feed_id)
            )
        ).one()
        return {
            "title": row.title,
            "status": row.status,
            "last_successful_fetch_at": row.last_successful_fetch_at,
            "last_attempt_at": row.last_attempt_at,
            "last_error_code": row.last_error_code,
            "next_fetch_at": row.next_fetch_at,
            "consecutive_failures": row.consecutive_failures,
        }


async def _count_entries_for_feed(
    session_factory: async_sessionmaker[AsyncSession], feed_id: int
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_fetch_one_success_stores_entries_and_updates_timers(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/feed"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    interval = 60

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=interval,
            user_agent="test-agent",
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["title"] == "Test Feed"
    assert state["last_successful_fetch_at"] == now
    assert state["last_attempt_at"] == now
    assert state["next_fetch_at"] == now + timedelta(seconds=interval)
    assert state["last_error_code"] is None
    assert state["consecutive_failures"] == 0
    assert state["status"] == "active"

    assert await _count_entries_for_feed(sf, feed_id) == 2


@pytest.mark.asyncio
async def test_fetch_one_http_404_records_error_without_raising(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/dead"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(404))

    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_attempt_at"] == now
    assert state["last_successful_fetch_at"] is None
    assert state["last_error_code"] == "http_4xx"
    assert state["consecutive_failures"] == 1
    assert state["status"] == "active"  # no state machine in walking skeleton
    assert await _count_entries_for_feed(sf, feed_id) == 0


@pytest.mark.asyncio
async def test_fetch_one_second_success_resets_failure_counter(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/flaky"
    feed_id = await _create_feed(sf, feed_url)

    # First attempt fails
    respx_mock.get(feed_url).mock(return_value=Response(500))
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
        )
        await session.commit()

    state1 = await _load_feed(sf, feed_id)
    assert state1["consecutive_failures"] == 1
    assert state1["last_error_code"] == "http_5xx"

    # Second attempt succeeds
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 10, 13, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
        )
        await session.commit()

    state2 = await _load_feed(sf, feed_id)
    assert state2["consecutive_failures"] == 0
    assert state2["last_error_code"] is None
    assert state2["last_successful_fetch_at"] is not None


@pytest.mark.asyncio
async def test_fetch_one_rejects_html_content_type_as_not_a_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """200 OK + ``text/html`` body must be recorded as ``not_a_feed``
    and must not insert any entries, even if the body looks parseable.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/html-cloaking-as-feed"
    feed_id = await _create_feed(sf, feed_url)

    # A Cloudflare WAF page or a 200 OK HTML error page — not a feed.
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=b"<html><body>Attention Required</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "not_a_feed"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1
    assert await _count_entries_for_feed(sf, feed_id) == 0


MANY_ATOM_ENTRIES = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Many entries feed</title>
  <id>http://t.test/many</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry><title>E0</title><id>http://t.test/many/e0</id><link href="http://t.test/many/e0"/><published>2026-04-10T00:00:00Z</published><content>b0</content></entry>
  <entry><title>E1</title><id>http://t.test/many/e1</id><link href="http://t.test/many/e1"/><published>2026-04-10T01:00:00Z</published><content>b1</content></entry>
  <entry><title>E2</title><id>http://t.test/many/e2</id><link href="http://t.test/many/e2"/><published>2026-04-10T02:00:00Z</published><content>b2</content></entry>
  <entry><title>E3</title><id>http://t.test/many/e3</id><link href="http://t.test/many/e3"/><published>2026-04-10T03:00:00Z</published><content>b3</content></entry>
  <entry><title>E4</title><id>http://t.test/many/e4</id><link href="http://t.test/many/e4"/><published>2026-04-10T04:00:00Z</published><content>b4</content></entry>
  <entry><title>E5</title><id>http://t.test/many/e5</id><link href="http://t.test/many/e5"/><published>2026-04-10T05:00:00Z</published><content>b5</content></entry>
</feed>
"""


@pytest.mark.asyncio
async def test_fetch_one_caps_initial_fetch_entries(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Brand-new feed with many entries must be truncated to
    ``max_entries_initial`` on the first fetch."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/many"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=MANY_ATOM_ENTRIES,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            max_entries_initial=3,
        )
        await session.commit()

    assert await _count_entries_for_feed(sf, feed_id) == 3


@pytest.mark.asyncio
async def test_fetch_one_no_cap_on_subsequent_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Once a feed already has some entries, the initial cap must
    NOT apply — later fetches can bring in more entries than the cap."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/many"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=MANY_ATOM_ENTRIES,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    # First fetch with cap=3 → 3 entries persisted
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            max_entries_initial=3,
        )
        await session.commit()

    assert await _count_entries_for_feed(sf, feed_id) == 3

    # Second fetch with the same feed body (6 entries) and the same cap:
    # the cap must NOT apply because existing_count > 0. All 6 entries
    # should end up in the DB (3 existing upserted + 3 new).
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 1, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            max_entries_initial=3,
        )
        await session.commit()

    assert await _count_entries_for_feed(sf, feed_id) == 6


@pytest.mark.asyncio
async def test_fetch_one_rejects_oversized_response_as_too_large(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A response body larger than ``max_bytes`` must be rejected as
    ``too_large`` with no entries persisted and no successful-fetch
    timer advance."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/huge-feed"
    feed_id = await _create_feed(sf, feed_url)

    # 4 KB payload, cap set to 1 KB below.
    oversized = b"<feed>" + (b"x" * 4096) + b"</feed>"
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=oversized,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            max_bytes=1024,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "too_large"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1
    assert await _count_entries_for_feed(sf, feed_id) == 0


@pytest.mark.asyncio
async def test_fetch_one_accepts_blank_content_type(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """No Content-Type header + valid XML must still succeed — many
    real-world feeds ship empty or unusual Content-Type values."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/blank-ct-feed"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(200, content=ATOM_BODY),
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] is None
    assert state["last_successful_fetch_at"] is not None
    assert await _count_entries_for_feed(sf, feed_id) == 2
