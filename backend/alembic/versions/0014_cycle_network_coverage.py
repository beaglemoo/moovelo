"""Cycle-network coverage: which OSM ways the indexer's routes are made of,
and which of those a rider has actually ridden.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Published by the indexer, alongside places/pois/cycle_ways.
    #
    # Members carry their own geometry, unlike CycleWay's merged shape. The
    # ST_LineMerge in assemble_cycle_routes exists because unmerged parts
    # resist simplification and made the overlay *tile* expensive; this
    # table is never tiled, so that reasoning does not reach it - and
    # without member geometry "coverage near you" cannot be asked at all.
    # Filtering through cycle_ways.geom instead tests the envelope of an
    # entire national route: measured on the England extract, a 12 km box
    # around Tring pulled in 8,301 member ways totalling 1,921 km, against
    # 125 km of network actually inside it. A 15x overstatement, because
    # NCN 1's envelope covers most of the country.
    op.create_table(
        "cycle_way_members",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # Not a hard FK: the indexer publishes both tables in one
        # TRUNCATE-and-reload transaction (see indexer/db.py:publish), and a
        # foreign key would have to be dropped and recreated around every
        # rebuild for no benefit the transaction does not already give.
        sa.Column("relation_id", sa.BigInteger(), nullable=False),
        sa.Column("way_id", sa.BigInteger(), nullable=False),
        sa.Column("length_m", sa.Float(), nullable=False),
        # The way's own shape, so a coverage bbox selects the ways actually
        # in it. spatial_index=False with the index named below, matching
        # Place, Poi and CycleWay.
        sa.Column(
            "geom",
            Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
            nullable=False,
        ),
    )
    # Leads with relation_id, which is how the coverage query reaches this
    # table (joined from cycle_ways.id) - and doubles as the natural-key
    # uniqueness the indexer's publish step deduplicates on.
    op.create_index(
        "ix_cycle_way_members_relation_way",
        "cycle_way_members",
        ["relation_id", "way_id"],
        unique=True,
    )
    # The other direction: matching an activity's ridden way ids back to the
    # routes they belong to.
    op.create_index("ix_cycle_way_members_way", "cycle_way_members", ["way_id"])
    # What makes "the network near here" a bounding-box lookup rather than a
    # scan of every member way in the country.
    op.create_index(
        "ix_cycle_way_members_geom", "cycle_way_members", ["geom"], postgresql_using="gist"
    )

    # App-owned, unlike the table above: written by the backend as activities
    # are map-matched, not by the indexer. One row per way a rider has ever
    # been recorded riding, not per ride - ride_count and first_ridden_at
    # accumulate across every activity that touched it. The composite primary
    # key is the only index this needs: coverage always looks a way up by
    # (user_id, way_id) together, backfill's upsert conflicts on the same
    # pair, and nothing in this feature ever queries by way_id alone across
    # users.
    op.create_table(
        "activity_ways",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("way_id", sa.BigInteger(), nullable=False),
        sa.Column("first_ridden_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ride_count", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("user_id", "way_id"),
    )

    # Whether - and when - an activity's trace has been map-matched onto OSM
    # ways for coverage. Null for every activity imported before this PR;
    # that is the backfill queue's own work list (see services/way_matching.py),
    # and it is set (never cleared) on both a successful and a failed
    # attempt, so a track Valhalla genuinely cannot place is not retried
    # forever.
    op.add_column(
        "activities", sa.Column("ways_matched_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Nullable, no default: existing installs re-run the indexer to populate
    # this (see docs/data.md), and until they do, NULL is exactly the signal
    # /api/coverage/cycle-network needs to say "re-index to see this" rather
    # than a dishonest 0%.
    op.add_column(
        "search_index_meta", sa.Column("cycle_way_member_count", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("search_index_meta", "cycle_way_member_count")
    op.drop_column("activities", "ways_matched_at")
    op.drop_table("activity_ways")
    op.drop_index("ix_cycle_way_members_way", table_name="cycle_way_members")
    op.drop_index("ix_cycle_way_members_relation_way", table_name="cycle_way_members")
    op.drop_table("cycle_way_members")
