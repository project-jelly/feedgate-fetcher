from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from feedgate_fetcher.metrics import (
    ACTIVE_FEED_MAX_AGE_SECONDS,
    ACTIVE_FEEDS_STALE_TOTAL,
    _collect_state,
)
from feedgate_fetcher.models import Feed, FeedStatus


def _stale_count(threshold_seconds: int) -> float:
    return float(
        ACTIVE_FEEDS_STALE_TOTAL.labels(threshold_seconds=str(threshold_seconds))._value.get()
    )


@pytest.mark.asyncio
async def test_collect_state_sets_active_max_age_zero_with_no_active_feeds(
    async_session_factory: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
    truncate_tables: None,
) -> None:
    await _collect_state(async_session_factory, async_engine)

    assert ACTIVE_FEED_MAX_AGE_SECONDS._value.get() == 0
    assert _stale_count(1800) == 0
    assert _stale_count(3600) == 0
    assert _stale_count(86400) == 0


@pytest.mark.asyncio
async def test_collect_state_counts_active_stale_feeds_by_threshold(
    async_session_factory: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
    truncate_tables: None,
) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add_all(
            [
                Feed(
                    url="http://fresh.test/feed",
                    effective_url="http://fresh.test/feed",
                    last_successful_fetch_at=now - timedelta(minutes=10),
                    created_at=now - timedelta(days=3),
                ),
                Feed(
                    url="http://stale-45m.test/feed",
                    effective_url="http://stale-45m.test/feed",
                    last_successful_fetch_at=now - timedelta(minutes=45),
                    created_at=now - timedelta(days=3),
                ),
                Feed(
                    url="http://stale-2h.test/feed",
                    effective_url="http://stale-2h.test/feed",
                    last_successful_fetch_at=now - timedelta(hours=2),
                    created_at=now - timedelta(days=3),
                ),
                Feed(
                    url="http://stale-2d.test/feed",
                    effective_url="http://stale-2d.test/feed",
                    last_successful_fetch_at=now - timedelta(days=2),
                    created_at=now - timedelta(days=3),
                ),
                Feed(
                    url="http://broken-old.test/feed",
                    effective_url="http://broken-old.test/feed",
                    status=FeedStatus.BROKEN,
                    last_successful_fetch_at=now - timedelta(days=10),
                    created_at=now - timedelta(days=10),
                ),
            ]
        )
        await session.commit()

    await _collect_state(async_session_factory, async_engine)

    assert _stale_count(1800) == 3
    assert _stale_count(3600) == 2
    assert _stale_count(86400) == 1
    assert ACTIVE_FEED_MAX_AGE_SECONDS._value.get() == pytest.approx(
        timedelta(days=2).total_seconds(),
        abs=10,
    )


@pytest.mark.asyncio
async def test_collect_state_uses_created_at_for_never_successful_active_feed(
    async_session_factory: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
    truncate_tables: None,
) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add_all(
            [
                Feed(
                    url="http://never-success.test/feed",
                    effective_url="http://never-success.test/feed",
                    last_successful_fetch_at=None,
                    created_at=now - timedelta(hours=3),
                ),
                Feed(
                    url="http://success-newer-than-created.test/feed",
                    effective_url="http://success-newer-than-created.test/feed",
                    last_successful_fetch_at=now - timedelta(hours=1),
                    created_at=now - timedelta(days=10),
                ),
            ]
        )
        await session.commit()

    await _collect_state(async_session_factory, async_engine)

    assert ACTIVE_FEED_MAX_AGE_SECONDS._value.get() == pytest.approx(
        timedelta(hours=3).total_seconds(),
        abs=10,
    )
