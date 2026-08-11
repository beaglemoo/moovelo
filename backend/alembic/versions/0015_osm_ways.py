"""All-roads coverage: every bikeable OSM way, not just the signed cycle
network cycle_way_members already covers.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Published by the indexer, alongside places/pois/cycle_ways/
    # cycle_way_members. One row per OSM highway way a bicycle could ride -
    # see indexer/indexer/categories.py:ROAD_HIGHWAY_EXCLUDE for exactly
    # what is and is not kept, and models.OsmWay for why way_id (not a
    # synthetic id) is the primary key: unlike a cycle-route member, a road
    # way has no owning relation to pair it with, so there is nothing else
    # a natural key could be.
    op.create_table(
        "osm_ways",
        sa.Column("way_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("highway", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("length_m", sa.Float(), nullable=False),
        # spatial_index=False with the index named below, matching Place,
        # Poi, CycleWay and CycleWayMember. Geometry is simplified at index
        # time (indexer/indexer/db.py:assemble_road_ways) - nothing ever
        # draws this, it is only summed and bbox-tested, so a coarser
        # tolerance than anything meant for display is deliberate.
        sa.Column(
            "geom",
            Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
            nullable=False,
        ),
    )
    # What makes "the roads near here" a bounding-box lookup rather than a
    # scan of every bikeable way in the country - the same role
    # ix_cycle_way_members_geom plays for the signed-network denominator.
    op.create_index("ix_osm_ways_geom", "osm_ways", ["geom"], postgresql_using="gist")
    # Lets the coverage query's GROUP BY highway, and any future per-class
    # filter, avoid a full scan - mirrors ix_cycle_ways_network.
    op.create_index("ix_osm_ways_highway", "osm_ways", ["highway"])

    # Nullable, no default: existing installs re-run the indexer to
    # populate this (see docs/data.md), and until they do, NULL is exactly
    # the signal /api/coverage/roads needs to say "re-index to see this"
    # rather than a dishonest 0% - the same reasoning
    # cycle_way_member_count already carries for the signed network.
    op.add_column("search_index_meta", sa.Column("osm_way_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("search_index_meta", "osm_way_count")
    op.drop_index("ix_osm_ways_highway", table_name="osm_ways")
    op.drop_table("osm_ways")
