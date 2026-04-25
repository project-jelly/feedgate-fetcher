"""curl_cffi 기반 impersonation fetch (TLS JA3 spoofing).

WAF가 httpx의 Python ssl 시그니처를 차단할 때 호출되는 fallback path.
SSRF 가드는 호출 시점에 직접 실행 (httpx의 SSRFGuardTransport를 못 쓰니까).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from feedgate_fetcher.ssrf import validate_public_url


@dataclass
class FallbackResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str


class FallbackError(Exception):
    """impersonation fetch가 실패하거나 본문이 너무 클 때 발생."""


async def fetch_via_impersonation(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_bytes: int,
) -> FallbackResponse:
    """Fetch with Chrome-impersonated TLS via curl_cffi."""
    # SSRF guard first
    await validate_public_url(url, resolve=True)

    try:
        import curl_cffi.requests as curl_requests
    except ImportError as exc:
        raise FallbackError("curl_cffi not installed") from exc

    try:
        async with curl_requests.AsyncSession(impersonate="chrome120") as session:
            resp = await session.get(
                url,
                headers=dict(headers),
                timeout=timeout_seconds,
                allow_redirects=True,
                max_redirects=10,
            )
    except Exception as exc:
        raise FallbackError(str(exc)) from exc

    content = resp.content
    if len(content) > max_bytes:
        # Import ResponseTooLargeError from http module (avoid circular: define inline)
        from feedgate_fetcher.fetcher.http import ResponseTooLargeError

        raise ResponseTooLargeError(f"fallback body too large: {len(content)} > {max_bytes}")

    raw_headers: list[tuple[bytes, bytes]] = []
    for k, v in (resp.headers or {}).items():
        raw_headers.append((k.encode(), v.encode()))

    return FallbackResponse(
        status_code=resp.status_code,
        headers=httpx.Headers(raw_headers),
        content=content,
        url=str(resp.url),
    )
