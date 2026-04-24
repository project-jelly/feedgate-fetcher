"""Feed parser wrapper tests.

Validates that feedparser → ParsedFeed/ParsedEntry mapping works for
both Atom and RSS 2.0, with reasonable behaviour on missing fields.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from feedgate_fetcher.fetcher.parser import parse_feed

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Sample Atom Feed</title>
  <id>http://example.com/</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>First Post</title>
    <id>http://example.com/posts/1</id>
    <link href="http://example.com/posts/1"/>
    <published>2026-04-10T00:00:00Z</published>
    <updated>2026-04-10T00:00:00Z</updated>
    <content>First content</content>
    <author><name>Alice</name></author>
  </entry>
  <entry>
    <title>Second Post</title>
    <id>http://example.com/posts/2</id>
    <link href="http://example.com/posts/2"/>
    <published>2026-04-10T01:00:00Z</published>
    <updated>2026-04-10T01:00:00Z</updated>
    <summary>Second summary</summary>
  </entry>
</feed>
"""

RSS_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Sample RSS Feed</title>
    <link>http://example.com/</link>
    <description>desc</description>
    <item>
      <title>RSS Post</title>
      <guid>http://example.com/rss/1</guid>
      <link>http://example.com/rss/1</link>
      <pubDate>Thu, 10 Apr 2026 00:00:00 +0000</pubDate>
      <description>RSS content</description>
      <author>bob@example.com (Bob)</author>
    </item>
  </channel>
</rss>
"""


@pytest.mark.asyncio
async def test_parse_atom_basic() -> None:
    feed = await parse_feed(ATOM_SAMPLE)
    assert feed.title == "Sample Atom Feed"
    assert len(feed.entries) == 2

    first = feed.entries[0]
    assert first.guid == "http://example.com/posts/1"
    assert first.url == "http://example.com/posts/1"
    assert first.title == "First Post"
    assert first.content == "First content"
    assert first.author == "Alice"
    assert isinstance(first.published_at, datetime)

    second = feed.entries[1]
    assert second.guid == "http://example.com/posts/2"
    # Summary falls back into content
    assert second.content == "Second summary"


@pytest.mark.asyncio
async def test_parse_rss_basic() -> None:
    feed = await parse_feed(RSS_SAMPLE)
    assert feed.title == "Sample RSS Feed"
    assert len(feed.entries) == 1
    entry = feed.entries[0]
    assert entry.guid == "http://example.com/rss/1"
    assert entry.url == "http://example.com/rss/1"
    assert entry.title == "RSS Post"
    assert entry.content == "RSS content"
    assert isinstance(entry.published_at, datetime)


@pytest.mark.asyncio
async def test_parse_entry_with_missing_guid_falls_back_to_link() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>T</title>
    <link>http://example.com/</link>
    <description>d</description>
    <item>
      <title>No guid</title>
      <link>http://example.com/no-guid</link>
      <description>body</description>
    </item>
  </channel>
</rss>
"""
    feed = await parse_feed(xml)
    assert len(feed.entries) == 1
    # feedparser copies <link> into .id when no <guid> is present
    assert feed.entries[0].guid == "http://example.com/no-guid"


@pytest.mark.asyncio
async def test_parse_entry_with_missing_published_at() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>T</title><id>http://e.com/</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>No date</title>
    <id>x1</id>
    <link href="http://e.com/x1"/>
  </entry>
</feed>
"""
    feed = await parse_feed(xml)
    entry = feed.entries[0]
    # Either None or a datetime is acceptable (feedparser may fall back
    # to the feed's <updated>).
    assert entry.published_at is None or isinstance(entry.published_at, datetime)


@pytest.mark.asyncio
async def test_parse_empty_entries() -> None:
    xml = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Empty</title>
  <id>http://e.com/</id>
  <updated>2026-04-10T00:00:00Z</updated>
</feed>
"""
    feed = await parse_feed(xml)
    assert feed.title == "Empty"
    assert feed.entries == []
