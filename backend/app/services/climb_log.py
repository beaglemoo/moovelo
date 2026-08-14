"""The personal climb log: every climb a rider has actually ridden,
deduplicated so the same hill across many rides is one entry.

`Climb` (services/climbs.py) carries no coordinates - it is purely two
cumulative distances into one ride's own elevation profile - so a real
position has to be recovered from the activity's own recorded line.
`ST_LineInterpolatePoint(activity.geom, start_dist_m / activity.distance_m)`
walks that fraction of the way along the trace and returns the point there.
This is an approximation, not an exact arc-length position:
`ST_LineInterpolatePoint` parameterises by 2D Cartesian length in the
geometry's own SRID-4326 degree space, while `start_dist_m` is a true-ground
haversine distance (services/geo.py's `cumulative_distances`, which is what
built the ride's own `distance_m` in the first place) - close enough to
place a climb on a map and to cluster it against others nearby, not a
distance decision in its own right, so unlike services/route_match.py this
never needs an EPSG:3857 Mercator correction: nothing here is projected to
3857, or measured in map units, at all. Clustering below measures the
distance between two *already-recovered* lat/lon points with
services/geo.py's `haversine`, which is true ground metres by construction -
there is no distortion to correct for a second time.

Both `activity.distance_m` and a stored `Climb`'s `start_dist_m`/`end_dist_m`
can disagree very slightly with the *2D* length PostGIS would compute for
the same line, but only by the same small margin haversine and a flat
Cartesian approximation always disagree by over a short distance - not
enough to visibly move a climb marker, and irrelevant to the tolerance
`CLUSTER_DISTANCE_TOLERANCE_M` clusters at.
"""

import math
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import ClimbAscent, ClimbLogEntry
from app.services.geo import Point, haversine

# Both provisional - chosen to read as "obviously the same hill, ridden
# again" rather than tuned against real ride/route geometry. Staging
# verification with genuine GPS traces is expected to move both, the same
# caveat services/route_match.py's own COVERAGE_BUFFER_M and MIN_COVERAGE
# carry for the same reason (untuned geometric tolerances).
CLUSTER_DISTANCE_TOLERANCE_M = 200.0
CLUSTER_LENGTH_TOLERANCE_PCT = 20.0

# A safety net, not the mechanism that keeps this fast - the bucketed
# pre-filter below is. 20,000 is comfortably beyond "years of history": a
# rider who rode every single day for a decade and found three climbs on
# every ride is under 11,000 rows.
MAX_CLIMB_ROWS = 20_000

# Sized so any two points within CLUSTER_DISTANCE_TOLERANCE_M of each other
# always land in the same grid cell or an adjacent one - `_candidate_keys`
# below always searches the full 3x3 neighbourhood, so a cell edge can never
# hide a real match. Degrees of latitude do not shrink towards the poles the
# way degrees of longitude do, so sizing the grid uniformly from this
# latitude-only constant makes each cell slightly *wider* than the
# tolerance in longitude at any latitude away from the equator - harmless:
# an oversized cell only ever costs a few extra (correctly rejected)
# comparisons, never a missed match, which an undersized cell risks.
_METRES_PER_DEGREE_LATITUDE = 111_320.0
_GRID_CELL_DEG = CLUSTER_DISTANCE_TOLERANCE_M / _METRES_PER_DEGREE_LATITUDE

# `jsonb_array_elements` explodes each activity's own `climbs` column - one
# row per detected climb, joined back to the activity that carries it via a
# LATERAL join rather than a subquery per climb, so this is one query
# however many climbs a rider has, not one per activity.
#
# `frac` clamps each fraction into [0, 1] (LEAST/GREATEST) and guards a
# zero-length activity turning the division into an error (NULLIF) rather
# than merely producing a NULL fraction - `climb_log` below drops any row
# whose recovered point comes back NULL, so a zero-length or otherwise
# unusable activity is skipped rather than raising.
#
# `a.user_id = :user_id` is the one line that matters most here: getting it
# wrong is invisible with a single test account and would silently draw
# every rider's climbs into every rider's log - see
# test_climb_log.py's dedicated cross-user tests, which pin exactly this.
_CLIMB_ROWS_SQL = text("""
    SELECT
        a.id AS activity_id,
        a.started_at,
        a.moving_time_s,
        a.elapsed_time_s,
        a.distance_m AS activity_distance_m,
        c.value ->> 'category' AS category,
        (c.value ->> 'length_m')::float8 AS length_m,
        (c.value ->> 'gain_m')::float8 AS gain_m,
        (c.value ->> 'avg_grade_pct')::float8 AS avg_grade_pct,
        ST_Y(ST_LineInterpolatePoint(a.geom, frac.start_frac)) AS start_lat,
        ST_X(ST_LineInterpolatePoint(a.geom, frac.start_frac)) AS start_lon
    FROM activities a
    CROSS JOIN LATERAL jsonb_array_elements(a.climbs) AS c(value)
    CROSS JOIN LATERAL (
        SELECT
            LEAST(GREATEST(
                (c.value ->> 'start_dist_m')::float8 / NULLIF(a.distance_m, 0), 0
            ), 1) AS start_frac
    ) AS frac
    WHERE a.user_id = :user_id
    ORDER BY a.started_at NULLS LAST, a.created_at
    LIMIT :max_rows
""")


