"""Stored natural-language summary for public share pages.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both nullable: an install with no assistant configured, or a route
    # that has never been shared, simply has neither column populated.
    # summary_signature is a hash of the route's own geometry (see
    # services/route_summary.py) - not a foreign key or a timestamp -
    # so a later re-route can be detected and the stale summary
    # suppressed without a background job to keep it in sync.
    op.add_column("routes", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("routes", sa.Column("summary_signature", sa.Text(), nullable=True))


def downgrade() -> None:
    # No fixed content is lost that could not be regenerated from the
    # route's own stats the next time it is shared.
    op.drop_column("routes", "summary_signature")
    op.drop_column("routes", "summary")
