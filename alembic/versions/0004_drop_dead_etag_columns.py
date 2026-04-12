"""drop dead etag columns from feeds

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-12

Removes feeds.etag and feeds.last_modified. They were reserved for the
planned ETag/If-Modified-Since feature, but no code path currently
reads/writes them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("feeds", "etag")
    op.drop_column("feeds", "last_modified")


def downgrade() -> None:
    op.add_column("feeds", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("feeds", sa.Column("last_modified", sa.Text(), nullable=True))
