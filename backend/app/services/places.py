"""Searching the offline place index.

Ranking is a weighted blend of three signals, tuned against the real
England index (73,000 places, of which 21,848 share a name with at least
one other - so none of these three is optional).
"""

import unicodedata
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import MAX_POI_RESULTS, PlaceResult, PoiResult, PoisAlongRoute

# Trigrams need three characters to mean anything, so shorter queries lean
# entirely on the prefix branch. Below two characters there is nothing
# worth returning at all.
MIN_QUERY_LENGTH = 2
MAX_LIMIT = 20

# Anything less similar than this is noise. Applied through the % operator
# rather than a similarity() comparison because only the operator can use
# the trigram index.
SIMILARITY_THRESHOLD = 0.25

TEXT_WEIGHT = 0.55
IMPORTANCE_WEIGHT = 0.30
PROXIMITY_WEIGHT = 0.15
# Distance at which the proximity term halves.
#
# Not "how local the search feels", which is how this was first reasoned
# about and why it was originally set to 20 km. `1/(1 + d/H)` saturates at
# both ends: far below H every candidate scores near 1, far above it every
# candidate scores near 0, and in both cases proximity stops separating
# anything. It discriminates over distances comparable to H - so H should
# match the distances actually being compared, and "which of England's
# several Newports did you mean" is a 50-200 km question.
#
# Measured over 60 of the most duplicated names from 4 map centres, by the
# median distance of the top result: 154 km at H=5 km, 109 km at 20 km,
# 80 km at 80 km, 91 km at 160 km, 110 km at 400 km. A clean U, minimum
# around 80 km. Raising it to 80 km changed nothing at all across 30
# unambiguous city queries, so the improvement is not bought by burying
# obvious answers.
PROXIMITY_HALF_LIFE_M = 80_000.0

# An exact match beats a prefix match beats a fuzzy one. Deliberately
# coarse: the point is to separate the tiers, and importance and distance
# order things within them.
_TEXT_SCORE = """
    CASE WHEN p.name_norm = :term THEN 1.0
         WHEN p.name_norm LIKE :prefix ESCAPE '\\' THEN 0.85
         ELSE similarity(p.name_norm, :term) END
"""

# The map centre is built once in a CTE, with explicit casts: asyncpg
# infers parameter types from context, and a bare :lat inside
# `CASE WHEN :lat IS NULL` gives it nothing to work from - it fails with
# "could not determine data type of parameter $1".
_SEARCH_SQL = text(f"""
    WITH centre AS (
        SELECT CASE
            WHEN CAST(:lat AS double precision) IS NULL THEN NULL
            ELSE ST_SetSRID(
                ST_MakePoint(CAST(:lon AS double precision), CAST(:lat AS double precision)),
                4326
            )::geography
        END AS point
    )
    SELECT p.id, p.name, p.place_type,
           ST_Y(p.geog::geometry) AS lat,
           ST_X(p.geog::geometry) AS lon,
           CASE WHEN centre.point IS NULL THEN NULL
                ELSE ST_Distance(p.geog, centre.point) END AS distance_m
    FROM places p, centre
    WHERE p.name_norm LIKE :prefix ESCAPE '\\' OR p.name_norm % :term
    ORDER BY (
        {TEXT_WEIGHT} * ({_TEXT_SCORE})
      + {IMPORTANCE_WEIGHT} * p.importance
      + {PROXIMITY_WEIGHT} * CASE WHEN centre.point IS NULL THEN 0.0
            ELSE 1.0 / (1.0 + ST_Distance(p.geog, centre.point) / {PROXIMITY_HALF_LIFE_M})
        END
    ) DESC
    LIMIT :limit
""")


# How far a place may lend its name, at maximum importance. Also the outer
# bound of the index scan, since reach can never exceed it.
REVERSE_MAX_REACH_M = 50_000.0

# Floor on importance, so a future category weighted at zero cannot divide
# by it. The indexer's lowest today is 0.10.
MIN_IMPORTANCE = 0.05

