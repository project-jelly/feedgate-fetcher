"""align feed claim indexes with scheduler predicates

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-12

Replaces the active-only next_fetch_at partial index with two partial
indexes that match _claim_due_feeds paths:
- status <> 'dead' and next_fetch_at <= now (due path)
- status = 'dead' and last_attempt_at probe filter (dead-probe path)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_feeds_next_fetch_at_active", table_name="feeds")
    op.create_index(
        "ix_feeds_due_not_dead",
        "feeds",
        ["next_fetch_at"],
        # Matches due path predicate: status != 'dead'.
        postgresql_where=sa.text("status <> 'dead'"),
    )
    op.create_index(
        "ix_feeds_dead_last_attempt",
        "feeds",
        ["last_attempt_at"],
        # Matches dead-probe path predicate: status = 'dead'.
        postgresql_where=sa.text("status = 'dead'"),
    )


def downgrade() -> None:
    op.drop_index("ix_feeds_dead_last_attempt", table_name="feeds")
    op.drop_index("ix_feeds_due_not_dead", table_name="feeds")
    op.create_index(
        "ix_feeds_next_fetch_at_active",
        "feeds",
        ["next_fetch_at"],
        postgresql_where=sa.text("status = 'active'"),
    )
