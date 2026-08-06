"""Wahoo accounts and route sync state.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wahoo_accounts",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("access_token", sa.String(500), nullable=False),
        sa.Column("refresh_token", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("athlete", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "routes",
        sa.Column("wahoo_status", sa.String(10), nullable=False, server_default="none"),
    )
    op.add_column("routes", sa.Column("wahoo_error", sa.String(500), nullable=True))
    op.add_column("routes", sa.Column("wahoo_route_id", sa.String(64), nullable=True))
    op.add_column("routes", sa.Column("wahoo_pushed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("routes", "wahoo_pushed_at")
    op.drop_column("routes", "wahoo_route_id")
    op.drop_column("routes", "wahoo_error")
    op.drop_column("routes", "wahoo_status")
    op.drop_table("wahoo_accounts")
