"""scheduler.tick_once integration test (Phase 4 WP 4.2).

Seeds multiple active feeds, mocks their URLs, runs a single tick,
and verifies every feed got fetched and its entries stored.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi import FastAPI
from httpx import Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.fetcher import scheduler
from feedgate.fetcher.scheduler import _claim_due_feeds
from feedgate.models import Entry, Feed


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


@pytest.mark.asyncio
async def test_tick_once_fetches_all_active_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory

    # Seed three active feeds and one "inactive" feed that should be skipped.
    feed_urls = [
        "http://t.test/a/feed",
        "http://t.test/b/feed",
        "http://t.test/c/feed",
    ]
    inactive_url = "http://t.test/x/feed"

    async with sf() as session:
        for u in feed_urls:
            session.add(Feed(url=u, effective_url=u))
        session.add(
            Feed(
                url=inactive_url,
                effective_url=inactive_url,
                status="dead",
            )
        )
        await session.commit()

    # Mock the three active feed URLs
    for url in feed_urls:
        guid = url + "/post-1"
        respx_mock.get(url).mock(
            return_value=Response(
                200,
                content=_atom_with(guid, f"post for {url}"),
                headers={"Content-Type": "application/atom+xml"},
            )
        )
    # Inactive URL should never be called — don't mock it.

    await scheduler.tick_once(fetch_app)

    # All three active feeds should have 1 entry each.
    async with sf() as session:
        entry_count_total = int(
            (await session.execute(select(func.count()).select_from(Entry))).scalar_one()
        )
        assert entry_count_total == 3

        # Each active feed has its last_successful_fetch_at set.
        result = await session.execute(
            select(
                Feed.url,
                Feed.last_successful_fetch_at,
                Feed.status,
                Feed.consecutive_failures,
            )
        )
        by_url = {row.url: row for row in result}

    for u in feed_urls:
        row = by_url[u]
        assert row.last_successful_fetch_at is not None
        assert row.status == "active"
        assert row.consecutive_failures == 0

    inactive = by_url[inactive_url]
    assert inactive.last_successful_fetch_at is None
    assert inactive.status == "dead"


@pytest.mark.asyncio
async def test_tick_once_with_no_active_feeds_is_noop(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    # No feeds in the DB. Should not raise or issue any requests.
    await scheduler.tick_once(fetch_app)


@pytest.mark.asyncio
async def test_tick_once_continues_when_one_feed_fails(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    ok_url = "http://t.test/ok/feed"
    bad_url = "http://t.test/bad/feed"

    async with sf() as session:
        session.add(Feed(url=ok_url, effective_url=ok_url))
        session.add(Feed(url=bad_url, effective_url=bad_url))
        await session.commit()

    respx_mock.get(ok_url).mock(
        return_value=Response(
            200,
            content=_atom_with(ok_url + "/1", "ok post"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )
    respx_mock.get(bad_url).mock(return_value=Response(500))

    await scheduler.tick_once(fetch_app)

    async with sf() as session:
        ok = (await session.execute(select(Feed).where(Feed.url == ok_url))).scalar_one()
        bad = (await session.execute(select(Feed).where(Feed.url == bad_url))).scalar_one()

    assert ok.last_successful_fetch_at is not None
    assert ok.last_error_code is None
    assert ok.consecutive_failures == 0

    assert bad.last_successful_fetch_at is None
    assert bad.last_error_code == "http_5xx"
    assert bad.consecutive_failures == 1


@pytest.mark.asyncio
async def test_tick_once_skips_non_due_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Non-dead feeds whose ``next_fetch_at`` is in the future must
    be skipped so that the exponential backoff on broken feeds is
    actually honored."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    due_url = "http://t.test/due/feed"
    future_url = "http://t.test/future/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        session.add(
            Feed(
                url=due_url,
                effective_url=due_url,
                next_fetch_at=now - timedelta(seconds=5),  # due
            )
        )
        session.add(
            Feed(
                url=future_url,
                effective_url=future_url,
                next_fetch_at=now + timedelta(hours=1),  # not due
            )
        )
        await session.commit()

    # Only the due URL is mocked — if tick_once wrongly tried the
    # future feed, respx would raise unmatched-request.
    respx_mock.get(due_url).mock(
        return_value=Response(
            200,
            content=_atom_with(due_url + "/1", "due post"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        due_feed = (await session.execute(select(Feed).where(Feed.url == due_url))).scalar_one()
        future_feed = (
            await session.execute(select(Feed).where(Feed.url == future_url))
        ).scalar_one()

    # Due feed was fetched
    assert due_feed.last_successful_fetch_at is not None
    # Future feed was NOT fetched — last_attempt_at still untouched
    assert future_feed.last_attempt_at is None


@pytest.mark.asyncio
async def test_tick_once_probes_stale_dead_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Dead feeds whose ``last_attempt_at`` is older than the probe
    interval must be fetched. A successful probe returns the feed
    to ``active`` via fetch_one's success path."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    dead_url = "http://t.test/stale-dead/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    stale_attempt = now - timedelta(days=8)  # > 7 day probe interval

    async with sf() as session:
        session.add(
            Feed(
                url=dead_url,
                effective_url=dead_url,
                status="dead",
                last_attempt_at=stale_attempt,
                last_error_code="http_4xx",
                consecutive_failures=50,
            )
        )
        await session.commit()

    respx_mock.get(dead_url).mock(
        return_value=Response(
            200,
            content=_atom_with(dead_url + "/1", "revived"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        revived = (await session.execute(select(Feed).where(Feed.url == dead_url))).scalar_one()

    assert revived.status == "active"  # probe succeeded -> resurrection
    assert revived.consecutive_failures == 0
    assert revived.last_error_code is None
    assert revived.last_successful_fetch_at is not None


@pytest.mark.asyncio
async def test_tick_once_skips_recently_probed_dead_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Dead feeds whose ``last_attempt_at`` is WITHIN the probe
    interval must be skipped entirely. No request should be issued."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    dead_url = "http://t.test/fresh-dead/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    fresh_attempt = now - timedelta(hours=12)  # well under 7 days

    async with sf() as session:
        session.add(
            Feed(
                url=dead_url,
                effective_url=dead_url,
                status="dead",
                last_attempt_at=fresh_attempt,
                last_error_code="http_410",
                consecutive_failures=1,
            )
        )
        await session.commit()

    # Deliberately no mock — if tick_once hit this URL, respx would
    # raise unmatched-request. The assertion is "tick returns cleanly
    # and the feed state is untouched".
    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        still_dead = (await session.execute(select(Feed).where(Feed.url == dead_url))).scalar_one()

    assert still_dead.status == "dead"
    assert still_dead.last_attempt_at == fresh_attempt  # untouched
    assert still_dead.last_error_code == "http_410"


async def _seed_due_feeds(
    sf: async_sessionmaker[AsyncSession],
    urls: list[str],
    now: datetime,
) -> None:
    async with sf() as session:
        for u in urls:
            session.add(
                Feed(
                    url=u,
                    effective_url=u,
                    next_fetch_at=now - timedelta(seconds=5),
                )
            )
        await session.commit()


def _make_barrier_recorder() -> tuple[
    dict[str, int],
    asyncio.Event,
    Callable[[Request], Awaitable[Response]],
]:
    """Build a respx side-effect that tracks how many calls are
    simultaneously in flight via a counter, AND signals an
    ``asyncio.Event`` the moment two callers overlap.

    Each call increments ``in_flight``, records the running max,
    sets the event when two callers are inside the side-effect at
    the same time, then waits for the event with a small timeout
    before returning. This makes the parallel-vs-serialized assertion
    deterministic regardless of CI scheduling jitter:

      * If the per-host throttle lets both callers in, the second
        call sets the event and both return immediately — observed
        ``max_in_flight == 2``, event ``is_set``.
      * If the throttle serializes them, only one is ever inside the
        side-effect, the event never fires, and the wait_for inside
        the recorder times out — observed ``max_in_flight == 1``,
        event NOT set. The 0.1s ceiling per call keeps the test
        well under one second total.
    """
    state: dict[str, int] = {"in_flight": 0, "max_in_flight": 0}
    both_in_flight = asyncio.Event()

    async def recorder(request: Request) -> Response:
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        if state["in_flight"] >= 2:
            both_in_flight.set()
        # Wait long enough that a slow CI runner reliably lets the
        # second coroutine walk through _process_feed (DB query, host
        # sem, validate_public_url, respx dispatch) and reach the
        # recorder while the first is still holding. 0.1s was too
        # tight — the first caller timed out alone and the event
        # never fired in the parallel-hosts test.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_in_flight.wait(), timeout=1.0)
        state["in_flight"] -= 1
        guid = str(request.url) + "/post-1"
        return Response(
            200,
            content=_atom_with(guid, "throttle"),
            headers={"Content-Type": "application/atom+xml"},
        )

    return state, both_in_flight, recorder


@pytest.mark.asyncio
async def test_per_host_throttle_serializes_same_host_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Two feeds on the same host must NEVER overlap inside the HTTP
    side-effect. The barrier event the recorder uses is wired so that
    a second concurrent caller would *immediately* set it; if the
    per-host semaphore is doing its job, the second caller is blocked
    in scheduler land and never reaches the side-effect, so the event
    stays clear and ``max_in_flight`` stays at 1."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    url_a = "http://same.test/a/feed"
    url_b = "http://same.test/b/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    await _seed_due_feeds(sf, [url_a, url_b], now)

    state, both_in_flight, recorder = _make_barrier_recorder()
    respx_mock.get(url_a).mock(side_effect=recorder)
    respx_mock.get(url_b).mock(side_effect=recorder)

    await scheduler.tick_once(fetch_app, now=now)

    assert state["max_in_flight"] == 1, (
        f"per-host throttle leaked: max_in_flight={state['max_in_flight']}"
    )
    assert not both_in_flight.is_set(), (
        "barrier event fired — two callers were inside the side-effect simultaneously"
    )


@pytest.mark.asyncio
async def test_per_host_throttle_allows_distinct_hosts_in_parallel(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """The per-host cap must NOT serialize feeds on *different* hosts.
    Cross-host parallelism is the property the global ``fetch_concurrency``
    knob exists to provide; if this regresses, throughput collapses to
    one feed at a time on a healthy multi-origin batch.

    The barrier event makes the assertion deterministic: as soon as
    a second caller enters the side-effect, the event fires and both
    return. If the loop happens to enter them in strict series, the
    event never fires and the test fails loudly."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    url_a = "http://host-a.test/feed"
    url_b = "http://host-b.test/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    await _seed_due_feeds(sf, [url_a, url_b], now)

    state, both_in_flight, recorder = _make_barrier_recorder()
    respx_mock.get(url_a).mock(side_effect=recorder)
    respx_mock.get(url_b).mock(side_effect=recorder)

    await scheduler.tick_once(fetch_app, now=now)

    assert both_in_flight.is_set(), "barrier event never fired — distinct hosts were serialized"
    assert state["max_in_flight"] == 2, (
        f"distinct hosts wrongly serialized: max_in_flight={state['max_in_flight']}"
    )