# A place's reach grows with the square of its importance, which turns the
# indexer's weights into naming radii: a city carries about 48 km, a town
# 21 km, a village 8 km, a hamlet 3 km, a locality 1.1 km. Ranking by
# distance/reach then asks "how far into its natural range am I", not "what
# is closest".
_REACH = f"({REVERSE_MAX_REACH_M} * GREATEST(p.importance, {MIN_IMPORTANCE}) ^ 2)"

# Written out rather than built in a CTE so the KNN operator sees a
# pseudo-constant and can drive an index scan on ix_places_geog. The casts
# are for asyncpg, which infers parameter types from context.
_POINT = (
    "ST_SetSRID("
    "ST_MakePoint(CAST(:lon AS double precision), CAST(:lat AS double precision)), 4326"
    ")::geography"
)

# Nearest-wins was the obvious design and it is wrong. Over a third of
# England's 73,084 indexed places are place=locality, OSM's catch-all for
# named spots nobody lives in: field corners, bridges, trailheads, sandbanks
# out at sea. Ranked by distance alone the index answers "Ivinghoe Beacon"
# with "The Ridgeway Trailhead (Northeast Side)" and a point in open
# farmland with "Dixon's Gap Bridge". Both are the nearest named thing and
# neither is where you are.
#
# Reach fixes that without a whitelist: a locality has to be within about a
# kilometre to win, while the village up the lane reaches you from five. The
# same farmland point now answers "Wilstone".
_REVERSE_SQL = text(f"""
    SELECT p.id, p.name, p.place_type,
           ST_Y(p.geog::geometry) AS lat,
           ST_X(p.geog::geometry) AS lon,
           ST_Distance(p.geog, {_POINT}) AS distance_m
    FROM places p
    WHERE ST_DWithin(p.geog, {_POINT}, {REVERSE_MAX_REACH_M})
      AND ST_Distance(p.geog, {_POINT}) <= {_REACH}
    ORDER BY ST_Distance(p.geog, {_POINT}) / {_REACH}
    LIMIT 1
""")


def normalise(name: str) -> str:
    """Fold accents and case, exactly as the indexer does when writing.

    Must stay in step with indexer/indexer/geometry.py:normalise. The two
    are separate deployables with separate dependencies, so this is
    duplicated rather than shared - but if they ever diverge, accented
    names silently stop matching, which is why both sides have a test.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().strip()


def escape_like(term: str) -> str:
    """A place called "St. Michael's" must not become a wildcard."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_places(
    db: AsyncSession,
    query: str,
    near: tuple[float, float] | None = None,
    limit: int = 8,
) -> list[PlaceResult]:
    term = normalise(query)
    if len(term) < MIN_QUERY_LENGTH:
        return []

    # SET LOCAL, so the threshold applies to this transaction only and the
    # pooled connection goes back unchanged.
    await db.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {SIMILARITY_THRESHOLD}"))
    rows = await db.execute(
        _SEARCH_SQL,
        {
            "term": term,
            "prefix": escape_like(term) + "%",
            "lat": near[0] if near else None,
            "lon": near[1] if near else None,
            "limit": min(limit, MAX_LIMIT),
        },
    )
    return [
        PlaceResult(
            id=row.id,
            name=row.name,
            place_type=row.place_type,
            lat=row.lat,
            lon=row.lon,
            distance_m=row.distance_m,
        )
        for row in rows
    ]


async def reverse_geocode(db: AsyncSession, lat: float, lon: float) -> PlaceResult | None:
    """Name a point: the place whose reach covers it most comfortably.

    None is a normal answer, not an error: the index may not be built, and
    a point can genuinely be out of everywhere's range.
    """
    row = (await db.execute(_REVERSE_SQL, {"lat": lat, "lon": lon})).first()
    if row is None:
        return None
    return PlaceResult(
        id=row.id,
        name=row.name,
        place_type=row.place_type,
        lat=row.lat,
        lon=row.lon,
        distance_m=row.distance_m,
    )


