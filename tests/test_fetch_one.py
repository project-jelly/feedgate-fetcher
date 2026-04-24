"""fetch_one unit tests (Phase 4 WP 4.1).

Happy path and one failure case. respx mocks the transport so no real
HTTP is issued.
"""

from __future__ import annotations

import gzip
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.config import Settings
from feedgate.fetcher.http import (
    _classify_error,
    _compute_next_fetch_at,
    _parse_retry_after,
    fetch_one,
)
from feedgate.models import ErrorCode
from feedgate.models import Entry, Feed

_TEST_SETTINGS = Settings()
_FETCH_DEFAULTS: dict[str, Any] = {
    "max_bytes": _TEST_SETTINGS.fetch_max_bytes,
    "max_entries_per_fetch": _TEST_SETTINGS.fetch_max_entries_per_fetch,
    "max_entries_initial": _TEST_SETTINGS.fetch_max_entries_initial,
    "total_budget_seconds": _TEST_SETTINGS.fetch_total_budget_seconds,
    "broken_threshold": _TEST_SETTINGS.broken_threshold,
    "dead_duration_days": _TEST_SETTINGS.dead_duration_days,
    "broken_max_backoff_seconds": _TEST_SETTINGS.broken_max_backoff_seconds,
    "backoff_jitter_ratio": _TEST_SETTINGS.backoff_jitter_ratio,
    "entry_frequency_min_interval_seconds": 300,
    "entry_frequency_max_interval_seconds": 86400,
    "entry_frequency_factor": 1,
}


