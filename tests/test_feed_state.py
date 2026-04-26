from datetime import UTC, datetime, timedelta
from typing import cast

from structlog.testing import capture_logs

from feedgate_fetcher.feed_state import mark_fetch_failure, mark_fetch_success, transition_feed
from feedgate_fetcher.metrics import FEED_STATE_TRANSITION_TOTAL
from feedgate_fetcher.models import ErrorCode, Feed, FeedStatus


class _StubFeed:
    id = 1
    effective_url = "http://t.test/feed"
    title: str | None = None
    status = FeedStatus.ACTIVE
    last_successful_fetch_at: datetime | None = None
    last_error_code: ErrorCode | None = None
    consecutive_failures = 0
    created_at = datetime(2026, 1, 1, tzinfo=UTC)


def test_transition_feed_logs_at_error_for_broken_and_dead() -> None:
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.ACTIVE

    with capture_logs() as logs:
        transition_feed(feed, FeedStatus.BROKEN, reason="test_broken")
        transition_feed(feed, FeedStatus.DEAD, reason="test_dead")

    assert [record.get("log_level") for record in logs] == ["error", "error"]


def test_mark_fetch_success_recovers_broken_feed_and_increments_counter() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.BROKEN
    feed.consecutive_failures = 3
    feed.last_error_code = ErrorCode.TIMEOUT
    before = FEED_STATE_TRANSITION_TOTAL.labels(
        from_status=FeedStatus.BROKEN.value,
        to_status=FeedStatus.ACTIVE.value,
        reason="fetch_succeeded",
    )._value.get()

    mark_fetch_success(feed, now=now, title="Recovered")

    after = FEED_STATE_TRANSITION_TOTAL.labels(
        from_status=FeedStatus.BROKEN.value,
        to_status=FeedStatus.ACTIVE.value,
        reason="fetch_succeeded",
    )._value.get()
    assert after - before == 1
    assert feed.status == FeedStatus.ACTIVE
    assert feed.consecutive_failures == 0
    assert feed.last_error_code is None
    assert feed.last_successful_fetch_at == now
    assert feed.title == "Recovered"


def test_mark_fetch_failure_http_410_marks_dead_immediately() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.ACTIVE
    feed.consecutive_failures = 0

    mark_fetch_failure(
        feed,
        now=now,
        code=ErrorCode.HTTP_410,
        broken_threshold=100,
        dead_duration_days=100,
    )

    assert feed.status == FeedStatus.DEAD
    assert feed.consecutive_failures == 1
    assert feed.last_error_code == ErrorCode.HTTP_410


def test_mark_fetch_failure_marks_broken_feed_dead_after_dead_duration() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.BROKEN
    feed.consecutive_failures = 3
    feed.last_successful_fetch_at = now - timedelta(days=8)

    mark_fetch_failure(
        feed,
        now=now,
        code=ErrorCode.TIMEOUT,
        broken_threshold=3,
        dead_duration_days=7,
    )

    assert feed.status == FeedStatus.DEAD
    assert feed.consecutive_failures == 4
    assert feed.last_error_code == ErrorCode.TIMEOUT
