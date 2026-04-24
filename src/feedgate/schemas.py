"""Pydantic request/response schemas.

Shapes the API contract defined in ADR 002 and docs/spec/feed.md /
docs/spec/entry.md. Feed responses carry the full lifecycle field set
(status, last_successful_fetch_at, last_attempt_at, last_error_code,
effective_url) so clients can judge feed health without a second call.
Entry responses carry both `fetched_at` (first-seen, retention clock)
and `content_updated_at` (latest edit) so clients can distinguish new
entries from edited ones.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from feedgate.models import ErrorCode, FeedStatus


class FeedCreate(BaseModel):
    url: str = Field(..., min_length=1)


class FeedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    effective_url: str
    title: str | None
    status: FeedStatus
    last_successful_fetch_at: datetime | None
    last_attempt_at: datetime | None
    last_error_code: ErrorCode | None
    created_at: datetime


class PaginatedFeeds(BaseModel):
    items: list[FeedResponse]
    next_cursor: str | None = None


class EntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    guid: str
    feed_id: int
    url: str
    title: str | None
    content: str | None
    author: str | None
    published_at: datetime | None
    fetched_at: datetime
    content_updated_at: datetime


class PaginatedEntries(BaseModel):
    items: list[EntryResponse]
    next_cursor: str | None = None
