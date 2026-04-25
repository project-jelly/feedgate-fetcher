from __future__ import annotations

import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Any

import httpx
import pytest

from feedgate_fetcher.fetcher.fallback import (
    FallbackResponse,
    fetch_via_impersonation,
)
from feedgate_fetcher.fetcher.http import ResponseTooLargeError


class _StubResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"<rss/>",
        headers: Mapping[str, str] | None = None,
        url: str = "http://x.test/feed",
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.url = url


def _session_factory(response: _StubResponse) -> type[Any]:
    class StubSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> StubSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, *args: object, **kwargs: object) -> _StubResponse:
            return response

    return StubSession


def _install_stub_curl_requests(
    monkeypatch: pytest.MonkeyPatch,
    response: _StubResponse,
) -> None:
    curl_cffi = ModuleType("curl_cffi")
    curl_requests = ModuleType("curl_cffi.requests")
    curl_requests.AsyncSession = _session_factory(response)  # type: ignore[attr-defined]
    curl_cffi.requests = curl_requests  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", curl_cffi)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", curl_requests)


@pytest.mark.asyncio
async def test_fetch_via_impersonation_calls_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    async def spy_validate_public_url(url: str, *, resolve: bool = False) -> None:
        calls.append((url, resolve))

    monkeypatch.setattr(
        "feedgate_fetcher.fetcher.fallback.validate_public_url",
        spy_validate_public_url,
    )
    _install_stub_curl_requests(monkeypatch, _StubResponse())

    await fetch_via_impersonation(
        "http://x.test/feed",
        headers={},
        timeout_seconds=1.0,
        max_bytes=100,
    )

    assert calls == [("http://x.test/feed", True)]


@pytest.mark.asyncio
async def test_fetch_via_impersonation_raises_for_oversize_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_validate_public_url(url: str, *, resolve: bool = False) -> None:
        return None

    monkeypatch.setattr(
        "feedgate_fetcher.fetcher.fallback.validate_public_url",
        noop_validate_public_url,
    )
    _install_stub_curl_requests(monkeypatch, _StubResponse(content=b"x" * 1000))

    with pytest.raises(ResponseTooLargeError):
        await fetch_via_impersonation(
            "http://x.test/feed",
            headers={},
            timeout_seconds=1.0,
            max_bytes=100,
        )


@pytest.mark.asyncio
async def test_fetch_via_impersonation_returns_normalized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_validate_public_url(url: str, *, resolve: bool = False) -> None:
        return None

    monkeypatch.setattr(
        "feedgate_fetcher.fetcher.fallback.validate_public_url",
        noop_validate_public_url,
    )
    _install_stub_curl_requests(
        monkeypatch,
        _StubResponse(
            content=b"<rss>ok</rss>",
            headers={"Content-Type": "application/rss+xml"},
            url="http://x.test/feed",
        ),
    )

    result = await fetch_via_impersonation(
        "http://x.test/feed",
        headers={},
        timeout_seconds=1.0,
        max_bytes=100,
    )

    assert isinstance(result, FallbackResponse)
    assert result.status_code == 200
    assert result.content == b"<rss>ok</rss>"
    assert result.url == "http://x.test/feed"
    assert isinstance(result.headers, httpx.Headers)
    assert result.headers["content-type"] == "application/rss+xml"
