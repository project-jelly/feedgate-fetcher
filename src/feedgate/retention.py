"""Entry retention sweep.

Implements the deletion rule from ADR 004 / docs/spec/entry.md:

    For each feed, retain the UNION of
      * entries within the time window (``fetched_at >= cutoff``)
      * the ``min_per_feed`` most-recent entries, regardless of age
    Delete the rest.

The retain rule is expressed as a single Postgres statement using a
window-function CTE for the per-feed top-N and a UNION for the time
window. The ``fetched_at`` clock — not ``published_at`` — is the
retention anchor (ADR 004 decision #1) because we control it.

The ``cutoff`` is passed in explicitly so callers (and tests) control
the clock. Production code calls with ``datetime.now(UTC) -
timedelta(days=settings.retention_days)``.

The function does NOT commit — the caller owns the transaction.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Single statement that computes the keep-set (time window OR per-feed
# top-N by fetched_at DESC) and deletes everything else. RETURNING id
# lets the caller count deleted rows cheaply.
SWEEP_SQL = text(
    """
    WITH ranked AS (
        SELECT
            id,
            row_number() OVER (
                PARTITION BY feed_id
                ORDER BY fetched_at DESC, id DESC
            ) AS rn
        FROM entries
    ),
    keep AS (
        SELECT id FROM entries WHERE fetched_at >= :cutoff
        UNION
        SELECT id FROM ranked WHERE rn <= :min_per_feed
    )
    DELETE FROM entries
    WHERE id NOT IN (SELECT id FROM keep)
    RETURNING id
    """
)


async def sweep(
    session: AsyncSession,
    *,
    cutoff: datetime,
    min_per_feed: int,
) -> int:
    """Delete aged-out entries that fall outside both retain windows.

    Returns the number of rows deleted. Does not commit — the caller
    is responsible for the transaction.
    """
    result = await session.execute(
        SWEEP_SQL,
        {"cutoff": cutoff, "min_per_feed": min_per_feed},
    )
    return len(result.fetchall())


# ---- App-level wrappers ----------------------------------------------------


async def tick_once(
    app: FastAPI,
    *,
    now: datetime | None = None,
) -> int:
    """Run a single retention sweep using settings from ``app.state``.

    Opens a dedicated session, computes the cutoff as
    ``now - retention_days``, executes the sweep, commits on success,
    rolls back on error. Returns the number of rows deleted.
    """
    now = now or datetime.now(UTC)
    days: int = app.state.retention_days
    min_per_feed: int = app.state.retention_min_per_feed
    cutoff = now - timedelta(days=days)

    sf = app.state.session_factory
    async with sf() as session:
        try:
            n = await sweep(
                session,
                cutoff=cutoff,
                min_per_feed=min_per_feed,
            )
            await session.commit()
            return n
        except Exception:
            await session.rollback()
            raise


async def run(
    app: FastAPI,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that calls ``tick_once`` every interval.

    TDD-exempt — thin wrapper around ``tick_once``, correctness is
    covered by the sweep tests and the tick_once integration test.
    Never lets the loop die: any exception is logged and swallowed.
    """
    interval = app.state.retention_sweep_interval_seconds
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            n = await tick_once(app)
            logger.info("retention sweep deleted %d entries", n)
        except Exception:
            logger.exception("retention sweep raised; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
