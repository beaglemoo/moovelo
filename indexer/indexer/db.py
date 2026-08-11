"""Loading the index without taking the app down.

The parse takes minutes; the app is serving search the whole time. So
nothing is written to the live tables until the very end. Rows stream by
COPY into clones created with `LIKE ... INCLUDING DEFAULTS`, which mirrors
whatever columns and defaults Alembic currently defines - so this file never
hand-duplicates DDL that could drift from the migration. Indexes are left
off the clones on purpose; nothing queries a staging table.

Only the final transaction touches the live tables, and it moves rows
rather than renaming: see publish() for why the rename it originally did
could not work.
"""

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from indexer.extract import CycleMemberRow, PlaceRow, PoiRow, RoadWayRow

log = logging.getLogger(__name__)

INDEXED_TABLES = ("places", "pois", "cycle_ways", "cycle_way_members", "osm_ways")

PLACE_COLUMNS = (
    "osm_type",
    "osm_id",
    "name",
    "name_norm",
    "place_type",
    "population",
    "importance",
    "geog",
)
POI_COLUMNS = ("osm_type", "osm_id", "name", "name_norm", "category", "osm_tags", "geog")
# cycle_ways.id is the OSM relation id, so unlike the other two it is
# supplied rather than generated.
CYCLE_COLUMNS = ("id", "ref", "name", "network", "operator", "geom")
# For cycle-network coverage (Phase 10): which OSM ways make up each route,
# and how long each one is - deliberately no geometry, see assemble_cycle_routes.
CYCLE_MEMBER_COLUMNS = ("relation_id", "way_id", "length_m", "geom")
# For all-roads coverage (Phase 10): every bikeable OSM way, not just signed
# cycle routes. way_id is the OSM way id and osm_ways' own primary key - a
# way is a way, unlike a cycle-route member it has no owning relation to
# pair it with - so unlike the two columns above it is supplied here too.
ROAD_WAY_COLUMNS = ("way_id", "highway", "name", "length_m", "geom")

COLUMNS: dict[str, tuple[str, ...]] = {
    "places": PLACE_COLUMNS,
    "pois": POI_COLUMNS,
    "cycle_ways": CYCLE_COLUMNS,
    "cycle_way_members": CYCLE_MEMBER_COLUMNS,
    "osm_ways": ROAD_WAY_COLUMNS,
}

# Raw member geometry, one row per (relation, way) - not one of the
# published tables above, and never queried once assemble_cycle_routes has
# both collapsed it into cycle_ways.geom and summed it into
# cycle_way_members_stage. Named apart from "cycle_way_members_stage" (what
# `_stage("cycle_way_members")` would also produce) so the two staging
# tables this file juggles at once are never confusable.
RAW_MEMBER_TABLE = "cycle_route_members_raw"

# Raw road-way geometry, full fidelity, one row per way - dropped by
# assemble_road_ways once it has both measured the true length and written
# a simplified shape into osm_ways_stage. Nothing ever queries this table;
# it exists for exactly as long as one build takes.
RAW_ROAD_WAY_TABLE = "osm_ways_raw"

# What makes a row unique, used to deduplicate at publish time.
NATURAL_KEY: dict[str, tuple[str, ...]] = {
    "places": ("osm_type", "osm_id"),
    "pois": ("osm_type", "osm_id"),
    "cycle_ways": ("id",),
    "cycle_way_members": ("relation_id", "way_id"),
    "osm_ways": ("way_id",),
}

# Metres, applied in Web Mercator after ST_Transform so it means the same
# thing at every latitude England spans. Nothing ever draws osm_ways.geom -
# it is only summed (already done, before this runs) and bbox-tested - so
# this can be considerably coarser than anything meant for display. See
# docs/data.md for what this bought on the England extract.
ROAD_SIMPLIFY_TOLERANCE_M = 10.0


def _stage(table: str) -> str:
    return f"{table}_stage"


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(database_url) as connection:
        yield connection


