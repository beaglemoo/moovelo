"""Match a recorded ride (Activity) to the saved Route it followed.

Two stages, the same shape as services/places.py's POI query: cheap,
index-backed narrowing first, then an exact geometric test on the
survivors only.

Narrowing is a bounding-box overlap (`routes.geom && activities.geom`,
GiST-backed - Route.geom's index is `ix_routes_geom` in the migration but
`idx_routes_geom` in the ORM's own metadata, a harmless naming divergence
noted on Activity.geom's index in models.py; `&&` is served regardless of
what the index is called) plus a distance ratio pre-filter, capped so a
pathological library cannot make this unbounded.

Confirmation is ST_FrechetDistance, which measures how far apart two lines
are shaped, not merely how close their endpoints are - PostGIS 3.5 is
pinned (docker-compose.yml) and nothing else in the repo uses this function
yet. It has no geography form, so both lines are projected to EPSG:3857 and
simplified before the O(n*m) comparison runs.

Two corrections on top of the raw Frechet result, both required for the
number to mean what it claims to:

Web Mercator's scale is not uniform - it grows as 1/cos(latitude), so a
"150m" Frechet distance computed in 3857 is really about 93m on the ground
at the UK's ~51.8N (1/cos(51.8 deg) =~ 1.62). Multiplying by cos(latitude)
of the activity's own centroid converts back to true ground metres. Skip
this and the threshold below is silently ~1.62x stricter than it claims to
be at UK latitudes - see test_route_match.py's dedicated test, which fails
if the correction is removed.

Frechet distance is direction-sensitive (it walks both lines start to
start), so a route ridden in reverse scores as if it were a completely
different shape. The match is computed against both the route and its
reverse, and the smaller of the two wins.
"""

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity

logger = logging.getLogger(__name__)

# Provisional. Chosen as a round number comfortably above GPS/Valhalla
# routing noise and comfortably below "a different road" - not yet tuned
# against real ride/route geometry. Staging verification (real GPS traces
# against their planned routes) is expected to move this.
MAX_MATCH_DISTANCE_M = 150.0

# A route within roughly half to double the activity's own distance is
# plausible; anything further out is cheap to discard before the expensive
# Frechet comparison ever runs.
CANDIDATE_DISTANCE_RATIO_MIN = 0.5
CANDIDATE_DISTANCE_RATIO_MAX = 2.0

# Bounds the O(n*m) Frechet comparisons a pathological library (or a bbox
# that happens to sit over a dense cluster of saved routes) could otherwise
# force.
MAX_CANDIDATES = 25

# Metres of tolerance for ST_Simplify before the Frechet comparison, in the
# projected (3857) units. Keeps the point count - and so the O(n*m) cost -
# sane on multi-thousand-point recorded traces without discarding the shape
# the match actually depends on.
SIMPLIFY_TOLERANCE_M = 20.0

# `act` narrows to the one activity being matched and its owning user, so
# every candidate route already belongs to that same user - there is no
# separate ownership filter to forget. `candidates` is the cheap,
# index-backed stage: `&&` uses the GiST index on each geom column, and the
# distance-ratio filter discards obvious non-matches before anything
# geometric runs. `scored` is the exact stage, on survivors only: both
# geometries are projected to 3857 (Frechet has no geography form) and
# simplified before the O(n*m) comparison; ST_Reverse on the candidate
# covers a route ridden backwards, and LEAST() takes whichever direction
# scored closer. The final `* act.lat_cos` converts the projected distance
# back to true ground metres - see the module docstring.
_MATCH_SQL = text("""
    WITH act AS (
        SELECT id, user_id, geom,
               ST_Transform(geom, 3857) AS geom_m,
               distance_m,
               cos(radians(ST_Y(ST_Centroid(geom)))) AS lat_cos
        FROM activities
        WHERE id = :activity_id
    ),
    candidates AS (
        SELECT r.id, ST_Transform(r.geom, 3857) AS geom_m
        FROM routes r, act
        WHERE r.user_id = act.user_id
          AND r.geom && act.geom
          AND r.distance_m BETWEEN
              act.distance_m * :ratio_min AND act.distance_m * :ratio_max
        LIMIT :max_candidates
    ),
    scored AS (
        SELECT c.id,
               LEAST(
                   ST_FrechetDistance(
                       ST_Simplify(act.geom_m, :simplify_m),
                       ST_Simplify(c.geom_m, :simplify_m)
                   ),
                   ST_FrechetDistance(
                       ST_Simplify(act.geom_m, :simplify_m),
                       ST_Simplify(ST_Reverse(c.geom_m), :simplify_m)
                   )
               ) * act.lat_cos AS distance_m
        FROM candidates c, act
    )
    SELECT id, distance_m
    FROM scored
    ORDER BY distance_m ASC
    LIMIT 1
""")


async def match_activity_to_route(db: AsyncSession, activity_id: uuid.UUID) -> uuid.UUID | None:
    """Find the best-matching route for one activity and persist the result.

    A no-op - returns None without touching anything - when the activity
    does not exist, or when `match_locked` is true: that flag means a rider
    already made this decision by hand, and an auto-match run must never
    overwrite it.

    Only ever considers routes owned by the activity's own user (enforced
    in the SQL itself via `act.user_id`, not by a filter this function has
    to remember to apply).

    Returns the winning route id, or None if nothing scored under
    MAX_MATCH_DISTANCE_M - in which case any existing link is left alone
    rather than cleared, since "no candidate beat the threshold this time"
    is not evidence the previous match was wrong.
    """
    activity = await db.get(Activity, activity_id)
    if activity is None or activity.match_locked:
        return None

    row = (
        await db.execute(
            _MATCH_SQL,
            {
                "activity_id": activity_id,
                "ratio_min": CANDIDATE_DISTANCE_RATIO_MIN,
                "ratio_max": CANDIDATE_DISTANCE_RATIO_MAX,
                "max_candidates": MAX_CANDIDATES,
                "simplify_m": SIMPLIFY_TOLERANCE_M,
            },
        )
    ).first()
    if row is None or row.distance_m is None or row.distance_m > MAX_MATCH_DISTANCE_M:
        return None

    route_id: uuid.UUID = row.id
    activity.route_id = route_id
    activity.match_confidence = float(row.distance_m)
    await db.commit()
    return route_id
