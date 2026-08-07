"""Where a route came from: planned in the app, or imported from a file.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column("source", sa.String(16), nullable=False, server_default="planned"),
    )


def downgrade() -> None:
    op.drop_column("routes", "source")