def prepare_staging(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        for table in INDEXED_TABLES:
            cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(_stage(table))))
            # LIKE mirrors whatever columns and types the migration
            # defines, so this file never hand-duplicates DDL that could
            # drift. INCLUDING DEFAULTS carries the id default across;
            # indexes and constraints are deliberately left off, since
            # nothing queries a staging table and building them twice
            # would double the cost of a rebuild.
            cursor.execute(
                sql.SQL("CREATE UNLOGGED TABLE {} (LIKE {} INCLUDING DEFAULTS)").format(
                    sql.Identifier(_stage(table)), sql.Identifier(table)
                )
            )
        cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(RAW_MEMBER_TABLE)))
        cursor.execute(
            sql.SQL("""
                CREATE UNLOGGED TABLE {} (
                    relation_id bigint NOT NULL,
                    ref text,
                    name text,
                    network text NOT NULL,
                    operator text,
                    way_id bigint NOT NULL,
                    geom geometry(LineString, 4326) NOT NULL
                )
            """).format(sql.Identifier(RAW_MEMBER_TABLE))
        )
        cursor.execute(
            sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(RAW_ROAD_WAY_TABLE))
        )
        cursor.execute(
            sql.SQL("""
                CREATE UNLOGGED TABLE {} (
                    way_id bigint NOT NULL,
                    highway text NOT NULL,
                    name text,
                    geom geometry(LineString, 4326) NOT NULL
                )
            """).format(sql.Identifier(RAW_ROAD_WAY_TABLE))
        )
    connection.commit()


def load(
    database_url: str, rows: Iterable[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow]
) -> dict[str, int]:
    """Stream every extracted row into the staging tables.

    Four COPY streams have to be open at once, because the extract yields
    in file order rather than grouped by type, and buffering a national
    extract in Python to sort it would undo the point of streaming.

    Each stream therefore gets its own connection. A COPY IN takes over the
    connection it runs on for its whole duration, so several on one
    connection do not interleave - they deadlock, silently, with the
    process sitting at 0% CPU rather than raising anything.
    """
    counts = {"places": 0, "pois": 0, "cycle_members": 0, "road_ways": 0}
    seen_places: set[tuple[str, int]] = set()
    seen_pois: set[tuple[str, int]] = set()

    place_sql = _copy_sql(_stage("places"), PLACE_COLUMNS)
    poi_sql = _copy_sql(_stage("pois"), POI_COLUMNS)
    member_sql = _copy_sql(
        RAW_MEMBER_TABLE, ("relation_id", "ref", "name", "network", "operator", "way_id", "geom")
    )
    road_sql = _copy_sql(RAW_ROAD_WAY_TABLE, ("way_id", "highway", "name", "geom"))

    with (
        psycopg.connect(database_url) as place_connection,
        psycopg.connect(database_url) as poi_connection,
        psycopg.connect(database_url) as member_connection,
        psycopg.connect(database_url) as road_connection,
        place_connection.cursor() as place_cursor,
        poi_connection.cursor() as poi_cursor,
        member_connection.cursor() as member_cursor,
        road_connection.cursor() as road_cursor,
        place_cursor.copy(place_sql) as places,
        poi_cursor.copy(poi_sql) as pois,
        member_cursor.copy(member_sql) as members,
        road_cursor.copy(road_sql) as road_ways,
    ):
        for row in rows:
            if isinstance(row, PlaceRow):
                # The unique index would abort the whole COPY on a repeat,
                # and an extract can legitimately carry the same node twice
                # when two source files overlap.
                if (row.osm_type, row.osm_id) in seen_places:
                    continue
                seen_places.add((row.osm_type, row.osm_id))
                places.write_row(
                    (
                        row.osm_type,
                        row.osm_id,
                        row.name,
                        row.name_norm,
                        row.place_type,
                        row.population,
                        row.importance,
                        _point(row.lat, row.lon),
                    )
                )
                counts["places"] += 1
            elif isinstance(row, PoiRow):
                if (row.osm_type, row.osm_id) in seen_pois:
                    continue
                seen_pois.add((row.osm_type, row.osm_id))
                pois.write_row(
                    (
                        row.osm_type,
                        row.osm_id,
                        row.name,
                        row.name_norm,
                        row.category,
                        Jsonb(row.tags),
                        _point(row.lat, row.lon),
                    )
                )
                counts["pois"] += 1
            elif isinstance(row, RoadWayRow):
                road_ways.write_row((row.way_id, row.highway, row.name, f"SRID=4326;{row.wkt}"))
                counts["road_ways"] += 1
            else:
                members.write_row(
                    (
                        row.relation_id,
                        row.ref,
                        row.name,
                        row.network,
                        row.operator,
                        row.way_id,
                        f"SRID=4326;{row.wkt}",
                    )
                )
                counts["cycle_members"] += 1

    # psycopg commits each connection on clean context exit; a failure
    # rolls them back, and the staging tables are rebuilt from scratch on
    # the next run anyway. Nothing is published until swap() runs.
    return counts


