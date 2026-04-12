"""add retention sweep sort index on entries

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-12

Adds a compound index that matches the retention sweep per-feed window
sort key: (feed_id, fetched_at DESC, id DESC).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_entries_feed_fetched_id",
        "entries",
        [
            sa.text("feed_id"),
            sa.text("fetched_at DESC"),
            sa.text("id DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_entries_feed_fetched_id", table_name="entries")
