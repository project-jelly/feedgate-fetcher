"""Application configuration via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FEEDGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/feedgate"
    fetch_interval_seconds: int = 60
    # Per-phase HTTP timeouts (httpx.Timeout). Splitting these is the
    # primary defense against slow-loris-style upstreams that drip-feed
    # bytes just slowly enough to keep a worker tied up. ``read`` is
    # the load-bearing one (per-chunk inactivity) and is intentionally
    # tighter than the historical 20s blanket. ``connect`` is short
    # because TCP handshake should never take 20s on a healthy host.
    fetch_connect_timeout_seconds: float = 5.0
    fetch_read_timeout_seconds: float = 15.0
    fetch_write_timeout_seconds: float = 10.0
    fetch_pool_timeout_seconds: float = 5.0
    # Hard total wall-clock budget for one ``fetch_one`` call,
    # enforced via ``asyncio.timeout``. Even if every individual chunk
    # arrives within ``read``, an upstream that streams a 200-byte
    # body across many small chunks can still pin a worker; this
    # bound caps the total time and reclassifies the failure as
    # ``ErrorCode.TIMEOUT``. Set comfortably above the sum of the
    # per-phase timeouts so it only fires on pathological cases.
    fetch_total_budget_seconds: float = 30.0
    fetch_max_bytes: int = 5 * 1024 * 1024
    fetch_user_agent: str = "feedgate-fetcher/0.0.1 (+https://github.com/feedgate)"
    fetch_max_entries_initial: int = 50
    fetch_concurrency: int = 4
    # Per-host concurrency cap. The global ``fetch_concurrency`` bounds
    # how many feeds we fetch simultaneously across the whole tick;
    # this knob bounds how many of those can target the **same** host.
    # Default 1 means same-host requests are fully serialized within a
    # tick — important when one origin hosts dozens of our feeds (e.g.
    # all the GitHub release feeds), so we never look like a DDoS to
    # any single upstream. The cap is per-tick (the dict is rebuilt
    # every tick_once); cross-tick spacing is handled by the existing
    # ``next_fetch_at`` schedule.
    fetch_per_host_concurrency: int = 1
    # Distributed-claim tuning for the scheduler's SKIP LOCKED loop.
    # A tick atomically reserves up to `fetch_claim_batch_size` feeds
    # by advancing their `next_fetch_at` to `now + claim_ttl_seconds`
    # (and `last_attempt_at = now`). Another worker running in
    # parallel sees the bumped timestamps and skips the feed until the
    # lease expires, giving crash-safe at-least-once semantics without
    # an external queue.
    fetch_claim_batch_size: int = 8
    fetch_claim_ttl_seconds: int = 180
    scheduler_enabled: bool = True

    # Retention policy (ADR 004, docs/spec/entry.md).
    # Entries are kept if they fall in EITHER the time window
    # (fetched_at >= now - retention_days) OR the per-feed top-N
    # window (most recent retention_min_per_feed by fetched_at DESC).
    # The sweeper runs every retention_sweep_interval_seconds.
    retention_days: int = 90
    retention_min_per_feed: int = 20
    retention_sweep_interval_seconds: int = 3600
    retention_enabled: bool = True

    # Feed lifecycle state machine (docs/spec/feed.md).
    # active -> broken after `broken_threshold` consecutive failures.
    # broken -> dead when (now - last_successful_fetch_at) exceeds
    # `dead_duration_days`, using `created_at` as fallback when no
    # success has ever been recorded. http_410 is an immediate dead
    # transition from any state.
    broken_threshold: int = 3
    dead_duration_days: int = 7
    broken_max_backoff_seconds: int = 3600
    backoff_jitter_ratio: float = 0.25
    dead_probe_interval_days: int = 7


def get_settings() -> Settings:
    return Settings()
