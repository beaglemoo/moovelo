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
