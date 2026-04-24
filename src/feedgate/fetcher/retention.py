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

By default (``batch_size=0``), the function does NOT commit.
When ``batch_size>0``, it deletes at most one batch and commits that
batch immediately to keep transactions short.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import delete, func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.metrics import RETENTION_DELETED_TOTAL
from feedgate.models import Entry

logger = logging.getLogger(__name__)


async def sweep(
    session: AsyncSession,
    *,
    cutoff: datetime,
    min_per_feed: int,
    batch_size: int = 0,
) -> int:
    """Delete aged-out entries that fall outside both retain windows.

    Uses SQLAlchemy 2.0 Core expressions end-to-end — a window-
    function CTE computes per-feed row numbers, the keep set is a
    UNION of the time window and the per-feed top-N, and the final
    DELETE ... RETURNING is an ORM-level bulk delete. Returns the
    number of rows deleted. By default (``batch_size=0``), does not
    commit. When ``batch_size>0``, commits the single deleted batch.
    """
    # Per-feed ranking by fetched_at DESC, id DESC (matches the
    # compound keyset index on entries).
    ranked = select(
        Entry.id.label("id"),
        func.row_number()
        .over(
            partition_by=Entry.feed_id,
            order_by=(Entry.fetched_at.desc(), Entry.id.desc()),
        )
        .label("rn"),
    ).cte("ranked")

    # Keep set: time window UNION per-feed top-N.
    time_window = select(Entry.id).where(Entry.fetched_at >= cutoff)
    top_n = select(ranked.c.id).where(ranked.c.rn <= min_per_feed)
    keep = union(time_window, top_n).subquery("keep")

    if batch_size > 0:
        victim_ids = (
            select(Entry.id)
            .where(Entry.id.not_in(select(keep.c.id)))
            .limit(batch_size)
        )
        stmt = (
            delete(Entry)
            .where(Entry.id.in_(victim_ids))
            .returning(Entry.id)
            .execution_options(synchronize_session=False)
        )
        result = await session.execute(stmt)
        n = len(result.fetchall())
        if n > 0:
            await session.commit()
        return n

    stmt = (
        delete(Entry)
        .where(Entry.id.not_in(select(keep.c.id)))
        .returning(Entry.id)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
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
    batch_size: int = app.state.retention_batch_size
    cutoff = now - timedelta(days=days)

    sf = app.state.session_factory
    async with sf() as session:
        try:
            n = await sweep(
                session,
                cutoff=cutoff,
                min_per_feed=min_per_feed,
                batch_size=batch_size,
            )
            await session.commit()
            if n > 0:
                RETENTION_DELETED_TOTAL.inc(n)
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