@pytest.mark.asyncio
async def test_claim_due_feeds_skip_locked_prevents_double_claim(
    fetch_app: FastAPI,
) -> None:
    """Two concurrent workers must never claim the same feed.

    We stage a single due feed, then fire two overlapping claim
    transactions. The first worker acquires ``FOR UPDATE`` on the
    row; the second hits ``SKIP LOCKED`` and sees an empty set. This
    is the core correctness test for the Postgres-as-queue model —
    if it ever regresses, two worker replicas will double-fetch
    every due feed on every tick.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/skip-locked-race/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        session.add(
            Feed(
                url=feed_url,
                effective_url=feed_url,
                next_fetch_at=now - timedelta(seconds=5),
            )
        )
        await session.commit()

    # Barrier: worker A acquires the row lock, THEN signals worker B
    # to attempt its own claim. B runs while A's transaction is still
    # open, so A's FOR UPDATE is in effect and B must SKIP LOCKED it.
    a_has_locked = asyncio.Event()
    b_is_done = asyncio.Event()

    async def worker_a() -> list[int]:
        async with sf() as session:
            claimed = await _claim_due_feeds(
                session,
                now=now,
                claim_batch_size=8,
                claim_ttl_seconds=180,
                dead_probe_interval_days=7,
            )
            # A now holds FOR UPDATE on the selected rows. Release
            # control so worker B can run its query under the lock.
            a_has_locked.set()
            await b_is_done.wait()
            await session.commit()
            return claimed

    async def worker_b() -> list[int]:
        await a_has_locked.wait()
        async with sf() as session:
            claimed = await _claim_due_feeds(
                session,
                now=now,
                claim_batch_size=8,
                claim_ttl_seconds=180,
                dead_probe_interval_days=7,
            )
            await session.commit()
            b_is_done.set()
            return claimed

    a_result, b_result = await asyncio.gather(worker_a(), worker_b())

    # Worker A claimed the feed; worker B's SKIP LOCKED made it
    # invisible, so B returns an empty list. No double-claim.
    assert len(a_result) == 1
    assert b_result == []


@pytest.mark.asyncio
async def test_claim_due_feeds_advances_lease(
    fetch_app: FastAPI,
) -> None:
    """After a successful claim, the feed's ``next_fetch_at`` is
    advanced to ``now + claim_ttl_seconds`` and ``last_attempt_at``
    is set to ``now``. A subsequent claim call in a new transaction
    must now skip the feed because its gate timestamps are in the
    future / freshly attempted. This is what prevents re-claim
    across ticks, not just within one tick."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/lease-advance/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    claim_ttl = 180

    async with sf() as session:
        session.add(
            Feed(
                url=feed_url,
                effective_url=feed_url,
                next_fetch_at=now - timedelta(seconds=5),
            )
        )
        await session.commit()

    async with sf() as session:
        first = await _claim_due_feeds(
            session,
            now=now,
            claim_batch_size=8,
            claim_ttl_seconds=claim_ttl,
            dead_probe_interval_days=7,
        )
        await session.commit()

    assert len(first) == 1

    async with sf() as session:
        # Same `now` — the prior claim should have bumped both
        # timestamps so the gate filters this feed out.
        second = await _claim_due_feeds(
            session,
            now=now,
            claim_batch_size=8,
            claim_ttl_seconds=claim_ttl,
            dead_probe_interval_days=7,
        )
        await session.commit()

    assert second == []

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == feed_url))).scalar_one()
    assert feed.next_fetch_at == now + timedelta(seconds=claim_ttl)
    assert feed.last_attempt_at == now


