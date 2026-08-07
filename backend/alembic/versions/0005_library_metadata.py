"""Tags, notes and favourites on routes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column("routes", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(
        "routes",
        sa.Column("is_favourite", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Filtering by tag is a containment query, which is what GIN indexes.
    op.create_index("ix_routes_tags", "routes", ["tags"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_routes_tags", table_name="routes")
    op.drop_column("routes", "is_favourite")
    op.drop_column("routes", "notes")
    op.drop_column("routes", "tags")
