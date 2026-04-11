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

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
