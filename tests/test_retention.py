"""Entry retention sweep tests (ADR 004, docs/spec/entry.md).

The rule:
    Per feed, KEEP the union of
      * entries with ``fetched_at >= cutoff`` (time window)
      * the ``min_per_feed`` most-recent entries (top-N window)
    Everything else gets DELETED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate import retention
from feedgate.models import Entry, Feed
from feedgate.retention import sweep


async def _mk_feed(session: AsyncSession, url: str) -> int:
    feed = Feed(url=url, effective_url=url)
    session.add(feed)
    await session.flush()
    return feed.id


async def _mk_entry(
    session: AsyncSession,
    feed_id: int,
    guid: str,
    fetched_at: datetime,
) -> None:
    session.add(
        Entry(
            feed_id=feed_id,
            guid=guid,
            url=f"http://t.test/{guid}",
            title=guid,
            fetched_at=fetched_at,
            content_updated_at=fetched_at,
        )
    )


async def _count_entries(session: AsyncSession, feed_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
    )
    return int(result.scalar_one())


async def _guids_for_feed(session: AsyncSession, feed_id: int) -> set[str]:
    result = await session.execute(select(Entry.guid).where(Entry.feed_id == feed_id))
    return {row[0] for row in result.all()}


@pytest.mark.asyncio
async def test_sweep_empty_db_is_noop(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    deleted = await sweep(
        async_session,
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        min_per_feed=20,
    )
    assert deleted == 0


@pytest.mark.asyncio
async def test_sweep_deletes_entries_older_than_cutoff(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    feed_id = await _mk_feed(async_session, "http://t.test/feed-age")

    now = datetime(2026, 4, 11, tzinfo=UTC)
    # 5 entries spaced 1 day apart going back in time
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"age-{i}",
            fetched_at=now - timedelta(days=i),
        )
    await async_session.flush()

    # Cutoff = now - 2d  (inclusive, >= cutoff keeps equal-to-boundary)
    # → keeps days 0, 1, 2 ; drops days 3, 4
    # Per-feed top-N also protects the most recent, so set
    # min_per_feed=0 to isolate the time-window behaviour.
    cutoff = now - timedelta(days=2)
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=0)

    assert deleted == 2
    assert await _count_entries(async_session, feed_id) == 3
    remaining = await _guids_for_feed(async_session, feed_id)
    assert remaining == {"age-0", "age-1", "age-2"}


@pytest.mark.asyncio
async def test_sweep_keeps_min_per_feed_regardless_of_age(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Even if every entry is ancient, the most-recent N must stay."""
    feed_id = await _mk_feed(async_session, "http://t.test/feed-ancient")

    ancient = datetime(2020, 1, 1, tzinfo=UTC)  # way before cutoff
    # 10 entries all way in the past, spaced 1 hour apart
    for i in range(10):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"anc-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    await async_session.flush()

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)  # all 10 are older
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=3)

    # 10 ancient entries, time window keeps 0, top-3 keeps 3 most-recent
    # (anc-9, anc-8, anc-7). Delete = 10 - 3 = 7.
    assert deleted == 7
    remaining = await _guids_for_feed(async_session, feed_id)
    assert remaining == {"anc-9", "anc-8", "anc-7"}


