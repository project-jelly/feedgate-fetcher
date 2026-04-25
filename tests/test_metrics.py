from typing import cast

from structlog.testing import capture_logs

from feedgate_fetcher.fetcher.http import _log_transition
from feedgate_fetcher.metrics import FEED_STATE_TRANSITION_TOTAL
from feedgate_fetcher.models import Feed, FeedStatus


class _StubFeed:
    id = 1
    effective_url = "http://t.test/feed"
    status = FeedStatus.ACTIVE


def test_feed_state_transition_counter_increments_on_broken() -> None:
    before = FEED_STATE_TRANSITION_TOTAL.labels(
        from_status=FeedStatus.ACTIVE.value,
        to_status=FeedStatus.BROKEN.value,
        reason="consecutive_failures",
    )._value.get()
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.ACTIVE
    _log_transition(feed, FeedStatus.BROKEN, reason="consecutive_failures")
    after = FEED_STATE_TRANSITION_TOTAL.labels(
        from_status=FeedStatus.ACTIVE.value,
        to_status=FeedStatus.BROKEN.value,
        reason="consecutive_failures",
    )._value.get()
    assert after - before == 1


def test_feed_state_transition_counter_logs_at_error_for_broken() -> None:
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.ACTIVE
    with capture_logs() as logs:
        _log_transition(feed, FeedStatus.BROKEN, reason="consecutive_failures")
    assert any(record.get("log_level") == "error" for record in logs)


def test_feed_state_transition_counter_logs_at_warning_for_active_recovery() -> None:
    feed = cast(Feed, _StubFeed())
    feed.status = FeedStatus.BROKEN
    with capture_logs() as logs:
        _log_transition(feed, FeedStatus.ACTIVE, reason="fetch_succeeded")
    assert any(record.get("log_level") == "warning" for record in logs)
    assert not any(record.get("log_level") == "error" for record in logs)
