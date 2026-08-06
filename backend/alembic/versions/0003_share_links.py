"""Public read-only share tokens on routes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("share_token", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_routes_share_token", "routes", ["share_token"])


def downgrade() -> None:
    op.drop_constraint("uq_routes_share_token", "routes")
    op.drop_column("routes", "share_token")
