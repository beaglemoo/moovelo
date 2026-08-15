"""Flag a match that outlived the geometry it was made against.

Re-routing a saved route re-derives the rides matched to it, except the ones
a rider locked by hand - their choice of route is theirs and must stand. But
anything DERIVED from comparing that ride to that route now describes a
different road: ride-time calibration solves the new elevation against the
old ride's moving time, and planned-vs-actual shows a confident comparison
against a road the ride never touched. Measured: a genuine 26 km/h rider was
offered 60 km/h - the model's ceiling - with a one-click Apply that writes it
into the setting every ETA in the app reads.

Set by the re-derive pass for the locked rides it had to skip, and cleared by
the same UPDATE that writes any fresh match, so it cannot drift out of step.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column("match_stale", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    # No normalisation needed on the way down, unlike migration 0010's enum:
    # the column only ever carried a hint about a link, never the link itself,
    # and older code neither reads nor requires it. Dropping it returns to
    # "every match is treated as current", which is the pre-0020 behaviour.
    op.drop_column("activities", "match_stale")
