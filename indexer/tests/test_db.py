"""db.py's load -> assemble -> publish pipeline against real Postgres/PostGIS,
with particular attention to `cycle_way_members` - the table this PR adds so
cycle-network coverage can map-match against real OSM way ids instead of the
relation-level geometry `cycle_ways` alone carries (see the module docstring
in indexer/db.py and models.CycleWayMember on the backend side).
"""

import pytest

from indexer import db
from indexer.extract import CycleMemberRow


def _rows() -> list[CycleMemberRow]:
    # Two routes sharing one way - a common real shape (a stretch of NCN and
    # a regional route running the same road) - plus a route-only way, so a
    # rebuild has both a repeated and a unique (relation_id, way_id) pair to
    # get right.
    return [
        CycleMemberRow(
            relation_id=1,
            ref="NCN 6",
            name="Route A",
            network="ncn",
            operator=None,
            way_id=100,
            wkt="LINESTRING(-0.660 51.800, -0.659 51.801)",
        ),
        CycleMemberRow(
            relation_id=1,
            ref="NCN 6",
            name="Route A",
            network="ncn",
            operator=None,
            way_id=101,
            wkt="LINESTRING(-0.659 51.801, -0.658 51.802)",
        ),
        CycleMemberRow(
            relation_id=2,
            ref="RCN 30",
            name="Route B",
            network="rcn",
            operator=None,
            way_id=100,
            wkt="LINESTRING(-0.660 51.800, -0.659 51.801)",
        ),
    ]


def _build(database_url: str, connection: object, rows: list[CycleMemberRow]) -> dict[str, int]:
    db.prepare_staging(connection)  # type: ignore[arg-type]
    counts = db.load(database_url, rows)
    counts["cycle_ways"] = db.assemble_cycle_routes(connection)  # type: ignore[arg-type]
    db.publish(connection, ["tring.osm.pbf"], counts)  # type: ignore[arg-type]
    return counts


def test_publish_populates_cycle_way_members_with_lengths(database_url: str) -> None:
    with db.connect(database_url) as connection:
        _build(database_url, connection, _rows())
        rows = connection.execute(
            "SELECT relation_id, way_id, length_m, ST_AsText(geom) FROM cycle_way_members "
            "ORDER BY relation_id, way_id"
        ).fetchall()

    assert [(r[0], r[1]) for r in rows] == [(1, 100), (1, 101), (2, 100)]
    assert all(length_m > 0 for _, _, length_m, _ in rows)
    # The shape comes through too, not just the length. Without it a
    # coverage bbox has to filter on the whole route's envelope, which
    # measured 15x too generous on the England extract.
    assert all(wkt and wkt.startswith("LINESTRING") for _, _, _, wkt in rows)
    # Way 100 is ridden as part of two different routes; both rows describe
    # the same physical way and must carry the same length.
    assert rows[0][2] == pytest.approx(rows[2][2])


def test_publish_records_the_member_count_search_index_meta_needs(database_url: str) -> None:
    """cycle_way_member_count is what lets /api/coverage/cycle-network tell
    an unbuilt index apart from one that predates this feature - see
    models.SearchIndexMeta. A rebuild must always leave it a real number,
    never null."""
    with db.connect(database_url) as connection:
        _build(database_url, connection, _rows())
        member_count = connection.execute(
            "SELECT cycle_way_member_count FROM search_index_meta"
        ).fetchone()[0]

    assert member_count == 3


def test_publish_leaves_no_raw_member_staging_table_behind(database_url: str) -> None:
    """The raw geometry table only ever exists mid-build. A stray copy left
    over from a crashed run must not survive prepare_staging, let alone a
    completed one - it holds full LineString geometry per member and is
    exactly what assemble_cycle_routes exists to make disposable."""
    with db.connect(database_url) as connection:
        _build(database_url, connection, _rows())
        found = connection.execute("SELECT to_regclass(%s)", (db.RAW_MEMBER_TABLE,)).fetchone()[0]

    assert found is None


def test_a_rebuild_replaces_rather_than_accumulates(database_url: str) -> None:
    with db.connect(database_url) as connection:
        _build(database_url, connection, _rows())
        _build(database_url, connection, _rows()[:2])  # only relation 1's two ways
        total = connection.execute("SELECT count(*) FROM cycle_way_members").fetchone()[0]

    assert total == 2


def test_cycle_ways_geometry_is_unaffected_by_the_member_table(database_url: str) -> None:
    """The member-length table is additive - it must not change what
    assemble_cycle_routes has always produced for the overlay itself."""
    with db.connect(database_url) as connection:
        counts = _build(database_url, connection, _rows())
        way_count = connection.execute("SELECT count(*) FROM cycle_ways").fetchone()[0]

    assert counts["cycle_ways"] == 2  # relations 1 and 2
    assert way_count == 2
