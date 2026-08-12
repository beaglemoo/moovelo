"""Track which extract osm_ways itself was built from, separately from
search_index_meta.source_files.

publish() (indexer/db.py) leaves the live osm_ways table untouched on a
rebuild that runs with INDEX_ROADS off, but it always overwrites
source_files and built_at with the run that just finished - so an install
that indexed England with roads on and later reruns against a different
extract with roads off ends up with search_index_meta describing the new
extract while osm_ways still holds the old one's roads, and nothing records
that they now disagree. /api/coverage/roads would report confidently
against a country the rest of the index no longer covers.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default, same reasoning as osm_way_count (migration 0015):
    # an install that has never indexed roads has nothing to name here, and
    # NULL has to mean that rather than an empty list ("built from zero
    # files"), which would compare unequal to source_files for the wrong
    # reason.
    op.add_column(
        "search_index_meta",
        sa.Column("osm_ways_source_files", sa.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("search_index_meta", "osm_ways_source_files")
