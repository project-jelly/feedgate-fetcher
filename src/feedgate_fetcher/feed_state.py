"""Feed lifecycle state transition helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from feedgate_fetcher.metrics import FEED_STATE_TRANSITION_TOTAL
from feedgate_fetcher.models import ErrorCode, Feed, FeedStatus

logger = structlog.get_logger()


def transition_feed(feed: Feed, new_status: FeedStatus, *, reason: str) -> None:
    FEED_STATE_TRANSITION_TOTAL.labels(
        from_status=str(feed.status),
        to_status=str(new_status),
        reason=reason,
    ).inc()
    log = logger.error if new_status in {FeedStatus.BROKEN, FeedStatus.DEAD} else logger.warning
    log(
        "feed_state_transition",
        feed_id=feed.id,
        url=feed.effective_url,
        old_status=feed.status,
        new_status=new_status,
        reason=reason,
    )


def mark_fetch_success(
    feed: Feed,
    *,
    now: datetime,
    title: str | None,
) -> None:
    if title:
        feed.title = title
    feed.last_successful_fetch_at = now
    feed.last_error_code = None
    feed.consecutive_failures = 0
    if feed.status != FeedStatus.ACTIVE:
        transition_feed(feed, FeedStatus.ACTIVE, reason="fetch_succeeded")
        feed.status = FeedStatus.ACTIVE


def mark_fetch_failure(
    feed: Feed,
    *,
    now: datetime,
    code: ErrorCode,
    broken_threshold: int,
    dead_duration_days: int,
) -> None:
    feed.last_error_code = code
    feed.consecutive_failures = feed.consecutive_failures + 1

    if code == ErrorCode.HTTP_410:
        if feed.status != FeedStatus.DEAD:
            transition_feed(feed, FeedStatus.DEAD, reason="http_410")
            feed.status = FeedStatus.DEAD
    else:
        if feed.status == FeedStatus.ACTIVE and feed.consecutive_failures >= broken_threshold:
            transition_feed(
                feed,
                FeedStatus.BROKEN,
                reason=f"consecutive_failures>={broken_threshold}",
            )
            feed.status = FeedStatus.BROKEN
        if feed.status == FeedStatus.BROKEN:
            reference = feed.last_successful_fetch_at or feed.created_at
            if now - reference >= timedelta(days=dead_duration_days):
                transition_feed(
                    feed,
                    FeedStatus.DEAD,
                    reason=f"no_success_for_>={dead_duration_days}d",
                )
                feed.status = FeedStatus.DEAD
