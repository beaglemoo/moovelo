"""Places, POIs and cycle routes parsed from the OSM extract.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geography, Geometry
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Trigram indexing is what makes "trin" find "Tring" without a
    # full-text search server. unaccent is deliberately not used: it is
    # STABLE rather than IMMUTABLE, so it cannot appear in a generated
    # column or an index without a wrapper function. The indexer folds
    # accents in Python and stores the result in name_norm instead.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "places",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # n/w/r plus the OSM id, so a rebuild can be diffed against the
        # previous one and a result can be traced back to openstreetmap.org.
        sa.Column("osm_type", sa.String(1), nullable=False),
        sa.Column("osm_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Accent-folded, casefolded name. Written by the indexer, never by
        # the app; the trigram index lives on this column, not on name.
        sa.Column("name_norm", sa.Text(), nullable=False),
        sa.Column("place_type", sa.Text(), nullable=False),
        sa.Column("county", sa.Text(), nullable=True),
        sa.Column("population", sa.Integer(), nullable=True),
        # Ranking weight derived from place_type and population at index
        # time, so search ordering costs no joins.
        sa.Column("importance", sa.Float(), nullable=False, server_default="0"),
        # geography, not geometry: ST_DWithin and ST_Distance then work in
        # meters and stay index-backed. A geometry column cast per row
        # would quietly disable ix_places_geog.
        sa.Column(
            "geog",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
    )
    op.create_index("ix_places_osm", "places", ["osm_type", "osm_id"], unique=True)
    op.create_index(
        "ix_places_name_trgm",
        "places",
        ["name_norm"],
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.create_index("ix_places_geog", "places", ["geog"], postgresql_using="gist")

    op.create_table(
        "pois",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("osm_type", sa.String(1), nullable=False),
        sa.Column("osm_id", sa.BigInteger(), nullable=False),
        # Nullable on purpose: a drinking fountain or a bench rarely
        # carries a name, and those are exactly the POIs worth finding.
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("name_norm", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        # Opening hours and the like, kept whole rather than modelled.
        sa.Column("osm_tags", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "geog",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
    )
    op.create_index("ix_pois_osm", "pois", ["osm_type", "osm_id"], unique=True)
    op.create_index("ix_pois_geog", "pois", ["geog"], postgresql_using="gist")
    op.create_index("ix_pois_category", "pois", ["category"])
    # Partial, so the unnamed majority costs nothing to index.
    op.execute(
        "CREATE INDEX ix_pois_name_trgm ON pois USING gin (name_norm gin_trgm_ops) "
        "WHERE name_norm IS NOT NULL"
    )

    op.create_table(
        "cycle_ways",
        # One row per OSM route relation rather than per way, so the
        # overlay can label a line "NCN 6" instead of drawing anonymous
        # segments.
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=True),
        # geometry rather than geography, and 4326 rather than 3857: the
        # tile query transforms the tile envelope, which is a constant, so
        # the index still serves the bounding-box filter.
        sa.Column(
            "geom",
            Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False),
            nullable=False,
        ),
    )
    op.create_index("ix_cycle_ways_geom", "cycle_ways", ["geom"], postgresql_using="gist")
    op.create_index("ix_cycle_ways_network", "cycle_ways", ["network"])

    op.create_table(
        "search_index_meta",
        # Single row, enforced by the type: the app asks "has the index
        # been built" and there is only ever one answer.
        sa.Column("id", sa.Boolean(), primary_key=True, server_default=sa.true()),
        sa.CheckConstraint("id", name="ck_search_index_meta_singleton"),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_files", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("place_count", sa.Integer(), nullable=False),
        sa.Column("poi_count", sa.Integer(), nullable=False),
        sa.Column("cycle_way_count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("search_index_meta")
    op.drop_index("ix_cycle_ways_network", table_name="cycle_ways")
    op.drop_index("ix_cycle_ways_geom", table_name="cycle_ways")
    op.drop_table("cycle_ways")
    op.drop_index("ix_pois_name_trgm", table_name="pois")
    op.drop_index("ix_pois_category", table_name="pois")
    op.drop_index("ix_pois_geog", table_name="pois")
    op.drop_index("ix_pois_osm", table_name="pois")
    op.drop_table("pois")
    op.drop_index("ix_places_geog", table_name="places")
    op.drop_index("ix_places_name_trgm", table_name="places")
    op.drop_index("ix_places_osm", table_name="places")
    op.drop_table("places")
