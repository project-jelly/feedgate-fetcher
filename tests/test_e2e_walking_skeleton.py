"""E2E walking skeleton test — the TDD first red.

This test is intentionally red at Phase 0 of the plan and progresses through
fail modes as Phases 1~5 fill in the missing pieces. The test body is
written in its final form; each Phase completion shifts the failure mode
forward (ImportError → AttributeError → HTTP 404 → assertion → green at
Phase 5.2).

DO NOT mark this test `xfail` or `skip`. It must fail loudly until the
walking skeleton is complete. See `.omc/plans/ralplan-feedgate-walking-
skeleton.md` Phase 0.3 for the policy.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

ATOM_BODY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Fake Test Feed</title>
  <id>http://fake.test/feed.xml</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>Hello World</title>
    <id>http://fake.test/posts/hello</id>
    <link href="http://fake.test/posts/hello"/>
    <updated>2026-04-10T00:00:00Z</updated>
    <published>2026-04-10T00:00:00Z</published>
    <content>First entry.</content>
  </entry>
  <entry>
    <title>Second Post</title>
    <id>http://fake.test/posts/second</id>
    <link href="http://fake.test/posts/second"/>
    <updated>2026-04-10T01:00:00Z</updated>
    <published>2026-04-10T01:00:00Z</published>
    <content>Second entry.</content>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_walking_skeleton_happy_path(
    pg_container: PostgresContainer,
    respx_mock,  # provided by the respx pytest plugin
    truncate_tables: None,  # ensure a clean feeds/entries table
) -> None:
    """Register a feed, run one scheduler tick, verify entries appear via API.

    Expected failure mode per Phase:
      * Phase 0 end: ImportError (feedgate.main / scheduler do not exist)
      * Phase 1 end: same (main.py still absent)
      * Phase 2 end: same
      * Phase 3 end: same (or AttributeError on scheduler.tick_once)
      * Phase 4 end: same (main.py still absent)
      * Phase 5.1:   test runs but wiring may be off (404 / missing field)
      * Phase 5.2:   green
    """
    # Imports live inside the test body so earlier-Phase failures surface
    # as ImportError on the specific missing symbol rather than a pytest
    # collection error on the whole file.
    from feedgate.fetcher import scheduler
    from feedgate.main import create_app

    # Wire the app to the test database and disable the background
    # scheduler task — we will drive ticks manually to avoid racing with
    # the lifespan-spawned loop.
    os.environ["FEEDGATE_SCHEDULER_ENABLED"] = "false"
    db_url = pg_container.get_connection_url()
    db_url = db_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
    os.environ["FEEDGATE_DATABASE_URL"] = db_url

    # Mock the external feed URL with a minimal valid Atom document.
    feed_url = "http://fake.test/feed.xml"
    respx_mock.get(feed_url).respond(
        status_code=200,
        headers={"Content-Type": "application/atom+xml"},
        content=ATOM_BODY,
    )

    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Register the feed.
        resp = await client.post("/v1/feeds", json={"url": feed_url})
        assert resp.status_code in (200, 201), resp.text
        feed = resp.json()
        assert feed["url"] == feed_url
        assert feed["status"] == "active"
        assert "last_successful_fetch_at" in feed
        feed_id = feed["id"]

        # Drive one scheduler tick manually.
        await scheduler.tick_once(app)

        # Fetch entries for the registered feed.
        resp = await client.get(
            "/v1/entries",
            params={"feed_ids": str(feed_id), "limit": "10"},
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()

        assert "items" in page
        entries = page["items"]
        assert len(entries) == 2
        guids = {e["guid"] for e in entries}
        assert "http://fake.test/posts/hello" in guids
        assert "http://fake.test/posts/second" in guids
        for e in entries:
            for field in (
                "id",
                "guid",
                "feed_id",
                "url",
                "title",
                "fetched_at",
                "content_updated_at",
            ):
                assert field in e, f"missing {field!r} in entry response"
