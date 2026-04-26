"""HTTP response policy helpers for fetch scheduling and headers."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from feedgate_fetcher.models import Feed, FeedStatus


def compute_next_fetch_at(
    feed: Feed,
    *,
    now: datetime,
    base_interval_seconds: int,
    broken_threshold: int,
    broken_max_backoff_seconds: int,
    backoff_jitter_ratio: float,
    server_hint_seconds: int | None = None,
    weekly_entry_count: int = 0,
    entry_frequency_min_interval_seconds: int = 300,
    entry_frequency_max_interval_seconds: int = 86400,
    entry_frequency_factor: int = 1,
) -> datetime:
    """Pick the next fetch instant based on the feed's current status.

    Active and dead feeds use entry_frequency scheduling: feeds that
    post frequently get polled more often, quiet feeds get polled less
    often. When no history is available, falls back to
    ``base_interval_seconds``. Broken feeds use exponential backoff:

        excess_failures = max(0, consecutive_failures - broken_threshold)
        factor = 2 ** excess_failures
        raw = base_interval_seconds * factor
        capped = min(raw, broken_max_backoff_seconds)
        jitter = uniform(-ratio, +ratio) * capped
        next = now + (capped + jitter) seconds

    The jitter prevents thundering-herd recovery when many feeds
    transition to broken together due to a shared upstream outage.
    """
    if feed.status != FeedStatus.BROKEN:
        if weekly_entry_count > 0:
            raw = (7 * 24 * 3600) / (weekly_entry_count * entry_frequency_factor)
            computed = max(
                entry_frequency_min_interval_seconds,
                min(raw, entry_frequency_max_interval_seconds),
            )
        else:
            computed = float(base_interval_seconds)
        effective = max(computed, server_hint_seconds or 0)
        return now + timedelta(seconds=effective)

    excess = max(0, feed.consecutive_failures - broken_threshold)
    factor = 2**excess
    raw_interval = base_interval_seconds * factor
    capped = min(raw_interval, broken_max_backoff_seconds)
    jitter_span = backoff_jitter_ratio * capped
    jitter = random.uniform(-jitter_span, jitter_span)
    return now + timedelta(seconds=capped + jitter)


# Content types that unambiguously indicate "this is not a feed".
# We intentionally do NOT maintain an allow-list because many feeds
# serve odd values like ``application/octet-stream`` (Python Insider)
# or omit the header entirely; feedparser handles those just fine.
# ``text/plain`` is also allowed: GitHub raw URLs always serve files
# that way under their nosniff policy, while the body can still be valid
# RSS/Atom XML that feedparser parses correctly.
NOT_A_FEED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/ld+json",
    }
)


def is_not_a_feed_content_type(ct: str | None) -> bool:
    """Return True if the Content-Type header is clearly not a feed."""
    if not ct:
        return False
    base = ct.split(";", 1)[0].strip().lower()
    return base in NOT_A_FEED_CONTENT_TYPES


def parse_retry_after(header: str | None, *, now: datetime) -> int | None:
    """Parse ``Retry-After`` per RFC 7231 §7.1.3.

    Accepts either the integer-seconds form (``"120"``) or the
    HTTP-date form (``"Wed, 11 Apr 2026 07:30:00 GMT"``). Cloudflare,
    GitHub, and other large origins emit the date form in production,
    so supporting only seconds would silently drop the signal and
    hammer the upstream at base interval instead of honoring the
    requested delay.

    Returns the delay in seconds relative to ``now``, clamped at zero
    (a past date returns ``0``, not a negative number). Returns
    ``None`` when the header is absent or unparseable in either form.
    """
    if header is None:
        return None
    stripped = header.strip()
    try:
        return max(0, int(stripped))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231 §7.1.1.1). email.utils handles all three
    # legal date formats — IMF-fixdate, RFC 850, asctime.
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    # Dates without an explicit timezone are treated as UTC per RFC 7231.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta_seconds = (parsed - now).total_seconds()
    return max(0, int(delta_seconds))


def parse_cache_hint(headers: httpx.Headers, *, now: datetime) -> int | None:
    """Parse Cache-Control max-age or Expires.

    Returns seconds from now, or None if absent/unparseable.
    """
    cc = headers.get("cache-control", "")
    for part in cc.split(","):
        stripped = part.strip()
        if stripped.lower().startswith("max-age="):
            try:
                return max(0, int(stripped[8:]))
            except ValueError:
                pass
    expires = headers.get("expires")
    if expires:
        try:
            parsed_dt = parsedate_to_datetime(expires)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=UTC)
            return max(0, int((parsed_dt - now).total_seconds()))
        except (TypeError, ValueError):
            pass
    return None