# The route arrives as a linestring literal rather than a stored row: the
# question is asked while planning, before anything is saved.
#
# ix_pois_geog carries this. Against a real Tring to Oxford ride - 1,773
# points, 50 km - the index offers 1,740 candidates from the line's bounding
# box, the exact distance test keeps 722, and the whole thing takes 26 ms.
# Without the geography column it would be a sequential scan of 285,000 POIs.
#
# ST_LineLocatePoint needs plain geometry and it runs only over the
# survivors. It is fed web mercator, NOT the stored 4326: on unprojected
# lon/lat it treats a degree of longitude and a degree of latitude as the
# same unit, and at 51N a degree of longitude is only about 63% as long. A
# route 20 km east then 20 km north reported a POI at the corner as 24,917 m
# along a 40,382 m ride when the true figure is 20,357 m - 22% out. Mercator
# is conformal, so both axes scale together and the same case comes back at
# 20,311 m. The one test that existed used a purely east-west line, the
# single geometry where the distortion vanishes.
#
# On a route that doubles back it reports the first pass, which is the
# honest reading of "how far along".
#
# The category array is CAST explicitly: asyncpg infers parameter types from
# context, and an empty list gives it nothing - "could not determine
# polymorphic type because input has type unknown".
#
# The cap is spread along the route rather than taken from the front. POI
# density is wildly uneven - a ride out of Oxford had 498 POIs within 500 m,
# and the first 301 by distance-along all fell inside the opening 233 m, so
# the answer described 0.6% of a 38 km ride while reporting only
# "truncated". Bucketing by position and keeping the nearest few per bucket
# guarantees the whole ride is represented; the window count reports how
# many were dropped.
POI_BUCKETS = 50
POI_PER_BUCKET = MAX_POI_RESULTS // POI_BUCKETS

_POIS_SQL = text(f"""
    WITH r AS (
        SELECT line AS geom, ST_Transform(line, 3857) AS geom_m,
               line::geography AS geog, ST_Length(line::geography) AS len
        FROM (SELECT ST_SetSRID(ST_GeomFromText(:wkt), 4326) AS line) s
    ),
    near AS (
        SELECT p.id, p.name, p.category, p.osm_tags,
               ST_Y(p.geog::geometry) AS lat,
               ST_X(p.geog::geometry) AS lon,
               ST_Distance(p.geog, r.geog) AS dist_from_route_m,
               ST_LineLocatePoint(r.geom_m, ST_Transform(p.geog::geometry, 3857))
                   * r.len AS dist_along_m,
               r.len AS route_len
        FROM pois p, r
        WHERE ST_DWithin(p.geog, r.geog, :radius)
          AND (cardinality(CAST(:categories AS text[])) = 0
               OR p.category = ANY(CAST(:categories AS text[])))
    ),
    ranked AS (
        SELECT near.*,
               count(*) OVER () AS candidate_count,
               ROW_NUMBER() OVER (
                   PARTITION BY width_bucket(
                       dist_along_m, 0, GREATEST(route_len, 1), {POI_BUCKETS}
                   )
                   ORDER BY dist_from_route_m
               ) AS rank_in_bucket
        FROM near
    )
    SELECT id, name, category, osm_tags, lat, lon,
           dist_from_route_m, dist_along_m, candidate_count
    FROM ranked
    WHERE rank_in_bucket <= {POI_PER_BUCKET}
    ORDER BY dist_along_m
    LIMIT {MAX_POI_RESULTS}
""")


# Vector tiles rather than a bbox GeoJSON endpoint: England's signed network
# is 5,545 relations and 43,897 km, so a refetch on every pan would be
# multi-megabyte with simplification pushed onto the browser. MVT is clipped,
# quantised and cacheable per tile.
#
# Which networks appear depends on zoom, the way a paper map drops detail
# rather than drawing everything at every scale. All four at street level;
# no local routes when looking at a county; national and international only
# when looking at the country.
MIN_CYCLE_ZOOM = 4
MAX_CYCLE_ZOOM = 16

