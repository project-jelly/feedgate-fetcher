"""fetch_one unit tests (Phase 4 WP 4.1).

Happy path and one failure case. respx mocks the transport so no real
HTTP is issued.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.config import Settings
from feedgate.fetcher.http import (
    _compute_next_fetch_at,
    _parse_retry_after,
    fetch_one,
)
from feedgate.models import Entry, Feed

_TEST_SETTINGS = Settings()
_FETCH_DEFAULTS: dict[str, Any] = {
    "max_bytes": _TEST_SETTINGS.fetch_max_bytes,
    "max_entries_initial": _TEST_SETTINGS.fetch_max_entries_initial,
    "broken_threshold": _TEST_SETTINGS.broken_threshold,
    "dead_duration_days": _TEST_SETTINGS.dead_duration_days,
    "broken_max_backoff_seconds": _TEST_SETTINGS.broken_max_backoff_seconds,
    "backoff_jitter_ratio": _TEST_SETTINGS.backoff_jitter_ratio,
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


def test_parse_retry_after_none_returns_none() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after(None, now=now) is None


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
