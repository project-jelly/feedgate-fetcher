"""Foundation smoke test.

Verifies that the testcontainers Postgres is reachable and we can run a
trivial query through the async engine. If this test is red the whole
project is blocked — every later Phase depends on this fixture working.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_postgres_container_is_reachable(
    async_engine: AsyncEngine,
) -> None:
    async with async_engine.begin() as conn:
        result = await conn.execute(text("SELECT 1"))
        row = result.one()
        assert row[0] == 1
