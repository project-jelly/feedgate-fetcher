"""Entry listing endpoint with keyset pagination.

Contract per ADR 002:
  * ``feed_ids`` query parameter is REQUIRED (no global scan).
  * Ordering is ``(published_at DESC, id DESC)``; the compound index
    on ``entries`` was created for exactly this sort.
  * Pagination is keyset-based; the ``cursor`` parameter is an opaque
    string. Clients must not interpret it.

Entries are cached and upserted, so pagination is best-effort under
edits — clients should dedupe by ``guid`` (ADR 002).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.api import get_session
from feedgate.models import Entry
from feedgate.schemas import EntryResponse, PaginatedEntries

router = APIRouter(prefix="/v1/entries", tags=["entries"])

MAX_FEED_IDS = 200
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _encode_cursor(published_at: datetime | None, entry_id: int) -> str:
    payload = {
        "p": published_at.isoformat() if published_at is not None else None,
        "i": entry_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime | None, int]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode())
        p_raw = payload.get("p")
        pub = datetime.fromisoformat(p_raw) if p_raw else None
        return pub, int(payload["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid cursor",
        ) from exc


@router.get("", response_model=PaginatedEntries)
async def list_entries(
    session: Annotated[AsyncSession, Depends(get_session)],
    feed_ids: Annotated[str, Query(..., description="comma-separated feed ids")],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> PaginatedEntries:
    try:
        feed_id_list = [int(x) for x in feed_ids.split(",") if x]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid feed_ids",
        ) from exc

    if not feed_id_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feed_ids is required",
        )
    if len(feed_id_list) > MAX_FEED_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"feed_ids length exceeds {MAX_FEED_IDS}",
        )

    stmt = select(Entry).where(Entry.feed_id.in_(feed_id_list))

    if cursor is not None:
        cur_pub, cur_id = _decode_cursor(cursor)
        # Keyset "after": tuples strictly less than (cur_pub, cur_id) in
        # the sort order `(published_at DESC, id DESC)`.
        if cur_pub is None:
            stmt = stmt.where(and_(Entry.published_at.is_(None), Entry.id < cur_id))
        else:
            stmt = stmt.where(
                or_(
                    Entry.published_at < cur_pub,
                    and_(Entry.published_at == cur_pub, Entry.id < cur_id),
                )
            )

    stmt = stmt.order_by(Entry.published_at.desc(), Entry.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = list(rows[:limit])

    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.published_at, last.id)

    return PaginatedEntries(
        items=[EntryResponse.model_validate(e) for e in items],
        next_cursor=next_cursor,
    )
