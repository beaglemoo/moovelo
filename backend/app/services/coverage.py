"""Cycle-network coverage: ridden vs total metres of the signed network, by
tier, for one rider.

Two tables carry this, both added alongside this feature: `cycle_way_members`
(indexer-published, relation_id/way_id/length_m - see models.CycleWayMember)
and `activity_ways` (app-owned, one row per way a rider has ever been
recorded riding - see services/way_matching.py). Coverage joins them on
way_id and filters on cycle_ways.geom, which already carries a GIST index,
rather than on anything in cycle_way_members itself - the spatial filter is
"which routes are near here", not "which of their member ways are".
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SearchIndexMeta

BBox = tuple[float, float, float, float]

# Roughly 5.5 km of latitude either side - a degree of longitude shrinks
# towards the poles, but a degree of latitude does not, so this buffer is
# deliberately expressed in degrees of latitude only and applied uniformly.
# Wide enough that a commute's worth of activities pulls in the surrounding
# network; narrow enough that one wildly out-of-area activity does not blow
# the default bbox out to a scan of the whole index.
DEFAULT_BBOX_BUFFER_DEG = 0.05

_BBOX_SQL = text("""
    SELECT ST_XMin(ext) AS min_lon, ST_YMin(ext) AS min_lat,
           ST_XMax(ext) AS max_lon, ST_YMax(ext) AS max_lat
    FROM (SELECT ST_Extent(geom) AS ext FROM activities WHERE user_id = :user_id) e
    WHERE ext IS NOT NULL
""")

# cw.geom && the envelope is what makes this index-backed (ix_cycle_ways_geom,
# GIST) rather than a scan of the whole national index - the same technique
# services/places.py's cycle-network tile query and services/heatmap.py both
# already use, transforming the (constant) query bound rather than every row.
# The LEFT JOIN's own ON clause carries the user_id filter, not a WHERE
# added after it - moving it to WHERE would turn the LEFT JOIN into an inner
# one and silently drop every unridden way, which is exactly backwards for a
# coverage denominator.
#
# Two things here were measured on the England extract rather than reasoned
# about, and both were wrong in the obvious implementation:
#
# The bbox is tested against the member way, not the route it belongs to. A
# relation's envelope is the envelope of the whole route, and NCN 1's covers
# most of the country - so filtering on cycle_ways.geom pulled 8,301 member
# ways totalling 1,921 km into a 12 km box around Tring that actually holds
# 125 km of network. Fifteen times the real answer.
#
# DISTINCT ON collapses a way that carries more than one route of the same
# tier. 43,902 of 187,392 member ways (23%) belong to several relations, and
# summing them per relation inflates the national denominator from 33,968 km
# to 43,898 km. Worse than the size of the error, it is uneven: it would
# reward a rider whose miles happened to fall on multiplexed sections.
#
# Deduplication is per tier, not global, and that is deliberate. A towpath
# carrying both an NCN route and a local one is genuinely part of both
# networks, and each tier's percentage is reported on its own - nothing sums
# them into a single figure, which is the only thing that would double count.
_COVERAGE_SQL = text("""
    SELECT network,
           SUM(length_m) AS total_m,
           SUM(CASE WHEN ridden THEN length_m ELSE 0 END) AS ridden_m
    FROM (
        SELECT DISTINCT ON (cw.network, cwm.way_id)
               cw.network AS network,
               cwm.length_m AS length_m,
               (aw.way_id IS NOT NULL) AS ridden
        FROM cycle_way_members cwm
        JOIN cycle_ways cw ON cw.id = cwm.relation_id
        LEFT JOIN activity_ways aw
               ON aw.way_id = cwm.way_id AND aw.user_id = :user_id
        WHERE cwm.geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
        ORDER BY cw.network, cwm.way_id, ridden DESC
    ) member
    GROUP BY network
""")


async def index_meta(db: AsyncSession) -> SearchIndexMeta | None:
    """The search index's single metadata row, or None if it has never been
    built. Kept apart from the coverage query itself so the caller can tell
    "never built" and "built before this feature existed" apart - both need
    to say so plainly rather than let a missing join silently return 0%."""
    return (await db.execute(select(SearchIndexMeta))).scalar_one_or_none()


async def default_bbox(db: AsyncSession, user_id: uuid.UUID) -> BBox | None:
    """A bbox around wherever this rider has actually ridden, buffered so
    the surrounding network - ridden or not - is included in both the
    numerator and the denominator. None when they have imported nothing to
    centre one on.
    """
    row = (await db.execute(_BBOX_SQL, {"user_id": user_id})).one_or_none()
    if row is None:
        return None
    min_lon, min_lat, max_lon, max_lat = row
    return (
        min_lon - DEFAULT_BBOX_BUFFER_DEG,
        min_lat - DEFAULT_BBOX_BUFFER_DEG,
        max_lon + DEFAULT_BBOX_BUFFER_DEG,
        max_lat + DEFAULT_BBOX_BUFFER_DEG,
    )


async def cycle_network_coverage(
    db: AsyncSession, user_id: uuid.UUID, bbox: BBox
) -> list[tuple[str, float, float]]:
    """Ridden vs total metres per network tier within `bbox`, as
    (network, total_m, ridden_m). Empty when nothing in the index intersects
    it - a genuine answer, not a failure.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    rows = (
        await db.execute(
            _COVERAGE_SQL,
            {
                "user_id": user_id,
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            },
        )
    ).all()
    return [(row.network, float(row.total_m), float(row.ridden_m)) for row in rows]
