"""Feed CRUD endpoints.

Implements POST/GET/DELETE for ``/v1/feeds`` per ADR 002 and
docs/spec/feed.md. POST is idempotent — reposting an already-registered
URL returns the existing feed with HTTP 200 (409 is intentionally NOT
used; see ADR 002).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate_fetcher.api import get_session
from feedgate_fetcher.config import get_settings
from feedgate_fetcher.models import Feed, FeedStatus
from feedgate_fetcher.schemas import FeedCreate, FeedResponse, PaginatedFeeds
from feedgate_fetcher.ssrf import BlockedURLError, validate_public_url

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _to_idna(host: str) -> str:
    """Convert an IDN host to its punycode form, lowercased on failure."""
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host.lower()


def normalize_url(raw: str) -> str:
    parts = urlsplit(raw.strip())

    scheme = parts.scheme.lower()
    host = _to_idna(parts.hostname or "")

    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None

    netloc = host
    if parts.username:
        cred = parts.username
        if parts.password:
            cred = f"{cred}:{parts.password}"
        netloc = f"{cred}@{netloc}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""

    return urlunsplit((scheme, netloc, path, parts.query, ""))

logger = structlog.get_logger()

router = APIRouter(prefix="/v1/feeds", tags=["feeds"])
limiter = Limiter(key_func=get_remote_address)


def _create_feed_rate_limit() -> str:
    return get_settings().api_rate_limit


def _encode_feed_cursor(feed_id: int) -> str:
    payload = {"i": feed_id}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_feed_cursor(cursor: str) -> int:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode())
        return int(payload["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid cursor",
        ) from exc


@router.post(
    "",
    response_model=FeedResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(_create_feed_rate_limit)
async def create_feed(
    request: Request,
    payload: FeedCreate,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    del request  # required by slowapi decorator
    url = normalize_url(payload.url)

    # SSRF guard: cheap check (scheme + IP literal). Hostname-resolution
    # check happens at fetch time so a flaky resolver cannot drop a
    # legitimate registration. ``http://10.0.0.1/feed`` is rejected here.
    try:
        await validate_public_url(url, resolve=False)
    except BlockedURLError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"blocked_url: {exc}",
        ) from exc

    stmt = (
        pg_insert(Feed)
        .values(url=url, effective_url=url)
        .on_conflict_do_nothing(index_elements=["url"])
        .returning(Feed.id)
    )
    result = await session.execute(stmt)
    new_id = result.scalar_one_or_none()

    # Load the row either way (newly inserted OR pre-existing).
    feed = (await session.execute(select(Feed).where(Feed.url == url))).scalar_one()
    if new_id is None:
        response.status_code = status.HTTP_200_OK
    return feed


@router.get("", response_model=PaginatedFeeds)
async def list_feeds(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[str | None, Query()] = None,
    limit: int = 50,
    status_filter: Annotated[
        FeedStatus | None,
        Query(
            alias="status",
            description="Filter by lifecycle state (active | broken | dead)",
        ),
    ] = None,
) -> PaginatedFeeds:
    max_limit = request.app.state.api_feeds_max_limit
    limit = max(1, min(limit, max_limit))
    stmt = select(Feed)
    if cursor is not None:
        cur_id = _decode_feed_cursor(cursor)
        stmt = stmt.where(Feed.id > cur_id)
    if status_filter is not None:
        stmt = stmt.where(Feed.status == status_filter)
    stmt = stmt.order_by(Feed.id.asc()).limit(limit + 1)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    has_more = len(rows) > limit
    feeds = list(rows[:limit])
    next_cursor: str | None = None
    if has_more and feeds:
        next_cursor = _encode_feed_cursor(feeds[-1].id)
    return PaginatedFeeds(
        items=[FeedResponse.model_validate(f) for f in feeds],
        next_cursor=next_cursor,
    )


@router.get("/{feed_id}", response_model=FeedResponse)
async def get_feed(
    feed_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    return feed


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feed(
    feed_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
    if feed is not None:
        await session.delete(feed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{feed_id}/reactivate", response_model=FeedResponse)
async def reactivate_feed(
    feed_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    """Manually flip any feed back to ``active`` (spec/feed.md).

    The primary use case is moving a ``dead`` feed back into the
    fetch rotation after an operator has confirmed the upstream is
    healthy again. Also works on a ``broken`` feed to skip the
    exponential backoff and force an immediate next tick.

    Semantics:
      * ``status`` -> ``'active'``
      * ``consecutive_failures`` -> ``0``
      * ``last_error_code`` -> ``None``
      * ``next_fetch_at`` -> ``now`` (picked up by the very next tick)
      * ``last_successful_fetch_at`` stays unchanged (we have not
        actually succeeded yet — a subsequent successful fetch will
        update it)
    """
    feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")

    if feed.status != FeedStatus.ACTIVE:
        logger.warning(
            "feed_id=%s url=%s state=%s->%s reason=%s",
            feed.id,
            feed.effective_url,
            feed.status,
            FeedStatus.ACTIVE,
            "manual_reactivate",
        )

    feed.status = FeedStatus.ACTIVE
    feed.consecutive_failures = 0
    feed.last_error_code = None
    feed.next_fetch_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(feed)
    return feed
