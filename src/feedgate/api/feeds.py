"""Feed CRUD endpoints.

Implements POST/GET/DELETE for ``/v1/feeds`` per ADR 002 and
docs/spec/feed.md. POST is idempotent — reposting an already-registered
URL returns the existing feed with HTTP 200 (409 is intentionally NOT
used; see ADR 002).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.api import get_session
from feedgate.models import Feed
from feedgate.schemas import FeedCreate, FeedResponse, PaginatedFeeds
from feedgate.urlnorm import normalize_url

router = APIRouter(prefix="/v1/feeds", tags=["feeds"])


@router.post(
    "",
    response_model=FeedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_feed(
    payload: FeedCreate,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    url = normalize_url(payload.url)

    # Idempotent: if already registered, return existing row with 200.
    existing = (await session.execute(select(Feed).where(Feed.url == url))).scalar_one_or_none()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    feed = Feed(url=url, effective_url=url)
    session.add(feed)
    await session.flush()
    await session.refresh(feed)
    return feed


@router.get("", response_model=PaginatedFeeds)
async def list_feeds(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> PaginatedFeeds:
    limit = max(1, min(limit, 200))
    result = await session.execute(select(Feed).order_by(Feed.id.asc()).limit(limit))
    feeds = result.scalars().all()
    return PaginatedFeeds(
        items=[FeedResponse.model_validate(f) for f in feeds],
        next_cursor=None,
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
