"""Entry upsert semantics — spec/entry.md mutation policy.

Covers the four cases from plan WP 1.5:

(a) new entry INSERT -> fetched_at == content_updated_at
(b) re-upsert identical payload -> no-op (both timestamps unchanged)
(c) re-upsert with any content field changed -> UPDATE affected fields
    + content_updated_at = now(), but fetched_at stays immutable
(d) (feed_id, guid) UNIQUE is not violated across repeated upserts
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate_fetcher.fetcher.upsert import ParsedEntry, upsert_entries
from feedgate_fetcher.models import Entry, Feed


@pytest_asyncio.fixture
async def feed(async_session: AsyncSession) -> Feed:
    """Insert a fresh feed row and return it. Rolled back per-test."""
    row = Feed(url="http://fake.test/feed.xml", effective_url="http://fake.test/feed.xml")
    async_session.add(row)
    await async_session.flush()
    return row


async def _entry_row(session: AsyncSession, feed_id: int, guid: str) -> Entry:
    result = await session.execute(
        select(Entry).where(Entry.feed_id == feed_id, Entry.guid == guid)
    )
    return result.scalar_one()


async def _entry_snapshot(session: AsyncSession, feed_id: int, guid: str) -> dict[str, Any]:
    """Read entry fields as a plain dict, bypassing the ORM identity map.

    Used in the "upsert twice" tests so the second read isn't tainted by
    the cached ORM instance from the first read (which would otherwise
    trigger lazy reload and fail inside async-session context).
    """
    result = await session.execute(
        select(
            Entry.title,
            Entry.content,
            Entry.author,
            Entry.url,
            Entry.published_at,
            Entry.fetched_at,
            Entry.content_updated_at,
        ).where(Entry.feed_id == feed_id, Entry.guid == guid)
    )
    row = result.one()
    return {
        "title": row.title,
        "content": row.content,
        "author": row.author,
        "url": row.url,
        "published_at": row.published_at,
        "fetched_at": row.fetched_at,
        "content_updated_at": row.content_updated_at,
    }


async def _count_entries(session: AsyncSession, feed_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_upsert_new_entry_sets_both_timestamps_equal(
    async_session: AsyncSession, feed: Feed
) -> None:
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    parsed = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [parsed], now=t0)

    row = await _entry_row(async_session, feed.id, "guid-a")
    assert row.title == "Hello"
    assert row.content == "Body"
    assert row.author == "Alice"
    assert row.fetched_at == t0
    assert row.content_updated_at == t0


@pytest.mark.asyncio
async def test_upsert_identical_payload_is_noop(async_session: AsyncSession, feed: Feed) -> None:
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    parsed = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [parsed], now=t0)
    await upsert_entries(async_session, feed.id, [parsed], now=t1)

    row = await _entry_row(async_session, feed.id, "guid-a")
    # Both timestamps must be unchanged (no-op) because content is identical.
    assert row.fetched_at == t0
    assert row.content_updated_at == t0
    # And exactly one row exists (UNIQUE constraint honored).
    assert await _count_entries(async_session, feed.id) == 1


@pytest.mark.asyncio
async def test_upsert_changed_title_updates_content_updated_at_only(
    async_session: AsyncSession, feed: Feed
) -> None:
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    original = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )
    edited = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello (edited)",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [original], now=t0)
    before = await _entry_snapshot(async_session, feed.id, "guid-a")

    # Use a later clock for the second call; the UPDATE branch sets
    # content_updated_at via SQL now() so the actual value will be
    # greater than the original regardless of the `now` parameter, but
    # fetched_at must stay frozen.
    await upsert_entries(
        async_session,
        feed.id,
        [edited],
        now=t0 + timedelta(hours=2),
    )
    after = await _entry_snapshot(async_session, feed.id, "guid-a")

    assert after["title"] == "Hello (edited)"
    assert after["fetched_at"] == before["fetched_at"]  # immutable
    assert after["content_updated_at"] > before["content_updated_at"]  # advanced
    assert await _count_entries(async_session, feed.id) == 1


@pytest.mark.asyncio
async def test_upsert_changed_published_at_also_updates(
    async_session: AsyncSession, feed: Feed
) -> None:
    """Cover the published_at branch of the distinct-from check."""
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    original = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )
    edited = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 30, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [original], now=t0)
    before = await _entry_snapshot(async_session, feed.id, "guid-a")

    await upsert_entries(
        async_session,
        feed.id,
        [edited],
        now=t0 + timedelta(hours=2),
    )
    after = await _entry_snapshot(async_session, feed.id, "guid-a")

    assert after["published_at"] == datetime(2026, 4, 10, 11, 30, 0, tzinfo=UTC)
    assert after["fetched_at"] == before["fetched_at"]
    assert after["content_updated_at"] > before["content_updated_at"]
