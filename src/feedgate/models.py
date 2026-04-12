"""SQLAlchemy 2.0 ORM models.

Contract lives in ADR 001 (invariants) and docs/spec/feed.md +
docs/spec/entry.md (columns, indexes, lifecycle). Walking skeleton
creates all columns the spec requires — logic for status transitions,
error coding, etc. is deferred per the plan's non-goals, but the
schema is complete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Feed(Base):
    __tablename__ = "feeds"

    # Core identity (ADR 001, spec/feed.md)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    effective_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle (API-exposed, spec/feed.md)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    last_successful_fetch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Scheduler metadata (internal only, NOT API-exposed per ADR 003)
    next_fetch_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    entries: Mapped[list[Entry]] = relationship(
        "Entry",
        back_populates="feed",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_feeds_status", "status"),
        Index(
            "ix_feeds_due_not_dead",
            "next_fetch_at",
            # Covers due path: status != 'dead' AND next_fetch_at <= now.
            postgresql_where="status <> 'dead'",
        ),
        Index(
            "ix_feeds_dead_last_attempt",
            "last_attempt_at",
            # Covers dead-probe path: status = 'dead' AND last_attempt_at ...
            postgresql_where="status = 'dead'",
        ),
    )


class Entry(Base):
    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("feeds.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Stable per-feed identifier (ADR 001 invariant #2)
    guid: Mapped[str] = mapped_column(Text, nullable=False)

    # Content fields (mutable via upsert per spec/entry.md)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Retention clock — NEVER mutated by upsert (ADR 001 invariant #4, ADR 004)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Last content-bearing change. Equal to fetched_at on first insert.
    content_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    feed: Mapped[Feed] = relationship("Feed", back_populates="entries")

    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_entries_feed_guid"),
        Index("ix_entries_fetched_at", "fetched_at"),
    )


# Compound index with DESC ordering matches the keyset sort key
# `(published_at DESC, id DESC)` used by GET /v1/entries (ADR 002).
# Defined outside __table_args__ so the class attributes (with .desc())
# are resolvable. Attaches automatically to Entry.__table__.
Index(
    "ix_entries_feed_pub_id",
    Entry.feed_id,
    Entry.published_at.desc(),
    Entry.id.desc(),
)