def _kwargs(**overrides: Any) -> dict[str, Any]:
    return {**_FETCH_DEFAULTS, **overrides}


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
) -> dict[str, Any]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    Feed.url,
                    Feed.effective_url,
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
            "url": row.url,
            "effective_url": row.effective_url,
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
            **_FETCH_DEFAULTS,
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
async def test_fetch_one_301_updates_effective_url(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    old_url = "http://old.test/feed"
    new_url = "http://new.test/feed"
    feed_id = await _create_feed(sf, old_url)

    respx_mock.get(old_url).mock(return_value=Response(301, headers={"Location": new_url}))
    respx_mock.get(new_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    now = datetime(2026, 4, 10, 12, 30, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["effective_url"] == new_url
    assert state["url"] == old_url
    assert state["last_successful_fetch_at"] is not None
    assert state["last_error_code"] is None


@pytest.mark.asyncio
async def test_fetch_one_302_does_not_update_effective_url(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    original_url = "http://tmp.test/feed"
    detour_url = "http://detour.test/feed"
    feed_id = await _create_feed(sf, original_url)

    respx_mock.get(original_url).mock(return_value=Response(302, headers={"Location": detour_url}))
    respx_mock.get(detour_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    now = datetime(2026, 4, 10, 12, 40, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["effective_url"] == original_url
    assert state["url"] == original_url
    assert state["last_successful_fetch_at"] is not None


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
            **_FETCH_DEFAULTS,
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
            **_FETCH_DEFAULTS,
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
            **_FETCH_DEFAULTS,
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
            **_FETCH_DEFAULTS,
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
            **_kwargs(max_entries_initial=3),
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
            **_kwargs(max_entries_initial=3),
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
            **_kwargs(max_entries_initial=3),
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
            **_kwargs(max_bytes=1024),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "too_large"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1
    assert await _count_entries_for_feed(sf, feed_id) == 0


@pytest.mark.asyncio
async def test_fetch_one_compression_bomb_blocked(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A tiny gzip body that inflates past ``max_bytes`` must still
    be rejected as ``too_large`` because httpx auto-decompresses."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/gzip-bomb-feed"
    feed_id = await _create_feed(sf, feed_url)

    compressed = gzip.compress(b"x" * 1025)  # decompressed > 1024 cap
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=compressed,
            headers={
                "Content-Type": "application/atom+xml",
                "Content-Encoding": "gzip",
            },
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
            **_kwargs(max_bytes=1024),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == ErrorCode.TOO_LARGE
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1
    assert await _count_entries_for_feed(sf, feed_id) == 0


@pytest.mark.asyncio
async def test_fetch_one_active_to_broken_after_n_failures(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """After ``broken_threshold`` consecutive failures, an active feed
    transitions to ``broken``. Failures below the threshold leave the
    status unchanged."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/flaky-to-broken"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async def _one_call(i: int) -> None:
        async with sf() as session:
            feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
            await fetch_one(
                session,
                fetch_app.state.http_client,
                feed,
                now=datetime(2026, 4, 11, 0, i, 0, tzinfo=UTC),
                interval_seconds=60,
                user_agent="test-agent",
                **_kwargs(broken_threshold=3),
            )
            await session.commit()

    for i in (1, 2):
        await _one_call(i)
        state = await _load_feed(sf, feed_id)
        assert state["status"] == "active", f"call {i}: {state}"
        assert state["consecutive_failures"] == i

    await _one_call(3)
    state = await _load_feed(sf, feed_id)
    assert state["status"] == "broken"
    assert state["consecutive_failures"] == 3

    await _one_call(4)
    state = await _load_feed(sf, feed_id)
    assert state["status"] == "broken"
    assert state["consecutive_failures"] == 4


@pytest.mark.asyncio
async def test_fetch_one_broken_to_active_on_success(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A successful fetch on a broken feed flips it back to active and
    resets consecutive_failures to 0."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/recovered"
    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=7,
            last_error_code="http_5xx",
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
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
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "active"
    assert state["consecutive_failures"] == 0
    assert state["last_error_code"] is None
    assert state["last_successful_fetch_at"] is not None


@pytest.mark.asyncio
async def test_fetch_one_broken_to_dead_after_duration_since_last_success(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A broken feed whose ``last_successful_fetch_at`` is older than
    ``dead_duration_days`` transitions to ``dead`` on the next failure."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/long-broken"

    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    last_success = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)  # 8 days ago

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=20,
            last_error_code="http_5xx",
            last_successful_fetch_at=last_success,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "dead"


@pytest.mark.asyncio
async def test_fetch_one_broken_to_dead_falls_back_to_created_at(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """If a feed has never had a successful fetch, the dead clock
    counts from ``created_at`` instead of ``last_successful_fetch_at``."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/never-succeeded"

    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    created = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)  # 8 days ago

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=20,
            last_error_code="connection",
            last_successful_fetch_at=None,
            created_at=created,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "dead"


@pytest.mark.asyncio
async def test_fetch_one_broken_stays_broken_within_duration(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A broken feed that is still within the dead_duration window
    must stay ``broken`` — no premature dead transition."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/recent-broken"

    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    last_success = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)  # 2 days ago

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=5,
            last_error_code="http_5xx",
            last_successful_fetch_at=last_success,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "broken"
    assert state["consecutive_failures"] == 6


@pytest.mark.asyncio
async def test_fetch_one_http_410_transitions_to_dead_immediately(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """HTTP 410 Gone is the only explicit permanent HTTP signal. It
    transitions the feed to ``dead`` on the first occurrence, even if
    consecutive_failures is still below the broken threshold."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/gone"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(410))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "dead"
    assert state["last_error_code"] == "http_410"
    assert state["consecutive_failures"] == 1


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
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] is None
    assert state["last_successful_fetch_at"] is not None
    assert await _count_entries_for_feed(sf, feed_id) == 2


# ---- next_fetch_at / exponential backoff (pure function tests) -------------


def _fake_feed(status: str, consecutive_failures: int) -> Feed:
    return Feed(
        url="http://t.test/dummy",
        effective_url="http://t.test/dummy",
        status=status,
        consecutive_failures=consecutive_failures,
    )


@pytest.mark.asyncio
async def test_fetch_one_429_is_not_a_circuit_breaker_failure(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Repeated 429 responses must NOT increment consecutive_failures
    and must NOT transition the feed state. 429 is "slow down", not
    "you are broken". Only ``last_error_code`` is recorded."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/rate-limited"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(429))

    for i in range(5):
        async with sf() as session:
            feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
            await fetch_one(
                session,
                fetch_app.state.http_client,
                feed,
                now=datetime(2026, 4, 11, 0, i, 0, tzinfo=UTC),
                interval_seconds=60,
                user_agent="test-agent",
                **_kwargs(broken_threshold=3),
            )
            await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "active"
    assert state["consecutive_failures"] == 0
    assert state["last_error_code"] == "rate_limited"
    assert state["last_successful_fetch_at"] is None


@pytest.mark.asyncio
async def test_fetch_one_429_honors_retry_after_header(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """When Retry-After is present and >= base_interval, fetch_one
    schedules next_fetch_at at ``now + retry_after``."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/retry-after-300"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(429, headers={"Retry-After": "300"}))

    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    # Retry-After 300s > base_interval 60s → use 300
    assert delta == 300
    assert state["last_error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_fetch_one_429_floors_retry_after_at_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Retry-After values shorter than base_interval must be clamped
    up to base_interval — we never poll faster than our normal
    schedule just because the upstream says "10s"."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/retry-after-small"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(429, headers={"Retry-After": "10"}))

    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    # Retry-After 10 < base_interval 60 → floor at 60
    assert delta == 60


@pytest.mark.asyncio
async def test_fetch_one_429_honors_retry_after_http_date(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cloudflare/GitHub emit Retry-After as an HTTP-date, not a
    delta-seconds integer. fetch_one must convert the absolute date to
    a relative delay against ``now`` and honor it (subject to the
    base-interval floor)."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/retry-after-http-date"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    # 5 minutes in the future, RFC 7231 IMF-fixdate form
    retry_date = "Sat, 11 Apr 2026 00:05:00 GMT"
    respx_mock.get(feed_url).mock(return_value=Response(429, headers={"Retry-After": retry_date}))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert delta == 300
    assert state["last_error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_fetch_one_total_budget_kills_slow_response(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A respx side-effect that sleeps longer than ``total_budget_seconds``
    must trigger ``asyncio.timeout`` inside fetch_one. The feed is
    marked with ``last_error_code = 'timeout'`` and ``consecutive_failures``
    is bumped — that is the load-bearing slow-loris defense.
    """
    import asyncio as _asyncio  # local alias to avoid clashing with re-exports

    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/slow-loris/feed"
    feed_id = await _create_feed(sf, feed_url)

    async def slow_response(request: Any) -> Response:
        # Sleep is several times the budget — guarantees the timeout
        # fires before the body is delivered.
        await _asyncio.sleep(0.5)
        return Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )

    respx_mock.get(feed_url).mock(side_effect=slow_response)

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
            **_kwargs(total_budget_seconds=0.05),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "timeout"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1


def test_parse_retry_after_none_returns_none() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after(None, now=now) is None


def test_classify_error_tls() -> None:
    cause = ssl.SSLError("handshake fail")
    exc = httpx.ConnectError("tls")
    exc.__cause__ = cause
    assert _classify_error(exc) == ErrorCode.TLS_ERROR


def test_classify_error_dns() -> None:
    cause = socket.gaierror(-2, "Name or service not known")
    exc = httpx.ConnectError("dns")
    exc.__cause__ = cause
    assert _classify_error(exc) == ErrorCode.DNS


def test_classify_error_tcp_refused() -> None:
    cause = ConnectionRefusedError()
    exc = httpx.ConnectError("tcp refused")
    exc.__cause__ = cause
    assert _classify_error(exc) == ErrorCode.TCP_REFUSED


def test_classify_error_connect_error_without_known_cause() -> None:
    exc = httpx.ConnectError("generic")
    assert _classify_error(exc) == ErrorCode.CONNECTION


def test_classify_error_too_many_redirects() -> None:
    exc = httpx.TooManyRedirects("redirect loop")
    assert _classify_error(exc) == ErrorCode.REDIRECT_LOOP


def test_classify_error_nested_cause_chain() -> None:
    dns_cause = socket.gaierror(-2, "Name or service not known")
    wrap = OSError("wrap")
    wrap.__cause__ = dns_cause
    exc = httpx.ConnectError("outer")
    exc.__cause__ = wrap
    assert _classify_error(exc) == ErrorCode.DNS


def test_parse_retry_after_integer_seconds() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after("120", now=now) == 120
    # Whitespace allowed around the integer form.
    assert _parse_retry_after("  60 ", now=now) == 60
    # Negative integers clamp to zero.
    assert _parse_retry_after("-10", now=now) == 0


def test_parse_retry_after_http_date_future() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    # IMF-fixdate, 2 minutes in the future
    assert _parse_retry_after("Sat, 11 Apr 2026 00:02:00 GMT", now=now) == 120


def test_parse_retry_after_http_date_past_clamps_to_zero() -> None:
    now = datetime(2026, 4, 11, 0, 10, 0, tzinfo=UTC)
    # Date is 10 minutes earlier than now — must clamp at 0, not go negative
    assert _parse_retry_after("Sat, 11 Apr 2026 00:00:00 GMT", now=now) == 0


def test_parse_retry_after_unparseable_returns_none() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after("not-a-date", now=now) is None
    assert _parse_retry_after("", now=now) is None


def test_compute_next_fetch_at_active_feed_uses_exact_base_interval() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    feed = _fake_feed("active", 1)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    assert result == now + timedelta(seconds=60)


def test_compute_next_fetch_at_broken_at_threshold_boundary_uses_base_with_jitter() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    # Just transitioned: consecutive_failures == threshold, excess = 0
    feed = _fake_feed("broken", 3)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    delta_seconds = (result - now).total_seconds()
    # factor=1, raw=60, jitter in [-15, +15]
    assert 45 <= delta_seconds <= 75


def test_compute_next_fetch_at_broken_feed_exponential_factor() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    # excess = 5 - 3 = 2, factor = 4, raw = 240s, jitter [-60, +60]
    feed = _fake_feed("broken", 5)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    delta_seconds = (result - now).total_seconds()
    assert 180 <= delta_seconds <= 300


def test_compute_next_fetch_at_broken_feed_capped_at_max_backoff() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    # excess = 20 - 3 = 17, factor = 2**17 = 131072, raw ~= 7.8M s,
    # capped at 3600s, jitter [-900, +900]
    feed = _fake_feed("broken", 20)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    delta_seconds = (result - now).total_seconds()
    assert 2700 <= delta_seconds <= 4500


# ---- ETag / If-None-Match + Last-Modified / If-Modified-Since tests ---------


@pytest.mark.asyncio
async def test_fetch_one_304_schedules_next_fetch_without_updating_success_fields(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A 304 Not Modified response must advance next_fetch_at but must NOT
    update last_successful_fetch_at or consecutive_failures."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/conditional-304"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(304))

    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
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
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=interval)
    assert state["last_successful_fetch_at"] == now
    assert state["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_fetch_one_sends_if_none_match_on_second_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """After a 200 that returns ETag, the next fetch must include
    If-None-Match with that ETag value."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/etag-feed"
    feed_id = await _create_feed(sf, feed_url)

    etag_value = '"abc123"'

    # First fetch: 200 with ETag header
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "ETag": etag_value},
        )
    )

    now1 = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now1,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    # Second fetch: verify If-None-Match is sent
    captured_headers: dict[str, str] = {}

    def capture_and_respond(request: Any) -> Response:
        captured_headers.update(dict(request.headers))
        return Response(304)

    respx_mock.get(feed_url).mock(side_effect=capture_and_respond)

    now2 = datetime(2026, 4, 23, 11, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now2,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    assert captured_headers.get("if-none-match") == etag_value


@pytest.mark.asyncio
async def test_fetch_one_sends_if_modified_since_when_no_etag(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """When there is no ETag but a Last-Modified is stored, the next
    fetch must include If-Modified-Since instead."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/last-modified-feed"
    last_modified_value = "Wed, 23 Apr 2026 09:00:00 GMT"

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            last_modified=last_modified_value,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    captured_headers: dict[str, str] = {}

    def capture_and_respond(request: Any) -> Response:
        captured_headers.update(dict(request.headers))
        return Response(304)

    respx_mock.get(feed_url).mock(side_effect=capture_and_respond)

    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    assert captured_headers.get("if-modified-since") == last_modified_value
    assert "if-none-match" not in captured_headers


@pytest.mark.asyncio
async def test_fetch_one_updates_etag_on_new_200(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """When a 200 response returns a new ETag, feed.etag must be updated
    to reflect the latest value."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/etag-update-feed"
    old_etag = '"old-etag"'
    new_etag = '"new-etag"'

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            etag=old_etag,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "ETag": new_etag},
        )
    )

    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        assert feed.etag == new_etag


# ---- Improvement 1: 304 recovers a broken feed ------------------------------


@pytest.mark.asyncio
async def test_304_recovers_broken_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A 304 on a broken feed must flip it back to active and reset failure counters."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/broken-304-recovery"

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=5,
            last_error_code="http_5xx",
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(304))

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "active"
    assert state["consecutive_failures"] == 0
    assert state["last_successful_fetch_at"] == now


# ---- Improvement 2: Cache-Control / Expires header parsing ------------------


@pytest.mark.asyncio
async def test_200_cache_control_max_age_sets_next_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cache-Control: max-age=7200 on a 200 must set next_fetch_at to now+7200s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/cache-control-max-age"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "Cache-Control": "max-age=7200"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=7200)


@pytest.mark.asyncio
async def test_200_cache_control_max_age_floored_at_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cache-Control: max-age=10 < base_interval=60 must be floored to 60s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/cache-control-small-max-age"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "Cache-Control": "max-age=10"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_200_expires_header_sets_next_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Expires header 2h in the future must set next_fetch_at approximately now+2h."""
    from email.utils import format_datetime as _fmt_dt

    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/expires-header"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    expires_dt = now + timedelta(hours=2)
    expires_str = _fmt_dt(expires_dt, usegmt=True)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "Expires": expires_str},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 7200) <= 5


@pytest.mark.asyncio
async def test_304_cache_control_max_age_respected(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cache-Control: max-age=3600 on a 304 must set next_fetch_at to now+3600s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/304-cache-control"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(304, headers={"Cache-Control": "max-age=3600"})
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=3600)


# ---- Improvement 3: RSS TTL field -------------------------------------------

_RSS_TTL_120 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>TTL Feed</title>
<ttl>120</ttl>
<item><title>A</title><link>http://ex.com/1</link><guid>http://ex.com/1</guid></item>
</channel></rss>"""

_RSS_TTL_0 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>TTL Feed</title>
<ttl>0</ttl>
<item><title>A</title><link>http://ex.com/1</link><guid>http://ex.com/1</guid></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_rss_ttl_sets_next_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """RSS <ttl>120</ttl> (minutes) → 7200s must win over base_interval=60s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/rss-ttl-120"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=_RSS_TTL_120,
            headers={"Content-Type": "application/rss+xml"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=7200)


@pytest.mark.asyncio
async def test_rss_ttl_floored_at_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """RSS <ttl>0</ttl> → 0s must be floored to base_interval=60s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/rss-ttl-0"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=_RSS_TTL_0,
            headers={"Content-Type": "application/rss+xml"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=60)


# ---- entry_frequency scheduling tests ---------------------------------------

EMPTY_ATOM_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Empty Feed</title>
  <id>http://t.test/empty</id>
  <updated>2026-04-10T00:00:00Z</updated>
</feed>
"""


async def _insert_entries(
    session_factory: async_sessionmaker[AsyncSession],
    feed_id: int,
    fetched_ats: list[datetime],
) -> None:
    async with session_factory() as session:
        for i, fat in enumerate(fetched_ats):
            entry = Entry(
                feed_id=feed_id,
                guid=f"http://t.test/ef-entry/{feed_id}/{i}",
                url=f"http://t.test/ef-entry/{feed_id}/{i}",
                fetched_at=fat,
                content_updated_at=fat,
            )
            session.add(entry)
        await session.commit()


@pytest.mark.asyncio
async def test_entry_frequency_active_feed_with_history(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-history"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    fetched_ats = [now - timedelta(days=i) for i in range(7)]
    await _insert_entries(sf, feed_id, fetched_ats)

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
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 86400) <= 5


@pytest.mark.asyncio
async def test_entry_frequency_new_feed_falls_back_to_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-new"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=EMPTY_ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=3600,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=3600)


@pytest.mark.asyncio
async def test_entry_frequency_very_active_feed_clamped_at_min(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-very-active"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    fetched_ats = [now - timedelta(minutes=i) for i in range(10000)]
    await _insert_entries(sf, feed_id, fetched_ats)

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
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 300) <= 5


@pytest.mark.asyncio
async def test_entry_frequency_quiet_feed_clamped_at_max(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-quiet"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    await _insert_entries(sf, feed_id, [now - timedelta(days=6)])

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
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=86400)


@pytest.mark.asyncio
async def test_entry_frequency_broken_feed_ignores_history(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-broken"

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    last_success = now - timedelta(days=1)

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=5,
            last_error_code="http_5xx",
            last_successful_fetch_at=last_success,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    fetched_ats = [now - timedelta(hours=i) for i in range(100)]
    await _insert_entries(sf, feed_id, fetched_ats)

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 864) > 5


@pytest.mark.asyncio
async def test_fetch_one_caps_entries_on_subsequent_fetch_with_per_fetch_limit(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/per-fetch-cap-subsequent"
    feed_id = await _create_feed(sf, feed_url)

    # Ensure this is a subsequent fetch path (initial cap does not apply).
    async with sf() as session:
        seed_feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        session.add(
            Entry(
                feed_id=feed_id,
                guid=f"{seed_feed.url}#seed",
                url=f"{seed_feed.url}#seed",
                fetched_at=datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC),
                content_updated_at=datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC),
            )
        )
        await session.commit()

    ten_entries_body = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Per-fetch cap</title>
  <id>http://t.test/per-fetch-cap-subsequent</id>
  <updated>2026-04-11T00:00:00Z</updated>
  <entry><title>E0</title><id>http://t.test/per-fetch-cap-subsequent/e0</id><link href="http://t.test/per-fetch-cap-subsequent/e0"/><published>2026-04-11T00:00:00Z</published><content>b0</content></entry>
  <entry><title>E1</title><id>http://t.test/per-fetch-cap-subsequent/e1</id><link href="http://t.test/per-fetch-cap-subsequent/e1"/><published>2026-04-11T01:00:00Z</published><content>b1</content></entry>
  <entry><title>E2</title><id>http://t.test/per-fetch-cap-subsequent/e2</id><link href="http://t.test/per-fetch-cap-subsequent/e2"/><published>2026-04-11T02:00:00Z</published><content>b2</content></entry>
  <entry><title>E3</title><id>http://t.test/per-fetch-cap-subsequent/e3</id><link href="http://t.test/per-fetch-cap-subsequent/e3"/><published>2026-04-11T03:00:00Z</published><content>b3</content></entry>
  <entry><title>E4</title><id>http://t.test/per-fetch-cap-subsequent/e4</id><link href="http://t.test/per-fetch-cap-subsequent/e4"/><published>2026-04-11T04:00:00Z</published><content>b4</content></entry>
  <entry><title>E5</title><id>http://t.test/per-fetch-cap-subsequent/e5</id><link href="http://t.test/per-fetch-cap-subsequent/e5"/><published>2026-04-11T05:00:00Z</published><content>b5</content></entry>
  <entry><title>E6</title><id>http://t.test/per-fetch-cap-subsequent/e6</id><link href="http://t.test/per-fetch-cap-subsequent/e6"/><published>2026-04-11T06:00:00Z</published><content>b6</content></entry>
  <entry><title>E7</title><id>http://t.test/per-fetch-cap-subsequent/e7</id><link href="http://t.test/per-fetch-cap-subsequent/e7"/><published>2026-04-11T07:00:00Z</published><content>b7</content></entry>
  <entry><title>E8</title><id>http://t.test/per-fetch-cap-subsequent/e8</id><link href="http://t.test/per-fetch-cap-subsequent/e8"/><published>2026-04-11T08:00:00Z</published><content>b8</content></entry>
  <entry><title>E9</title><id>http://t.test/per-fetch-cap-subsequent/e9</id><link href="http://t.test/per-fetch-cap-subsequent/e9"/><published>2026-04-11T09:00:00Z</published><content>b9</content></entry>
</feed>
"""
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ten_entries_body,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(max_entries_per_fetch=3),
        )
        await session.commit()

    # seed(1) + capped new entries(3) = 4 total
    assert await _count_entries_for_feed(sf, feed_id) == 4