def assemble_cycle_routes(connection: psycopg.Connection[Any]) -> int:
    """Collapse the member ways into one multilinestring per route, and
    separately record each member's own length for coverage.

    Done in SQL rather than Python so the geometry never has to be held in
    memory all at once, and so PostGIS owns the assembly.

    ST_LineMerge stitches contiguous member ways into continuous lines, and
    it is what makes the vector-tile overlay affordable. A collected route
    averages 195 separate parts (NCN 1 has 3,547); merged it averages 11.
    Douglas-Peucker keeps both endpoints of every part no matter the
    tolerance, so without this an unmerged route resists simplification
    entirely - the England-wide tile measured 228 kB simplified against
    50 kB merged first. 98 ms for all 5,545 routes, once, here rather than
    in every tile request.

    Members keep their own geometry as well as their length. The merge is
    what makes the overlay tile cheap, and members are never tiled - but a
    coverage query cannot ask "the network near here" without member
    shapes, because a relation's own envelope spans the whole route. This
    is the last thing read from the raw member table before it is dropped.
    `way_owners` in extract.py already guarantees at most one row per
    (relation_id, way_id), so the length is a plain per-row value rather
    than an aggregate.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("""
                INSERT INTO {} (id, ref, name, network, operator, geom)
                SELECT relation_id, min(ref), min(name), min(network), min(operator),
                       ST_Multi(ST_LineMerge(ST_Collect(geom)))
                FROM {}
                GROUP BY relation_id
            """).format(sql.Identifier(_stage("cycle_ways")), sql.Identifier(RAW_MEMBER_TABLE))
        )
        inserted = cursor.rowcount
        cursor.execute(
            sql.SQL("""
                INSERT INTO {} (relation_id, way_id, length_m, geom)
                SELECT relation_id, way_id, ST_Length(geography(geom)), geom
                FROM {}
            """).format(
                sql.Identifier(_stage("cycle_way_members")), sql.Identifier(RAW_MEMBER_TABLE)
            )
        )
        cursor.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(RAW_MEMBER_TABLE)))
    connection.commit()
    return inserted


def assemble_road_ways(
    connection: psycopg.Connection[Any], tolerance_m: float = ROAD_SIMPLIFY_TOLERANCE_M
) -> int:
    """Measure each way's true length, then simplify its shape before it is
    ever written to the table coverage queries run against.

    Same shape as assemble_cycle_routes: SQL rather than Python so the
    geometry never has to be held in memory all at once, and this is the
    last thing read from the raw table before it is dropped. Unlike cycle
    routes there is nothing to collapse - a road way has no owning relation,
    so this is a 1:1 copy rather than a GROUP BY.

    Length is measured from the *unsimplified* geometry, in the same
    statement that writes the simplified one: PostgreSQL evaluates every
    expression in an UPDATE or INSERT...SELECT against the source row's
    original values, so a coarse tolerance cannot quietly shrink the
    ridden-vs-total figures this feeds - only the stored shape, used solely
    for the bbox filter, gets coarser. ST_Transform to 3857 and back is
    what makes the tolerance mean metres at every latitude England spans;
    ST_Simplify on a raw 4326 geometry would mean degrees, which do not.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("""
                INSERT INTO {} (way_id, highway, name, length_m, geom)
                SELECT way_id, highway, name, ST_Length(geography(geom)),
                       ST_Transform(
                           ST_SimplifyPreserveTopology(ST_Transform(geom, 3857), %s),
                           4326
                       )
                FROM {}
            """).format(sql.Identifier(_stage("osm_ways")), sql.Identifier(RAW_ROAD_WAY_TABLE)),
            (tolerance_m,),
        )
        inserted = cursor.rowcount
        cursor.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(RAW_ROAD_WAY_TABLE)))
    connection.commit()
    return inserted


