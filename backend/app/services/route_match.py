"""Match a recorded ride (Activity) to the saved Route it followed.

Two stages, the same shape as services/places.py's POI query: cheap,
index-backed narrowing first, then an exact geometric test on the
survivors only.

Narrowing is a bounding-box overlap (`routes.geom && activities.geom`,
GiST-backed - Route.geom's index is `ix_routes_geom` in the migration but
`idx_routes_geom` in the ORM's own metadata, a harmless naming divergence
noted on Activity.geom's index in models.py; `&&` is served regardless of
what the index is called) plus a distance-ratio pre-filter on the same band
the decision itself uses, ordered by closeness of distance and capped so a
pathological library cannot make this unbounded.

Confirmation was originally going to be ST_FrechetDistance thresholded
against a fixed distance. Measured against real dev Postgres before
building on that plan, two things turned out to be wrong with it:

**It is a maximum-based metric, and real rides have outliers.** A ride that
follows its route exactly except for one ~300m detour (a shop stop, a wrong
turn, a loop round a car park) scored a 300.6m Frechet distance - over any
sane threshold - while 85%+ of the ride was genuinely on the route. Frechet
and Hausdorff both let a single excursion veto an otherwise obvious match,
which is precisely the feature failing at its job. Confirmation is instead
**bidirectional coverage**: the fraction of the ride's length that falls
within a tolerance buffer of the route, and the fraction of the route's
length that falls within a tolerance buffer of the ride, both measured
after both lines are projected to EPSG:3857 (so length and buffer distance
are in the same, if locally distorted, unit) and simplified. A short
detour drags the ratio down without the rest of the ride ever being able
to veto the match on its own. Bidirectional matters on its own: one
direction alone would let a ride that only covered half a route (or an
out-and-back that doubles a one-way route) pass on ride-coverage while
badly under- or over-covering the route itself.

**An unsimplified real trace can crash the database, not merely run
slowly.** A 14,000-point activity (an ordinary four-hour ride recorded at
1Hz) run through ST_FrechetDistance against itself - a ~196M-cell
comparison - killed the Postgres backend process outright ("server closed
the connection unexpectedly"). ST_Simplify's tolerance alone is not a
sufficient guard: it has no vertex-count bound, and a noisy real GPS trace
does not simplify nearly as well as a smooth synthetic line does.
MAX_SIMPLIFIED_VERTICES enforces a hard cap, tried against a ladder of
increasing tolerances (SIMPLIFY_TOLERANCE_LADDER_M) until either geometry -
the activity, or a given candidate route - is safely under it. If nothing
on the ladder gets a geometry under the cap, that geometry is left out of
matching entirely (the whole activity, if it is the one too large; just
that one candidate, if it is a route) rather than ever letting
ST_Buffer/ST_Intersection run on something that size. Skipping is the
correct failure mode here: a missing match is an enrichment not delivered,
a dead database is an outage.

One correction survives from the original plan, and applies in two places
now rather than one: Web Mercator's scale is not uniform - it grows as
1/cos(latitude), so a "40m" buffer (or a "20m" simplify tolerance)
expressed directly in EPSG:3857 units is really only about 25m (or 12m)
on the ground at the UK's ~51.8N (1/cos(51.8 deg) =~ 1.62). Both
COVERAGE_BUFFER_M and every rung of SIMPLIFY_TOLERANCE_LADDER_M are
divided by cos(latitude) of the activity's own centroid before being
handed to ST_Buffer/ST_Simplify, so each one means what its name in true
ground metres claims. Missing this on the simplify tolerance specifically
would make every rung of the ladder ~1.6x weaker than it claims at UK
latitudes - simplifying less aggressively than intended, and needing more
rungs to bring a large trace under the cap than the constant's own name
would suggest. This only has to apply to the buffer/tolerance distances
themselves, not to the coverage ratio: numerator and denominator are both
lengths measured in the same projected geometry at the same rough
latitude, so the distortion cancels out of the ratio on its own - see
test_route_match.py's dedicated test, which fails if the correction is
dropped from the buffer.

Coverage is direction-agnostic by construction - a route ridden backwards
buffers and intersects exactly the same as one ridden forwards - so there
is no ST_Reverse arm here the way the Frechet plan needed one. That
whole bug family is retired rather than patched.
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

from app.db import session_factory
from app.models import Activity

logger = logging.getLogger(__name__)

# Both provisional - a round buffer comfortably wider than ordinary GPS/road
# noise, and a coverage bar comfortably below "matches everywhere except one
# real excursion" but above "vaguely in the same area". Neither is tuned
# against real ride/route geometry yet; staging verification (real GPS
# traces against their planned routes) is expected to move both.
COVERAGE_BUFFER_M = 40.0
MIN_COVERAGE = 0.80

# Part of the decision itself, and - deliberately - the prefilter too:
# bidirectional coverage alone would accept an out-and-back ride over a
# one-way route (or vice versa) at ~100% coverage both ways despite being
# roughly double the distance, so a distance ratio catches what coverage
# cannot.
#
# There was briefly a second, looser pair of constants here for the
# prefilter. That is what a candidate cap plus two bands buys you: routes
# outside this band can never win the final SELECT, so admitting them to
# the candidate list only spent the LIMIT below on rows destined to be
# discarded - and, with more than MAX_CANDIDATES of them, could crowd the
# true match out of the list entirely. One band, used in both places, means
# the two can never drift apart and a fetched candidate is always one that
# could actually win.
MATCH_DISTANCE_RATIO_MIN = 0.8
MATCH_DISTANCE_RATIO_MAX = 1.25

# Bounds the candidate list a pathological library (or a bbox that happens
# to sit over a dense cluster of saved routes) could otherwise force.
MAX_CANDIDATES = 25

# Tried in ascending order; the first tolerance that brings a geometry's
# simplified point count under MAX_SIMPLIFIED_VERTICES wins. Expected to
# succeed on the first rung for the overwhelming majority of rides - a
# typical GPX import is already well under 1000 points - so the later rungs
# exist for the noisy multi-thousand-point tail, not the common case.
SIMPLIFY_TOLERANCE_LADDER_M = [20.0, 40.0, 80.0, 160.0, 320.0, 640.0, 1280.0]

# ST_Buffer/ST_Intersection are cheap on a few hundred points; the crash
# this guards against (see module docstring) was reproduced at 14,000. 1000
# is comfortably under anything that measured as a problem and comfortably
# above what a real ride needs to keep its shape after simplification.
MAX_SIMPLIFIED_VERTICES = 1000

# Fixed-precision grid for the coverage overlay, in EPSG:3857 units (1mm).
#
# Not a tuning knob - a correctness guard. GEOS's default floating-point
# overlay returns LINESTRING EMPTY for ST_Intersection(ST_Buffer(line, tol),
# line) when the line is EXACTLY axis-aligned, even though ST_Within(line,
# buffer) is true for the same pair. Measured: a due-north line scores
# covered_len 0 of 1620.09; the same line at a bearing of 1 degree scores
# full coverage. It is magnitude-independent (LINESTRING(0 0, 0 900, 0 1620)
# does it too), so it is a degeneracy, not a precision limit.
#
# That would be an exotic edge case if the input were the rider's raw GPS,
# and it is not: ST_Simplify MANUFACTURES it. A realistic near-north road of
# ten points wobbling 1-3m - far inside GPS/OSM noise, far under the ladder's
# 20m first rung - collapses to an exact two-point line at identical
# projected X, and nudging an interior point by 1m re-collapses to the same
# two endpoints. Canal towpaths, disused railways and Roman roads are the
# real population, and each one would silently never match any ride of it.
#
# Passing a gridSize routes the overlay through OverlayNG's fixed-precision
# path, which is robust here. Measured across a bearing sweep it is identical
# to the default everywhere the default works, and correct at 0 and 90 where
# the default returns zero. 1mm is far below any distance this module can
# resolve - the buffer it feeds is 40m - so it cannot affect a real verdict.
_OVERLAY_GRID_SIZE_M = 0.001

# `act` narrows to the one activity being matched and its owning user, so
# every candidate route already belongs to that same user - there is no
# separate ownership filter to forget.
#
# `act_tol`/`act_simplified` pick the smallest ladder tolerance that brings
# the activity under the vertex cap (dividing by lat_cos, same correction as
# the buffer - see the module docstring); if none of them do, act_simplified
# has no rows at all, `scored` below (which CROSS JOINs it) is empty, and
# nothing downstream ever runs ST_Buffer or ST_Intersection on the
# oversized geometry - the whole activity is skipped rather than risking
# the query. `route_tol` does the same per candidate route, independently:
# a single oversized route drops only that candidate, not the whole match.
#
# `candidates` is the cheap, index-backed prefilter stage: `&&` uses the
# GiST index on each geom column, and the distance-ratio filter - the same
# band the final SELECT decides on, see MATCH_DISTANCE_RATIO_* - discards
# everything that could not win before anything geometric runs.
#
# The ORDER BY is load-bearing, not tidiness. `LIMIT` without it lets
# Postgres return any qualifying rows it likes, so for a rider with more
# than MAX_CANDIDATES overlapping routes the true match could be cut purely
# on physical row order - and a dropped candidate is indistinguishable from
# "nothing matched".
#
# It orders on centroid distance rather than on closeness of length, which
# was the first attempt and does not work: variants of the same route
# usually have near-identical distances, so every candidate ties at zero and
# the cap goes back to cutting arbitrarily. A route the rider actually rode
# has a centroid essentially on top of the ride's, so this puts the likeliest
# candidates first and the cap drops the least plausible rather than an
# arbitrary set. ST_Centroid is linear in vertex count and runs on at most
# MAX_CANDIDATES rows, long before the buffering that actually costs.
#
# `coverage` computes both directions - what fraction of the (simplified)
# route lies within a buffered corridor around the (simplified) ride, and
# the same with the roles swapped - dividing COVERAGE_BUFFER_M by
# `lat_cos` so the buffer is a genuine ground distance rather than a
# Mercator-inflated one (see module docstring). NULLIF guards a
# zero-length simplified line turning a division into an error rather than
# a merely-excluded NULL row.
#
# The final SELECT is the decision: both coverage fractions over
# MIN_COVERAGE, the route/activity distance ratio within the tighter
# MATCH_DISTANCE_RATIO_* band, ordered by confidence **descending** - the
# highest-covering candidate wins, not the lowest (a real risk to get
# backwards now that "distance" is no longer the score - the old
# ST_FrechetDistance query correctly sorted ASC on a distance; this one
# must sort DESC on a coverage fraction, and test_route_match.py has a
# dedicated two-candidate test that would only pass with the sort the
# right way round).
_MATCH_SQL = text("""
    WITH act AS (
        SELECT id, user_id, geom, distance_m,
               ST_Transform(geom, 3857) AS geom_m,
               cos(radians(ST_Y(ST_Centroid(geom)))) AS lat_cos
        FROM activities
        WHERE id = :activity_id
    ),
    act_tol AS (
        SELECT act.id,
               (
                   SELECT tol FROM unnest(CAST(:tolerances AS float8[])) AS tol
                   WHERE ST_NPoints(ST_Simplify(act.geom_m, tol / act.lat_cos)) <= :max_vertices
                   ORDER BY tol
                   LIMIT 1
               ) AS tol
        FROM act
    ),
    act_simplified AS (
        SELECT act.id, act.distance_m, act.lat_cos,
               ST_Simplify(act.geom_m, act_tol.tol / act.lat_cos) AS geom_s
        FROM act
        JOIN act_tol ON act_tol.id = act.id
        WHERE act_tol.tol IS NOT NULL
    ),
    candidates AS (
        SELECT r.id, r.distance_m, ST_Transform(r.geom, 3857) AS geom_m, act.lat_cos AS lat_cos
        FROM routes r, act
        WHERE r.user_id = act.user_id
          AND r.geom && act.geom
          AND r.distance_m BETWEEN
              act.distance_m * :match_ratio_min AND act.distance_m * :match_ratio_max
        ORDER BY ST_Distance(
                     ST_Centroid(r.geom)::geography, ST_Centroid(act.geom)::geography
                 ),
                 abs(r.distance_m - act.distance_m)
        LIMIT :max_candidates
    ),
    route_tol AS (
        SELECT c.id,
               (
                   SELECT tol FROM unnest(CAST(:tolerances AS float8[])) AS tol
                   WHERE ST_NPoints(ST_Simplify(c.geom_m, tol / c.lat_cos)) <= :max_vertices
                   ORDER BY tol
                   LIMIT 1
               ) AS tol
        FROM candidates c
    ),
    scored AS (
        SELECT c.id,
               c.distance_m AS route_distance_m,
               act_s.distance_m AS activity_distance_m,
               act_s.lat_cos AS lat_cos,
               act_s.geom_s AS act_geom_s,
               ST_Simplify(c.geom_m, rt.tol / c.lat_cos) AS route_geom_s
        FROM candidates c
        JOIN route_tol rt ON rt.id = c.id AND rt.tol IS NOT NULL
        CROSS JOIN act_simplified act_s
    ),
    coverage AS (
        SELECT id, route_distance_m, activity_distance_m,
               ST_Length(
                   ST_Intersection(
                       ST_Buffer(route_geom_s, :buffer_m / lat_cos),
                       act_geom_s,
                       :grid_size
                   )
               ) / NULLIF(ST_Length(act_geom_s), 0) AS ride_covered,
               ST_Length(
                   ST_Intersection(
                       ST_Buffer(act_geom_s, :buffer_m / lat_cos),
                       route_geom_s,
                       :grid_size
                   )
               ) / NULLIF(ST_Length(route_geom_s), 0) AS route_covered
        FROM scored
    )
    SELECT id, LEAST(ride_covered, route_covered) AS confidence
    FROM coverage
    WHERE ride_covered >= :min_coverage
      AND route_covered >= :min_coverage
      AND route_distance_m BETWEEN
          activity_distance_m * :match_ratio_min AND activity_distance_m * :match_ratio_max
    ORDER BY confidence DESC
    LIMIT 1