@pytest.mark.asyncio
async def test_sweep_keeps_recent_entries_inside_window(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Entries inside the time window must be kept even if the
    per-feed top-N cap would not include them."""
    feed_id = await _mk_feed(async_session, "http://t.test/feed-recent")

    now = datetime(2026, 4, 11, tzinfo=UTC)
    for i in range(10):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"rec-{i}",
            fetched_at=now - timedelta(hours=i),  # all within last 10 hours
        )
    await async_session.flush()

    cutoff = now - timedelta(days=1)  # all 10 are inside the window
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=2)

    assert deleted == 0
    assert await _count_entries(async_session, feed_id) == 10


@pytest.mark.asyncio
async def test_sweep_per_feed_isolation(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Each feed gets its OWN top-N — feeds don't share the budget."""
    feed_a = await _mk_feed(async_session, "http://t.test/feed-a")
    feed_b = await _mk_feed(async_session, "http://t.test/feed-b")

    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    # Feed A: 5 ancient entries
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_a,
            guid=f"a-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    # Feed B: 5 ancient entries
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_b,
            guid=f"b-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    await async_session.flush()

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=2)

    # Each feed: 5 entries - 2 kept = 3 deleted -> 6 total deleted
    assert deleted == 6
    assert await _count_entries(async_session, feed_a) == 2
    assert await _count_entries(async_session, feed_b) == 2

    remaining_a = await _guids_for_feed(async_session, feed_a)
    remaining_b = await _guids_for_feed(async_session, feed_b)
    assert remaining_a == {"a-4", "a-3"}
    assert remaining_b == {"b-4", "b-3"}


@pytest.mark.asyncio
async def test_sweep_union_of_time_and_topn(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Integration case: some entries inside the window, some outside
    but protected by top-N, some outside both (should be deleted)."""
    feed_id = await _mk_feed(async_session, "http://t.test/feed-union")

    now = datetime(2026, 4, 11, tzinfo=UTC)
    # 3 recent entries (inside window)
    for i in range(3):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"new-{i}",
            fetched_at=now - timedelta(hours=i),
        )
    # 10 ancient entries
    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    for i in range(10):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"old-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    await async_session.flush()

    cutoff = now - timedelta(days=1)  # keeps the 3 recent ones
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=5)

    # Keep set: 3 recent (time window) + top-5 by fetched_at DESC
    # (which is new-0, new-1, new-2, old-9, old-8) = 5 unique.
    # UNION of time window {new-0, new-1, new-2} and top-5
    # {new-0, new-1, new-2, old-9, old-8} = 5 entries.
    # Total: 13, keep 5, delete 8.
    assert deleted == 8
    remaining = await _guids_for_feed(async_session, feed_id)
    assert remaining == {"new-0", "new-1", "new-2", "old-9", "old-8"}


@pytest.mark.asyncio
async def test_sweep_returns_zero_when_nothing_to_delete(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    feed_id = await _mk_feed(async_session, "http://t.test/feed-fresh")
    now = datetime(2026, 4, 11, tzinfo=UTC)
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"fresh-{i}",
            fetched_at=now - timedelta(minutes=i),
        )
    await async_session.flush()

    deleted = await sweep(
        async_session,
        cutoff=now - timedelta(days=90),
        min_per_feed=20,
    )
    assert deleted == 0
    assert await _count_entries(async_session, feed_id) == 5


# ---- retention.tick_once integration ---------------------------------------


@pytest_asyncio.fixture
async def retention_app(
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> FastAPI:
    """Minimal FastAPI app with the state retention.tick_once reads."""
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.retention_days = 90
    app.state.retention_min_per_feed = 2
    app.state.retention_sweep_interval_seconds = 3600
    return app


@pytest.mark.asyncio
async def test_tick_once_runs_sweep_via_app_state(
    retention_app: FastAPI,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """tick_once reads cutoff + min_per_feed from ``app.state``, opens
    its own session, commits, and returns the delete count."""
    # 5 ancient entries, retention_min_per_feed=2 in the fixture
    # → tick_once deletes 3, keeps the 2 most recent.
    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    async with async_session_factory() as session:
        feed_id = await _mk_feed(session, "http://t.test/feed-tick")
        for i in range(5):
            await _mk_entry(
                session,
                feed_id,
                guid=f"t-{i}",
                fetched_at=ancient + timedelta(hours=i),
            )
        await session.commit()

    now = datetime(2026, 4, 11, tzinfo=UTC)
    deleted = await retention.tick_once(retention_app, now=now)
    assert deleted == 3

    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
            )
        ).scalar_one()
        guids = {
            row[0]
            for row in (
                await session.execute(select(Entry.guid).where(Entry.feed_id == feed_id))
            ).all()
        }
    assert count == 2
    assert guids == {"t-4", "t-3"}