@dataclass
class _Cluster:
    lat: float
    lon: float
    category: str
    length_m: float
    gain_m: float
    avg_grade_pct: float
    ascents: list[ClimbAscent] = field(default_factory=list)

    def to_entry(self) -> ClimbLogEntry:
        return ClimbLogEntry(
            lat=self.lat,
            lon=self.lon,
            category=self.category,
            length_m=self.length_m,
            gain_m=self.gain_m,
            avg_grade_pct=self.avg_grade_pct,
            ascent_count=len(self.ascents),
            ascents=self.ascents,
        )


def _bucket_key(point: Point) -> tuple[int, int]:
    lat, lon = point
    return (math.floor(lat / _GRID_CELL_DEG), math.floor(lon / _GRID_CELL_DEG))


def _candidate_keys(key: tuple[int, int]) -> Iterator[tuple[int, int]]:
    """The 3x3 neighbourhood of grid cells - see `_GRID_CELL_DEG`'s own
    comment for why searching just these guarantees nothing within
    CLUSTER_DISTANCE_TOLERANCE_M is ever missed."""
    bi, bj = key
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            yield (bi + di, bj + dj)


def _lengths_are_similar(a: float, b: float) -> bool:
    longer = max(a, b)
    if longer <= 0:
        return a == b
    return abs(a - b) / longer <= CLUSTER_LENGTH_TOLERANCE_PCT / 100.0


def _ascent_time_s(
    moving_time_s: float | None,
    elapsed_time_s: float | None,
    activity_distance_m: float,
    climb_length_m: float,
) -> float | None:
    """The climb's share of the ride's own moving time (falling back to
    elapsed time when moving time is unknown), spread proportionally over
    distance.

    This is the only time source available: `Activity` stores an aggregate
    elapsed/moving time and a distance, never a per-point timestamp series
    (those exist transiently on the parsed `TrackPoint`, see
    services/importer.py, but are not persisted). The assumption this makes
    explicit is that the rider's average speed on the climb matches their
    average speed over the whole ride - which is false in the direction
    that matters (climbs are slower than a ride's flat and downhill
    sections, so this systematically *underestimates* time actually spent
    climbing) and is knowingly a coarse first cut rather than a real
    per-climb speed model.
    """
    total_time_s = moving_time_s if moving_time_s is not None else elapsed_time_s
    if total_time_s is None or activity_distance_m <= 0:
        return None
    return total_time_s * (climb_length_m / activity_distance_m)


def _cluster(rows: Sequence[Row[Any]]) -> list[ClimbLogEntry]:
    """Group climb rows into log entries.

    Bounded, not quadratic: a new row only ever compares against clusters
    whose OWN representative point already sits in one of its 9 neighbour
    grid cells (see `_GRID_CELL_DEG`), not against every climb seen so far.
    Once a hill has one cluster, every later ascent of the same hill costs
    one comparison (the cluster it already matches) rather than one per
    prior ascent - a hill ridden 500 times stays O(500), not O(500^2). The
    worst case is many genuinely distinct climbs crammed into the same
    small area (a city with dozens of named hills within a few hundred
    metres of each other); MAX_CLIMB_ROWS bounds that pathological case
    from the other side.
    """
    clusters: list[_Cluster] = []
    buckets: dict[tuple[int, int], list[int]] = {}

    for row in rows:
        point = (row.start_lat, row.start_lon)
        match_index: int | None = None
        for key in _candidate_keys(_bucket_key(point)):
            for index in buckets.get(key, []):
                cluster = clusters[index]
                close_enough = haversine(point, (cluster.lat, cluster.lon)) <= (
                    CLUSTER_DISTANCE_TOLERANCE_M
                )
                if close_enough and _lengths_are_similar(row.length_m, cluster.length_m):
                    match_index = index
                    break
            if match_index is not None:
                break

        if match_index is None:
            match_index = len(clusters)
            clusters.append(
                _Cluster(
                    lat=point[0],
                    lon=point[1],
                    category=row.category,
                    length_m=row.length_m,
                    gain_m=row.gain_m,
                    avg_grade_pct=row.avg_grade_pct,
                )
            )
            buckets.setdefault(_bucket_key(point), []).append(match_index)

        clusters[match_index].ascents.append(
            ClimbAscent(
                activity_id=row.activity_id,
                started_at=row.started_at,
                time_s=_ascent_time_s(
                    row.moving_time_s,
                    row.elapsed_time_s,
                    row.activity_distance_m,
                    row.length_m,
                ),
            )
        )

    # Most-ridden hills first - the question this log answers is "what do I
    # keep climbing", not "what is nearby" (there is no map centre here to
    # rank by distance from) or "what is steepest" (that is what the
    # per-ride ClimbsList already shows). Ties broken by average grade, the
    # closest thing to a fixed severity ordering CATEGORY_THRESHOLDS itself
    # implies without importing it here as a second ranking key.
    clusters.sort(key=lambda cluster: (-len(cluster.ascents), -cluster.avg_grade_pct))
    return [cluster.to_entry() for cluster in clusters]


async def climb_log(db: AsyncSession, user_id: uuid.UUID) -> list[ClimbLogEntry]:
    """This rider's own deduplicated climb log, newest ascent order within
    each entry preserved from the query's own ORDER BY."""
    rows = (
        await db.execute(_CLIMB_ROWS_SQL, {"user_id": user_id, "max_rows": MAX_CLIMB_ROWS})
    ).all()
    # A row's recovered point is NULL when the activity's own distance_m was
    # zero or missing (NULLIF above), which ST_LineInterpolatePoint cannot
    # place anywhere - skipped rather than clustered against nothing.
    usable = [row for row in rows if row.start_lat is not None and row.start_lon is not None]
    return _cluster(usable)
