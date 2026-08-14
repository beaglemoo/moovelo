"""Link a ride to the saved route it followed.

services/route_match.py finds this automatically (bbox-narrowed candidates,
confirmed with ST_FrechetDistance) or a rider sets it by hand. `route_id` is
ON DELETE SET NULL rather than CASCADE: deleting the route the ride was
matched against must not delete the ride itself - the ride happened
regardless, it just loses its link. `match_confidence` is the Frechet
distance, in true ground metres, that produced an auto-match; null for a
manual link or no match at all. `match_locked` is set the moment a rider
picks or clears the match by hand, so the auto-matcher never overwrites a
human decision on a later run.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column(
            "route_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("routes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_activities_route_id", "activities", ["route_id"])
    op.add_column("activities", sa.Column("match_confidence", sa.Float(), nullable=True))
    op.add_column(
        "activities",
        sa.Column("match_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("activities", "match_locked")
    op.drop_column("activities", "match_confidence")
    op.drop_index("ix_activities_route_id", table_name="activities")
    op.drop_column("activities", "route_id")