def publish(
    connection: psycopg.Connection[Any], source_files: list[str], counts: dict[str, int]
) -> None:
    """Replace the live index with the staged one, in one transaction.

    The parse and the COPY - minutes of work - happen outside this, so the
    only thing inside is moving rows that are already in the database.
    Readers block on the TRUNCATE for the length of that move and then see
    the complete new index; nobody ever sees it half-loaded, and rewriting
    the metadata row in the same transaction means a client polling
    /api/config never observes the index as missing.

    An earlier version renamed staged tables over the live ones, which is
    faster still but does not work here: `CREATE TABLE ... (LIKE ...)`
    copies the `nextval(...)` default without creating a new sequence, so
    after a rename the live table depends on a sequence owned by the table
    being dropped, and the drop fails. Dropping with CASCADE would take the
    sequence the live table needs. Moving rows avoids the whole problem.
    """
    with connection.cursor() as cursor:
        # One statement, so the tables are locked in a consistent order and
        # every one of them is replaced or none is.
        cursor.execute(
            sql.SQL("TRUNCATE {}").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in INDEXED_TABLES)
            )
        )
        for table in INDEXED_TABLES:
            columns = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS[table])
            key = sql.SQL(", ").join(sql.Identifier(c) for c in NATURAL_KEY[table])
            # DISTINCT ON the natural key. The in-memory dedup in load() is
            # per extract, and VALHALLA_TILE_URL accepts several - Geofabrik
            # extracts overlap at their borders, so the same OSM node can
            # arrive from two files and would collide with the unique index
            # here, failing the whole build at the last step. Doing it in SQL
            # covers every source of duplication at no memory cost.
            cursor.execute(
                sql.SQL("""
                    INSERT INTO {} ({})
                    SELECT DISTINCT ON ({}) {} FROM {} ORDER BY {}, ctid
                """).format(
                    sql.Identifier(table),
                    columns,
                    key,
                    columns,
                    sql.Identifier(_stage(table)),
                    key,
                )
            )
        cursor.execute("DELETE FROM search_index_meta")
        cursor.execute(
            """
            INSERT INTO search_index_meta
                (built_at, source_files, place_count, poi_count, cycle_way_count,
                 cycle_way_member_count, osm_way_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.now(UTC),
                source_files,
                counts["places"],
                counts["pois"],
                counts["cycle_ways"],
                # Both of the counts below are not null, unlike an install
                # that has never re-indexed since the respective column
                # existed: that NULL is exactly what tells
                # /api/coverage/cycle-network and /api/coverage/roads a
                # rebuild is needed rather than reporting a dishonest 0%.
                #
                # osm_ways is absent, and so NULL, whenever INDEX_ROADS was
                # off - the same signal for the same reason. "Not indexed"
                # and "indexed and you have ridden none of it" must not look
                # alike.
                counts["cycle_members"],
                counts.get("osm_ways"),
            ),
        )
    connection.commit()

    # Outside the publishing transaction: none of this is visible to
    # readers, and there is no reason to hold their lock for it.
    with connection.cursor() as cursor:
        for table in INDEXED_TABLES:
            cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(_stage(table))))
            cursor.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
    connection.commit()
    log.info("index published: %s", counts)


def _copy_sql(table: str, columns: tuple[str, ...]) -> sql.Composed:
    return sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    )


def _point(lat: float, lon: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"
