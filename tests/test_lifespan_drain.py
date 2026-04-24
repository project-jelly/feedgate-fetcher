"""Coverage for the lifespan drain helper.

PR #15 added a graceful shutdown path but only tested the
``scheduler.run`` side of the contract. Codex review on PR #15 flagged
two gaps:

  1. The lifespan-side drain logic (``wait_for(task, drain_budget)`` →
     force-cancel) was never exercised. A regression that flipped to
     plain ``task.cancel()`` would still pass the existing tests.
  2. The in-flight test set ``stop_event`` from inside a respx
     side-effect that returned 200 immediately, so a hypothetical
     cancel-on-stop implementation could still pass it by sheer
     timing — the in-flight state was barely held.

This file plugs both gaps. The drain helper is now exposed at module
level (``feedgate.main._drain_background_task``) so we can unit-test
it directly without spinning up a FastAPI app, and the in-flight
test holds the side-effect on an explicit ``release`` event so the
fetch is provably mid-flight when shutdown fires.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi import FastAPI
from httpx import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate_fetcher.fetcher import scheduler
from feedgate_fetcher.main import _drain_background_task
from feedgate_fetcher.models import Feed

# ---------------------------------------------------------------------------
# Unit tests on _drain_background_task — no scheduler, no DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_background_task_returns_for_none_task() -> None:
    """A None task is a no-op — exercised by the lifespan when the
    feature flag for that loop is disabled."""
    stop = asyncio.Event()
    await _drain_background_task(None, stop, name="scheduler", drain_seconds=1.0)
    assert not stop.is_set(), "must not touch the event for a None task"


@pytest.mark.asyncio
async def test_drain_background_task_clean_exit_within_budget() -> None:
    """A well-behaved loop that exits as soon as the stop event fires
    must NOT trigger the force-cancel branch."""
    stop = asyncio.Event()

    async def loop() -> None:
        # Mimic scheduler.run: wait for stop, then return.
        await stop.wait()

    task = asyncio.create_task(loop())
    await _drain_background_task(task, stop, name="scheduler", drain_seconds=1.0)

    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_drain_background_task_force_cancels_on_timeout() -> None:
    """A misbehaving loop that ignores the stop event must be force-
    cancelled after the drain budget elapses. The cancel side-effect
    is the load-bearing assertion — we deliberately don't depend on
    caplog because pytest log-capture interacts oddly with how other
    tests in the suite reconfigure the root logger."""
    stop = asyncio.Event()

    async def hung_loop() -> None:
        # Never returns. ``stop`` is set but the loop ignores it.
        await asyncio.sleep(60)

    task = asyncio.create_task(hung_loop())

    await _drain_background_task(
        task,
        stop,
        name="scheduler",
        drain_seconds=0.05,
    )

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_drain_background_task_swallows_loop_exception() -> None:
    """If the loop raises during drain (not on cancel), the helper
    must absorb it and return — the lifespan exit MUST NOT propagate
    background task exceptions or the process will not shut down
    cleanly."""
    stop = asyncio.Event()

    async def boom() -> None:
        await stop.wait()
        raise RuntimeError("kaboom")

    task = asyncio.create_task(boom())

    # The helper must NOT raise here. If it does, this line propagates
    # and the test fails with the original RuntimeError, which is the
    # exact behavior we are guarding against.
    await _drain_background_task(
        task,
        stop,
        name="scheduler",
        drain_seconds=1.0,
    )

    assert task.done()


# ---------------------------------------------------------------------------
# End-to-end drain on a real scheduler.run with a HELD in-flight fetch.
# ---------------------------------------------------------------------------


def _atom_with(guid: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>drain held</title>
  <id>{guid}</id>
  <updated>2026-04-12T00:00:00Z</updated>
  <entry>
    <title>x</title>
    <id>{guid}/post-1</id>
    <link href="{guid}/post-1"/>
    <published>2026-04-12T00:00:00Z</published>
    <content>x</content>
  </entry>
</feed>
""".encode()


@pytest.mark.asyncio
async def test_drain_waits_for_truly_in_flight_fetch_to_complete(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Stronger version of the existing in-flight drain test. The
    side-effect blocks on an explicit ``release`` event so the fetch
    is *provably* mid-flight when shutdown fires. A cancel-on-stop
    implementation would interrupt the fetch and the feed would
    never get ``last_successful_fetch_at``; the cooperative drain
    sees the fetch through to completion.

    Sequence:
      1. Spawn ``scheduler.run(stop_event=stop)`` as a task.
      2. ``in_flight`` event fires when the side-effect enters →
         we KNOW the fetch is currently inside http_client.stream.
      3. Set ``stop`` while the fetch is held.
      4. Briefly assert the loop has NOT exited yet (still draining).
      5. Release the side-effect.
      6. Loop drains, returns. Assert the feed was fully fetched.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    fetch_app.state.fetch_interval_seconds = 0.05
    feed_url = "http://t.test/held-drain/feed"
    seed_now = datetime.now(UTC) - timedelta(seconds=5)
    async with sf() as session:
        session.add(Feed(url=feed_url, effective_url=feed_url, next_fetch_at=seed_now))
        await session.commit()

    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def held_response(request: Request) -> Response:
        in_flight.set()
        await release.wait()
        return Response(
            200,
            content=_atom_with(feed_url),
            headers={"Content-Type": "application/atom+xml"},
        )

    respx_mock.get(feed_url).mock(side_effect=held_response)

    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(fetch_app, stop_event=stop))

    # Wait until the fetch is REALLY mid-call.
    await asyncio.wait_for(in_flight.wait(), timeout=2.0)

    # Signal shutdown while the fetch is still held.
    stop.set()
    # Give the loop a few ticks of the event loop to observe stop.
    await asyncio.sleep(0.05)
    assert not task.done(), "loop exited before the held fetch was released"

    # Now let the fetch complete.
    release.set()

    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == feed_url))).scalar_one()
    assert feed.last_successful_fetch_at is not None, (
        "drain interrupted the in-flight fetch — cooperative drain is broken"
    )
    assert feed.last_error_code is None