""")


async def _store_match(
    db: AsyncSession,
    activity_id: uuid.UUID,
    route_id: uuid.UUID | None,
    confidence: float | None,
) -> bool:
    """Write a match result, but only while the rider has not locked it.

    `match_locked = false` lives in the UPDATE's own WHERE clause rather
    than in a Python check, and that placement is the whole point. Reading
    the flag and then writing leaves a window, and the window here is wide:
    between the two sits the bidirectional-coverage query, which simplifies
    and buffers up to MAX_CANDIDATES routes. A rider picking a route by hand
    in that window used to lose it - and lose it invisibly, because
    SQLAlchemy's unit of work only emits the columns it saw change, so the
    manual write's `match_locked = true` survived while its `route_id` was
    overwritten. That leaves a row no code can tell from a genuine manual
    pick: locked against all future passes, pointing at the auto-matcher's
    guess. Silent, permanent, and the exact inversion of what the flag is
    for.

    Returns whether the row was actually written, so a caller cannot report
    a match it did not get to make.
    """
    result = cast(
        "CursorResult[Any]",
        await db.execute(
            update(Activity)
            .where(Activity.id == activity_id, Activity.match_locked.is_(False))
            .values(route_id=route_id, match_confidence=confidence)
        ),
    )
    await db.commit()
    return bool(result.rowcount)


async def match_activity_to_route(
    activity_id: uuid.UUID, *, clear_if_unmatched: bool = False
) -> uuid.UUID | None:
    """Find the best-matching route for one activity and persist the result.

    `clear_if_unmatched` says whether "no candidate qualified" should clear an
    existing link. It defaults to False, which is right for the passive pass
    that runs on import: a route the rider has not touched is still evidence,
    and a threshold that happens not to be beaten this time is not grounds to
    throw a match away. It must be True whenever the caller knows the evidence
    the link rested on is gone - the route was re-routed beneath it, or a rider
    explicitly asked for a rematch - because there the stale link is the
    harmful outcome, not the cautious one.

    A no-op - returns None without touching anything - when the activity
    does not exist, or when `match_locked` is true: that flag means a rider
    already made this decision by hand, and an auto-match run must never
    overwrite it.

    Only ever considers routes owned by the activity's own user (enforced
    in the SQL itself via `act.user_id`, not by a filter this function has
    to remember to apply).

    Returns the winning route id, or None if nothing scored a coverage over
    MIN_COVERAGE in both directions - in which case any existing link is
    left alone rather than cleared, since "no candidate beat the threshold
    this time" is not evidence the previous match was wrong. `match_confidence`
    is stored as a 0-1 score (the lower of the two coverage fractions), not
    a distance - higher is better.

    Never raises, and - the part that took four review rounds to get right -
    never disturbs the caller's session either. It opens its OWN session
    rather than borrowing one.

    That is a mechanism, not a patch, and it exists because three consecutive
    rounds found a new bug in the previous round's fix, all of them the same
    shape: this function has to write and commit, the caller is mid-request
    holding loaded ORM objects, and every way of failing inside a *shared*
    session damages those objects.

      - A failed statement aborts the whole Postgres transaction, so the
        caller's next query dies with "current transaction is aborted".
      - Catching it and calling `db.rollback()` clears the abort but expires
        every object in the session, so the caller's next attribute access
        becomes an implicit lazy load, which AsyncSession refuses outside a
        greenlet ("MissingGreenlet"). The ride is already durably committed
        by then, so a rider got a 500 for an import that genuinely worked.
      - A SAVEPOINT (`db.begin_nested()`) fixed the query path but not the
        COMMIT path, which cannot be inside a savepoint.
      - Rolling back and re-loading the activity to repopulate the caller's
        identity-mapped instance fixed that - unless the re-load ITSELF
        failed, which is exactly what a dropped connection does to the very
        next statement after a failed commit. Then the caller was left
        holding an expired object with no working reload: MissingGreenlet
        again, one branch deeper.

    Each fix was a case; the family was the borrowing. Every caller already
    commits its own work BEFORE calling this (import_activity, the archive
    worker, the rematch endpoint, and _rematch_linked_activities all do), so
    there was never a transaction here worth sharing - only one worth not
    breaking. With a private session the caller's objects are untouchable by
    construction: a failure of any kind in here rolls back a session nobody
    else can see, and the caller's request carries on with the enrichment
    simply not applied. That is the stated policy - the ride is the valuable
    thing, its route link is an enrichment.

    The cost of the mechanism, and the one thing callers must know: the
    caller's own `activity` object does NOT see the write, because it was
    made on another connection. A caller that goes on to render the new link
    has to re-read it (`db.refresh`), which import_activity does.
    """
    route_id: uuid.UUID | None = None
    try:
        # Module-scope `session_factory` on purpose: conftest monkeypatches
        # this attribute to point at the per-test throwaway database, the same
        # convention services/wahoo_queue.py and services/activity_import.py
        # already use for their own out-of-request sessions.
        async with session_factory() as db:
            activity = await db.get(Activity, activity_id)
            if activity is None or activity.match_locked:
                return None

            row = (
                await db.execute(
                    _MATCH_SQL,
                    {
                        "activity_id": activity_id,
                        "match_ratio_min": MATCH_DISTANCE_RATIO_MIN,
                        "match_ratio_max": MATCH_DISTANCE_RATIO_MAX,
                        "max_candidates": MAX_CANDIDATES,
                        "min_coverage": MIN_COVERAGE,
                        "buffer_m": COVERAGE_BUFFER_M,
                        "tolerances": SIMPLIFY_TOLERANCE_LADDER_M,
                        "max_vertices": MAX_SIMPLIFIED_VERTICES,
                        "grid_size": _OVERLAY_GRID_SIZE_M,
                    },
                )
            ).first()
            if row is None or row.confidence is None:
                # See clear_if_unmatched in the docstring: passively this
                # means "leave the existing link alone"; when the caller
                # knows the evidence is gone it means the opposite, because
                # a stale link is read as a real match by planned-vs-actual
                # and silently feeds a nonsensical implied speed into
                # ride-time calibration.
                if clear_if_unmatched and activity.route_id is not None:
                    await _store_match(db, activity_id, None, None)
                return None

            if not await _store_match(db, activity_id, row.id, float(row.confidence)):
                # A rider locked this activity while the coverage query was
                # running. Their pick stands and we did not write, so there is
                # no match of ours to report.
                return None
            route_id = row.id
    except Exception:  # noqa: BLE001 - see the "Never raises" docstring note above
        logger.warning("route match failed for activity %s", activity_id, exc_info=True)
        # No rollback needed and none possible to get wrong: leaving the
        # `async with` closes this private session, which rolls back anything
        # uncommitted. There is no other session to damage.
        return None

    return route_id
