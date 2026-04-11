"""Entry upsert logic.

Implements the mutation policy from docs/spec/entry.md:

* New entry (no existing row for (feed_id, guid)):
    INSERT with `fetched_at = content_updated_at = now()`.
* Existing entry, one of {url, title, content, author, published_at}
  changed:
    UPDATE the changed content fields and set `content_updated_at = now()`.
    `fetched_at` is NEVER touched (ADR 001 invariant #4, ADR 004).
* Existing entry, no change:
    no-op (not even `content_updated_at` or `fetched_at` move).

The Postgres-native `ON CONFLICT ... DO UPDATE ... WHERE` form expresses
all three cases in a single statement: the `WHERE` on the conflict
branch guards the UPDATE so identical payloads become no-ops.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.models import Entry


@dataclass(frozen=True)
class ParsedEntry:
    """Minimal content-bearing fields a parser emits for one entry.

    This is the upsert input shape. Additional normalization (e.g. URL
    absolutization) happens before this dataclass is constructed.
    """

    guid: str
    url: str
    title: str | None = None
    content: str | None = None
    author: str | None = None
    published_at: datetime | None = None


async def upsert_entries(
    session: AsyncSession,
    feed_id: int,
    entries: list[ParsedEntry],
    *,
    now: datetime,
) -> None:
    """Upsert a batch of parsed entries for a single feed.

    `now` is passed in explicitly so tests and callers can control the
    clock. Both `fetched_at` (new inserts only) and `content_updated_at`
    (new inserts + actual updates) use the same value within one call.
    """
    if not entries:
        return

    for entry in entries:
        stmt = insert(Entry).values(
            feed_id=feed_id,
            guid=entry.guid,
            url=entry.url,
            title=entry.title,
            content=entry.content,
            author=entry.author,
            published_at=entry.published_at,
            fetched_at=now,
            content_updated_at=now,
        )
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["feed_id", "guid"],
            set_={
                "url": excluded.url,
                "title": excluded.title,
                "content": excluded.content,
                "author": excluded.author,
                "published_at": excluded.published_at,
                "content_updated_at": func.now(),
            },
            where=(
                Entry.url.is_distinct_from(excluded.url)
                | Entry.title.is_distinct_from(excluded.title)
                | Entry.content.is_distinct_from(excluded.content)
                | Entry.author.is_distinct_from(excluded.author)
                | Entry.published_at.is_distinct_from(excluded.published_at)
            ),
        )
        await session.execute(stmt)
