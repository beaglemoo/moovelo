"""Rides that happened, as distinct from routes that were planned.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        # Nullable: a file can declare itself an activity and still carry no
        # usable timestamps. Those rides sort on created_at instead of being
        # refused.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        # Null together when the file had no timestamps. Not defaulted to 0 -
        # "we do not know" and "the rider never moved" are different answers.
        sa.Column("elapsed_time_s", sa.Float(), nullable=True),
        sa.Column("moving_time_s", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=False),
        sa.Column("ascent_m", sa.Float(), nullable=False),
        sa.Column("descent_m", sa.Float(), nullable=False),
        sa.Column("elevation", postgresql.JSONB(), nullable=False),
        # The recorded trace, not a map-matched one. geometry rather than
        # geography, and 4326: the heatmap tile query transforms the tile
        # envelope, which is a constant, so the index still serves the
        # bounding-box filter - the same reasoning as cycle_ways.
        sa.Column(
            "geom",
            Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default="file"),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("import_status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("import_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_activities_user_id", "activities", ["user_id"])
    # The library's own query: one user's rides, newest first.
    op.create_index("ix_activities_user_started", "activities", ["user_id", "started_at"])
    op.create_index("ix_activities_geom", "activities", ["geom"], postgresql_using="gist")
    # Partial, so the many rides with no natural identifier do not collide
    # with each other while a re-imported export still deduplicates.
    op.create_index(
        "ix_activities_source_ref",
        "activities",
        ["user_id", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    # Dropping the table takes its indexes with it. Nothing outside it
    # references activities yet, so there is no data to normalise on the way
    # down - unlike 0010, which had to put `preset = 'custom'` back to a
    # value the older code could read.
    op.drop_table("activities")
