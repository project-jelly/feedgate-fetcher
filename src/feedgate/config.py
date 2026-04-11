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
    fetch_timeout_seconds: float = 20.0
    fetch_max_bytes: int = 5 * 1024 * 1024
    fetch_user_agent: str = "feedgate-fetcher/0.0.1 (+https://github.com/feedgate)"
    fetch_max_entries_initial: int = 50
    fetch_concurrency: int = 4
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
