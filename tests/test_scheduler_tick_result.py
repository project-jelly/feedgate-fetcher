from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate_fetcher.fetcher import scheduler
from feedgate_fetcher.models import Feed


def _atom_with(guid: str, title: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed for {title}</title>
  <id>http://t.test/{guid}</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>{title}</title>
    <id>{guid}</id>
    <link href="{guid}"/>
    <published>2026-04-10T00:00:00Z</published>
    <content>body of {title}</content>
  </entry>
</feed>
""".encode()


async def _seed_due_feeds(
    sf: async_sessionmaker[AsyncSession],
    urls: list[str],
    now: datetime,
) -> None:
    async with sf() as session:
        for url in urls:
            session.add(
                Feed(
                    url=url,
                    effective_url=url,
                    next_fetch_at=now - timedelta(seconds=5),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_tick_result_with_no_due_feeds(fetch_app: FastAPI) -> None:
    result = await scheduler.tick_once(fetch_app)

    assert result.claimed == 0
    assert result.processed == 0
    assert result.fatal_errors == 0
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_tick_result_counts_successful_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    urls = [
        "http://result-a.test/feed",
        "http://result-b.test/feed",
        "http://result-c.test/feed",
    ]
    await _seed_due_feeds(sf, urls, now)

    for url in urls:
        respx_mock.get(url).mock(
            return_value=Response(
                200,
                content=_atom_with(url + "/post-1", f"post for {url}"),
                headers={"Content-Type": "application/atom+xml"},
            )
        )

    result = await scheduler.tick_once(fetch_app, now=now)

    assert result.claimed == len(urls)
    assert result.processed == len(urls)
    assert result.fatal_errors == 0
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_tick_result_counts_fatal_errors(
    fetch_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    urls = [
        "http://fatal-a.test/feed",
        "http://fatal-b.test/feed",
    ]
    await _seed_due_feeds(sf, urls, now)

    async def always_raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("fatal test error")

    monkeypatch.setattr(scheduler, "fetch_one", always_raise)

    result = await scheduler.tick_once(fetch_app, now=now)

    assert result.claimed == len(urls)
    assert result.processed == 0
    assert result.fatal_errors == len(urls)
    assert result.duration_seconds >= 0
