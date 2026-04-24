"""Feed parser wrapper around feedparser.

feedparser is a synchronous, battle-tested RSS/Atom parser. We dispatch
it to a worker thread via ``anyio.to_thread.run_sync`` so the asyncio
event loop isn't blocked on large feeds or CPU-heavy parsing work.

Output shape is the lightweight ``ParsedFeed`` + ``ParsedEntry``
dataclasses the rest of the pipeline consumes (fetcher.upsert takes
``list[ParsedEntry]`` directly).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
import feedparser

from feedgate_fetcher.fetcher.upsert import ParsedEntry


@dataclass(frozen=True)
class ParsedFeed:
    title: str | None
    ttl_seconds: int | None = None
    entries: list[ParsedEntry] = field(default_factory=list)


def _struct_time_to_datetime(st: Any) -> datetime | None:
    """Convert feedparser's time.struct_time to an aware datetime (UTC)."""
    if st is None:
        return None
    try:
        return datetime(st[0], st[1], st[2], st[3], st[4], st[5], tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        return None


def _extract_content(fp_entry: Any) -> str | None:
    """Pick the 'content' string from a feedparser entry, or None."""
    content_list = fp_entry.get("content")
    if content_list:
        first = content_list[0]
        value = first.get("value") if isinstance(first, dict) else None
        if value:
            return str(value)
    summary = fp_entry.get("summary")
    return str(summary) if summary else None


def _extract_entry(fp_entry: Any) -> ParsedEntry:
    # feedparser uses .id for Atom <id> / RSS <guid>. When a feed omits
    # guid/id, feedparser copies <link> into .id so the field is still
    # populated — that is our de-facto fallback for "guid missing".
    guid = fp_entry.get("id") or fp_entry.get("link") or ""
    url = fp_entry.get("link") or guid
    title = fp_entry.get("title")
    author = fp_entry.get("author")
    content = _extract_content(fp_entry)
    published_at = _struct_time_to_datetime(
        fp_entry.get("published_parsed") or fp_entry.get("updated_parsed")
    )

    return ParsedEntry(
        guid=guid,
        url=url,
        title=title,
        content=content,
        author=author,
        published_at=published_at,
    )


def _parse_sync(body: bytes) -> ParsedFeed:
    parsed = feedparser.parse(body)
    feed_meta = getattr(parsed, "feed", None)
    feed_title = feed_meta.get("title") if feed_meta else None
    entries = [_extract_entry(e) for e in parsed.entries]
    ttl_seconds: int | None = None
    if feed_meta:
        ttl_raw = feed_meta.get("ttl")
        if ttl_raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                ttl_seconds = max(0, int(ttl_raw)) * 60
    return ParsedFeed(title=feed_title, ttl_seconds=ttl_seconds, entries=entries)


async def parse_feed(body: bytes) -> ParsedFeed:
    """Parse a raw feed body (bytes) in a worker thread."""
    return await anyio.to_thread.run_sync(_parse_sync, body)
