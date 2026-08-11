"""db.py's load -> assemble -> publish pipeline against real Postgres/PostGIS,
with particular attention to `cycle_way_members` - the table this PR adds so
cycle-network coverage can map-match against real OSM way ids instead of the
relation-level geometry `cycle_ways` alone carries (see the module docstring
in indexer/db.py and models.CycleWayMember on the backend side).
"""

import pytest

from indexer import db
from indexer.extract import CycleMemberRow, RoadWayRow


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


def _build(
    database_url: str, connection: object, rows: list[CycleMemberRow] | list[RoadWayRow]
) -> dict[str, int]:
    db.prepare_staging(connection)  # type: ignore[arg-type]
    counts = db.load(database_url, rows)
    counts["cycle_ways"] = db.assemble_cycle_routes(connection)  # type: ignore[arg-type]
    # No RoadWayRow in these rows, but publish() still needs the key -
    # assemble_road_ways is always run, even when nothing was staged.
    counts["osm_ways"] = db.assemble_road_ways(connection)  # type: ignore[arg-type]
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


# --- osm_ways (all-roads coverage) -----------------------------------------


def _road_rows() -> list[RoadWayRow]:
    # A long, wiggly way (many vertices, so simplification has something to
    # do) and a short straight one, so a rebuild has both a case where
    # simplification changes the point count and one where there is nothing
    # to simplify away.
    return [
        RoadWayRow(
            way_id=100,
            highway="residential",
            name="Church Street",
            wkt=(
                "LINESTRING(-0.6600 51.8000, -0.65991 51.80003, -0.65982 51.79998, "
                "-0.65973 51.80004, -0.65964 51.79997, -0.6590 51.8000)"
            ),
        ),
        RoadWayRow(
            way_id=101,
            highway="track",
            name=None,
            wkt="LINESTRING(-0.6580 51.8010, -0.6570 51.8020)",
        ),
    ]


def test_publish_populates_osm_ways_with_lengths(database_url: str) -> None:
    with db.connect(database_url) as connection:
        _build(database_url, connection, _road_rows())
        rows = connection.execute(
            "SELECT way_id, highway, name, length_m, ST_AsText(geom) FROM osm_ways ORDER BY way_id"
        ).fetchall()

    assert [(r[0], r[1], r[2]) for r in rows] == [
        (100, "residential", "Church Street"),
        (101, "track", None),
    ]
    assert all(length_m > 0 for _, _, _, length_m, _ in rows)
    assert all(wkt and wkt.startswith("LINESTRING") for _, _, _, _, wkt in rows)


def test_simplification_reduces_the_vertex_count_but_not_to_nothing(database_url: str) -> None:
    """The wiggly way (6 points) should come out with fewer, but a
    LineString still needs at least 2 - ST_SimplifyPreserveTopology
    guarantees that, unlike plain ST_Simplify."""
    with db.connect(database_url) as connection:
        _build(database_url, connection, _road_rows())
        points = connection.execute(
            "SELECT ST_NPoints(geom) FROM osm_ways WHERE way_id = 100"
        ).fetchone()[0]

    assert 2 <= points < 6


def test_length_is_measured_before_simplification_not_after(database_url: str) -> None:
    """A coarse display tolerance must not quietly shrink the figure
    coverage sums - only the stored shape gets coarser."""
    with db.connect(database_url) as connection:
        _build(database_url, connection, _road_rows())
        length_m, simplified_length_m = connection.execute("""
            SELECT length_m, ST_Length(geography(geom)) FROM osm_ways WHERE way_id = 100
        """).fetchone()

    # The wiggle adds real distance a simplified straight-ish line loses -
    # if length_m had been computed from the simplified shape instead, the
    # two would already match.
    assert length_m > simplified_length_m


def test_publish_records_the_road_way_count_search_index_meta_needs(database_url: str) -> None:
    """osm_way_count is what lets /api/coverage/roads tell an unbuilt index
    apart from one that predates this feature - see models.SearchIndexMeta.
    A rebuild must always leave it a real number, never null."""
    with db.connect(database_url) as connection:
        _build(database_url, connection, _road_rows())
        way_count = connection.execute("SELECT osm_way_count FROM search_index_meta").fetchone()[0]

    assert way_count == 2


def test_publish_leaves_no_raw_road_way_staging_table_behind(database_url: str) -> None:
    with db.connect(database_url) as connection:
        _build(database_url, connection, _road_rows())
        found = connection.execute("SELECT to_regclass(%s)", (db.RAW_ROAD_WAY_TABLE,)).fetchone()[0]

    assert found is None


def test_a_road_way_rebuild_replaces_rather_than_accumulates(database_url: str) -> None:
    with db.connect(database_url) as connection:
        _build(database_url, connection, _road_rows())
        _build(database_url, connection, _road_rows()[:1])
        total = connection.execute("SELECT count(*) FROM osm_ways").fetchone()[0]

    assert total == 1


def test_a_way_repeated_across_overlapping_extracts_is_stored_once(database_url: str) -> None:
    """VALHALLA_TILE_URL accepts several extracts, and Geofabrik extracts
    overlap at their borders - the same way a border node can, a border way
    can arrive from two files. publish()'s DISTINCT ON the natural key
    (way_id alone, since a road way has no owning relation to pair it with)
    has to collapse that, the same as it already does for places and pois.
    """
    with db.connect(database_url) as connection:
        db.prepare_staging(connection)
        counts = db.load(database_url, _road_rows())
        more = db.load(database_url, _road_rows()[:1])  # way 100 again, as if from a second file
        for key, value in more.items():
            counts[key] += value
        counts["cycle_ways"] = db.assemble_cycle_routes(connection)
        counts["osm_ways"] = db.assemble_road_ways(connection)
        db.publish(connection, ["a.osm.pbf", "b.osm.pbf"], counts)
        total = connection.execute("SELECT count(*) FROM osm_ways").fetchone()[0]

    assert total == 2
