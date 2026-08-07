"""Searching the offline place index.

Ranking is a weighted blend of three signals, tuned against the real
England index (73,000 places, of which 21,848 share a name with at least
one other - so none of these three is optional).
"""

import unicodedata

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import PlaceResult

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
# Distance at which the proximity term halves. Small enough that the next
# town over still counts as "here", large enough that it does not reduce
# to a straight nearest-first sort.
PROXIMITY_HALF_LIFE_M = 20_000.0

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