# Every tile coordinate is cast. asyncpg infers parameter types from
# context, and the `2 ^ :z` below would otherwise type :z as double
# precision for the whole statement - which fails with "function
# st_tileenvelope(double precision, unknown, unknown) does not exist".
_Z = "CAST(:z AS integer)"
_ENVELOPE = f"ST_TileEnvelope({_Z}, CAST(:x AS integer), CAST(:y AS integer))"

_NETWORK_BY_ZOOM = f"""
    CASE WHEN {_Z} >= 11 THEN true
         WHEN {_Z} >= 8 THEN network IN ('icn', 'ncn', 'rcn')
         ELSE network IN ('icn', 'ncn') END
"""

# Web mercator spans 40,075,016.7 m, and a tile carries 4,096 units, so this
# is one tile unit in metres - simplifying below what the tile can express
# costs nothing visible. The indexer runs ST_LineMerge before storing, which
# is what lets this actually bite.
_TOLERANCE = "40075016.7 / (2 ^ CAST(:z AS double precision) * 4096)"

# ST_Transform is applied to the tile envelope, not to every row: the
# envelope is a constant, so ix_cycle_ways_geom still serves the bounding-box
# filter. Transforming each row instead would disable it.
_TILE_SQL = text(f"""
    SELECT ST_AsMVT(tile, 'cycle_ways', 4096, 'geom') AS mvt
    FROM (
        SELECT id, ref, name, network,
               ST_AsMVTGeom(
                   ST_Simplify(ST_Transform(geom, 3857), {_TOLERANCE}),
                   {_ENVELOPE}, 4096, 64, true
               ) AS geom
        FROM cycle_ways
        WHERE geom && ST_Transform({_ENVELOPE}, 4326)
          AND ({_NETWORK_BY_ZOOM})
    ) AS tile
    WHERE tile.geom IS NOT NULL
""")


async def cycle_network_tile(db: AsyncSession, z: int, x: int, y: int) -> bytes:
    """One vector tile of the signed cycle network.

    An empty tile is a normal answer - most of the world has no NCN - and
    MapLibre handles zero-length bodies fine, so this never 404s.
    """
    mvt = await db.scalar(_TILE_SQL, {"z": z, "x": x, "y": y})
    return bytes(mvt) if mvt else b""


def linestring_wkt(line: Sequence[tuple[float, float]]) -> str:
    """WKT from [lon, lat] pairs. Seven decimals is about a centimetre."""
    return "LINESTRING(" + ",".join(f"{lon:.7f} {lat:.7f}" for lon, lat in line) + ")"


async def pois_along_route(
    db: AsyncSession,
    line: Sequence[tuple[float, float]],
    radius_m: float,
    categories: Sequence[str],
) -> PoisAlongRoute:
    """Everything useful within `radius_m` of the line, ordered along it."""
    rows = list(
        await db.execute(
            _POIS_SQL,
            {
                "wkt": linestring_wkt(line),
                "radius": radius_m,
                "categories": list(categories),
            },
        )
    )
    # The window count is over every candidate within the radius, before the
    # per-bucket cap, so this reports what was actually dropped rather than
    # whether the page happened to fill up.
    candidates = rows[0].candidate_count if rows else 0
    truncated = candidates > len(rows)
    return PoisAlongRoute(
        pois=[
            PoiResult(
                id=row.id,
                name=row.name,
                category=row.category,
                lat=row.lat,
                lon=row.lon,
                dist_from_route_m=row.dist_from_route_m,
                dist_along_m=row.dist_along_m,
                tags={str(k): str(v) for k, v in (row.osm_tags or {}).items()},
            )
            for row in rows
        ],
        truncated=truncated,
    )
