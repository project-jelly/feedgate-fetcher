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


def get_settings() -> Settings:
    return Settings()
