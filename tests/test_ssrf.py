"""SSRF protection — unit + integration coverage.

Maps to docs/spec/resilience.md threat category D and the
TC-D1..D4 entries in docs/tests/resilience-test-cases.md.

Layered defense:
  * ``validate_public_url`` rejects scheme/IP-literal/DNS-resolved
    blocked addresses.
  * ``POST /v1/feeds`` calls it with ``resolve=False`` so a private IP
    literal is rejected with ``400`` at registration time.
  * ``fetch_one`` calls it with ``resolve=True`` (the default), so a
    DNS-rebinding host registered earlier is blocked at fetch time.
  * ``SSRFGuardTransport`` re-runs the check on every request the
    httpx client makes — including redirect follow-ups, which are the
    only path that bypasses the fetch_one pre-check.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate import ssrf
from feedgate.fetcher.http import fetch_one
from feedgate.lifecycle import ErrorCode
from feedgate.models import Feed
from feedgate.ssrf import (
    BlockedURLError,
    SSRFGuardTransport,
    validate_public_url,
)

# ---------------------------------------------------------------------------
# Unit tests — validate_public_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/feed",  # TC-D1-01 RFC1918
        "http://172.16.0.1/feed",  # TC-D1-01 RFC1918
        "http://192.168.1.1/feed",  # TC-D1-01 RFC1918
        "http://127.0.0.1/feed",  # TC-D1-02 loopback
        "http://[::1]/feed",  # TC-D1-02 loopback v6
        "http://169.254.169.254/latest/meta-data/",  # TC-D2-01 cloud metadata
        "http://0.0.0.0/feed",  # unspecified
        "http://[fe80::1]/feed",  # link-local v6
    ],
)
async def test_validate_blocks_private_ip_literal(url: str) -> None:
    with pytest.raises(BlockedURLError):
        await validate_public_url(url, resolve=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://internal.host/",
        "ftp://internal.host/",
        "javascript:alert(1)",
    ],
)
async def test_validate_blocks_unsupported_scheme(url: str) -> None:
    with pytest.raises(BlockedURLError):
        await validate_public_url(url, resolve=False)


@pytest.mark.asyncio
async def test_validate_rejects_missing_host() -> None:
    with pytest.raises(BlockedURLError):
        await validate_public_url("http:///feed", resolve=False)


@pytest.mark.asyncio
async def test_validate_allows_public_ip_literal() -> None:
    await validate_public_url("http://8.8.8.8/feed", resolve=False)
    await validate_public_url("https://1.1.1.1/", resolve=False)


@pytest.mark.asyncio
async def test_validate_skips_dns_when_resolve_false() -> None:
    """``resolve=False`` must not invoke ``_resolve``. We assert by
    swapping in a sentinel that would explode if called — this is the
    contract POST /v1/feeds depends on so registration latency stays
    bounded and tests do not need a network resolver."""

    async def explode(host: str) -> list[str]:  # pragma: no cover - guard
        raise AssertionError(f"_resolve was called for {host}")

    original = ssrf._resolve
    ssrf._resolve = explode  # type: ignore[assignment]
    try:
        await validate_public_url("http://example.com/feed", resolve=False)
    finally:
        ssrf._resolve = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_validate_blocks_dns_rebinding_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-D3-01: a perfectly innocent-looking hostname whose DNS
    record points at ``10.x`` must be rejected at fetch time."""

    async def fake_resolve(host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    with pytest.raises(BlockedURLError, match="resolves to blocked"):
        await validate_public_url("http://attacker.example.com/feed")


@pytest.mark.asyncio
async def test_validate_blocks_when_any_resolved_addr_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a hostname returns multiple A records and *any one* is
    private, the URL is blocked. This catches the dual-stack rebinding
    trick where one record is public and another is internal."""

    async def fake_resolve(host: str) -> list[str]:
        return ["8.8.8.8", "10.0.0.5"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    with pytest.raises(BlockedURLError):
        await validate_public_url("http://mixed.example.com/feed")


@pytest.mark.asyncio
async def test_validate_allows_public_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    await validate_public_url("http://example.com/feed")


@pytest.mark.asyncio
async def test_validate_allows_unresolvable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution failure (NXDOMAIN, no A record) is *allowed*. The
    fetch will fail naturally and we do not want a flaky resolver to
    drop registrations. The other layers still apply."""

    async def fake_resolve(host: str) -> list[str]:
        return []

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    await validate_public_url("http://no-such-host.test/feed")


# ---------------------------------------------------------------------------
# Unit tests — SSRFGuardTransport
# ---------------------------------------------------------------------------


class _SpyTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(200)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_transport_guard_blocks_private_request_before_inner() -> None:
    inner = _SpyTransport()
    guard = SSRFGuardTransport(inner)
    request = httpx.Request("GET", "http://10.0.0.1/feed")

    with pytest.raises(BlockedURLError):
        await guard.handle_async_request(request)
    assert inner.calls == []  # inner transport was never reached


@pytest.mark.asyncio
async def test_transport_guard_passes_public_request_to_inner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    inner = _SpyTransport()
    guard = SSRFGuardTransport(inner)
    request = httpx.Request("GET", "http://example.com/feed")

    response = await guard.handle_async_request(request)
    assert response.status_code == 200
    assert inner.calls == ["http://example.com/feed"]


@pytest.mark.asyncio
async def test_transport_guard_blocks_redirect_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-D4-01: a public URL that 302s to a private IP must be
    blocked at the **second** request, before the inner transport is
    even asked. We simulate httpx's redirect-follow loop by hand by
    pushing two requests through the same guard.
    """

    resolved_addrs = {
        "public.example.com": ["93.184.216.34"],
        "internal.example.com": ["10.0.0.5"],
    }

    async def fake_resolve(host: str) -> list[str]:
        return resolved_addrs.get(host, [])

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)

    inner = _SpyTransport()
    guard = SSRFGuardTransport(inner)

    # First hop is public — should pass and reach inner.
    first = httpx.Request("GET", "http://public.example.com/feed")
    await guard.handle_async_request(first)
    assert inner.calls == ["http://public.example.com/feed"]

    # Second hop (the redirect httpx would follow) goes to a private
    # host. The guard refuses it without invoking the inner transport.
    second = httpx.Request("GET", "http://internal.example.com/feed")
    with pytest.raises(BlockedURLError):
        await guard.handle_async_request(second)
    assert inner.calls == ["http://public.example.com/feed"]  # unchanged


# ---------------------------------------------------------------------------
# Integration — POST /v1/feeds rejects blocked URLs with 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/feed",  # TC-D1-01
        "http://127.0.0.1/feed",  # TC-D1-02
        "http://169.254.169.254/latest/meta-data/",  # TC-D2-01
        "file:///etc/passwd",
    ],
)
async def test_post_feed_rejects_blocked_url(
    api_client: AsyncClient,
    url: str,
) -> None:
    resp = await api_client.post("/v1/feeds", json={"url": url})
    assert resp.status_code == 400, resp.text
    assert "blocked_url" in resp.text


@pytest.mark.asyncio
async def test_post_feed_blocked_url_does_not_create_row(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Defense check: a rejected POST must NOT leave a Feed row behind.
    A future PR could accidentally swap the validation past the
    insert; this test would catch that regression."""
    resp = await api_client.post(
        "/v1/feeds",
        json={"url": "http://10.1.2.3/feed"},
    )
    assert resp.status_code == 400
    async with async_session_factory() as session:
        rows = (await session.execute(select(Feed))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# Integration — fetch_one pre-flight catches DNS rebinding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_one_marks_blocked_when_host_resolves_to_private_ip(
    fetch_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a feed registered with a benign hostname whose DNS
    later flips to ``10.x`` must be marked with ``last_error_code =
    'blocked'`` and never reach the HTTP client. We do not mock the
    URL with respx — if the SSRF pre-check failed to fire, respx
    would raise unmatched-request and the test would still fail loud.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    rebound_url = "http://rebound.example.com/feed"

    async with sf() as session:
        session.add(Feed(url=rebound_url, effective_url=rebound_url))
        await session.commit()

    async def fake_resolve(host: str) -> list[str]:
        return ["10.0.0.42"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == rebound_url))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC),
            interval_seconds=fetch_app.state.fetch_interval_seconds,
            user_agent=fetch_app.state.fetch_user_agent,
            max_bytes=fetch_app.state.fetch_max_bytes,
            max_entries_initial=fetch_app.state.fetch_max_entries_initial,
            broken_threshold=fetch_app.state.broken_threshold,
            dead_duration_days=fetch_app.state.dead_duration_days,
            broken_max_backoff_seconds=fetch_app.state.broken_max_backoff_seconds,
            backoff_jitter_ratio=fetch_app.state.backoff_jitter_ratio,
        )
        await session.commit()

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == rebound_url))).scalar_one()
    assert feed.last_error_code == ErrorCode.BLOCKED
    assert feed.last_successful_fetch_at is None
    assert feed.consecutive_failures == 1