# ---- graceful shutdown drain ------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_run_exits_cleanly_when_stop_event_is_set(
    fetch_app: FastAPI,
) -> None:
    """The background loop must respect ``stop_event`` and return
    cleanly without raising. Without this, lifespan shutdown can only
    rely on ``task.cancel()``, which interrupts in-flight fetches and
    leaves SKIP LOCKED claims dangling until the lease TTL elapses."""
    fetch_app.state.fetch_interval_seconds = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(fetch_app, stop_event=stop))

    # Let the loop spin a couple of (no-op) ticks so we know it is
    # really running, then signal stop.
    await asyncio.sleep(0.15)
    stop.set()

    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_scheduler_run_drains_in_flight_fetch_before_exit(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """If shutdown is signaled *while* a fetch is in flight, the
    drain path must let that fetch finish — the side-effect mock
    sets the stop event mid-call and we then assert the feed has
    ``last_successful_fetch_at`` populated. A naive cancel-on-stop
    implementation would interrupt fetch_one and leave the feed in
    its claimed-but-unreached state, failing this assertion."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    fetch_app.state.fetch_interval_seconds = 0.05
    feed_url = "http://t.test/drain/feed"
    now = datetime.now(UTC) - timedelta(seconds=5)
    async with sf() as session:
        session.add(Feed(url=feed_url, effective_url=feed_url, next_fetch_at=now))
        await session.commit()

    stop = asyncio.Event()

    async def stop_then_succeed(request: Request) -> Response:
        # Signal shutdown WHILE the fetch is in flight. The current
        # tick must still complete before the loop exits.
        stop.set()
        return Response(
            200,
            content=_atom_with(feed_url + "/post-1", "drained"),
            headers={"Content-Type": "application/atom+xml"},
        )

    respx_mock.get(feed_url).mock(side_effect=stop_then_succeed)

    task = asyncio.create_task(scheduler.run(fetch_app, stop_event=stop))
    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == feed_url))).scalar_one()
        entry_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Entry).where(Entry.feed_id == feed.id)
                )
            ).scalar_one()
        )
    assert feed.last_successful_fetch_at is not None, (
        "in-flight fetch was interrupted by shutdown — drain path is broken"
    )
    assert feed.last_error_code is None
    assert entry_count == 1
