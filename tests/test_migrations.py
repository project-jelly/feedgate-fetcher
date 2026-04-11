"""Migration smoke test.

Verifies that Alembic's `upgrade head` creates the `feeds` and `entries`
tables with the required indexes and the unique constraint defined in
docs/spec/feed.md and docs/spec/entry.md.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_tables_exist_after_migration(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        tables = {row[0] for row in result}
    assert "feeds" in tables
    assert "entries" in tables


@pytest.mark.asyncio
async def test_required_indexes_exist(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
        indexes = {row[0] for row in result}
    assert "ix_feeds_status" in indexes
    assert "ix_feeds_next_fetch_at_active" in indexes
    assert "ix_entries_fetched_at" in indexes
    assert "ix_entries_feed_pub_id" in indexes


@pytest.mark.asyncio
async def test_entries_unique_constraint(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'entries'::regclass AND contype = 'u'"
            )
        )
        constraints = {row[0] for row in result}
    assert "uq_entries_feed_guid" in constraints
