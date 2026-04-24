"""add etag and last_modified to feeds

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-23

Adds feeds.etag and feeds.last_modified to support HTTP conditional
requests (ETag/If-None-Match and Last-Modified/If-Modified-Since).
Values are stored after each successful 200 response and sent on the
next fetch to enable 304 Not Modified short-circuiting.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feeds", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("feeds", sa.Column("last_modified", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("feeds", "last_modified")
    op.drop_column("feeds", "etag")
