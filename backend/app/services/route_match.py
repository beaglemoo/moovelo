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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
        ORDER BY ST_Distance(ST_Centroid(r.geom), ST_Centroid(act.geom)),
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
                       ST_Buffer(route_geom_s, :buffer_m / lat_cos), act_geom_s
                   )
               ) / NULLIF(ST_Length(act_geom_s), 0) AS ride_covered,
               ST_Length(
                   ST_Intersection(
                       ST_Buffer(act_geom_s, :buffer_m / lat_cos), route_geom_s
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


async def match_activity_to_route(
    db: AsyncSession, activity_id: uuid.UUID, *, clear_if_unmatched: bool = False
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

    Never raises. Every caller's contract is "a matching failure must never
    fail the import", and that guarantee has to live here rather than at
    each call site - and it took two attempts to actually deliver it,
    reproduced directly against real Postgres both times:

    A failed statement leaves the whole session's Postgres transaction
    *aborted*, so catching the exception at the call site and carrying on
    is not enough by itself - the very next query on the same session
    (typically the caller re-reading the activity for its response) fails
    too, with "current transaction is aborted, commands ignored until end
    of transaction block", turning a missing enrichment into a 500 for the
    whole request.

    The first fix - catch here and call `db.rollback()` - clears the abort,
    but a plain rollback also *expires every object already loaded in the
    session*, matching real transactional semantics (their state may no
    longer be valid). The caller's own `activity` object - fetched before
    this function was ever called - is one of them, so its very next
    attribute access anywhere in the caller triggers an implicit lazy
    reload, and `AsyncSession` refuses to do that reload outside an
    explicit `await` ("MissingGreenlet: ... Was IO attempted in an
    unexpected place?"). Swapping the plain rollback for a `SAVEPOINT`
    (`db.begin_nested()`) is what actually fixes both problems at once: a
    failure inside it only undoes work done inside it, so the caller's
    already-loaded objects are never touched. This is the same mechanism
    Sprint 1's C8/G3 fix used for the identical shape of bug (see
    CLAUDE.md) - a plain rollback trashing a caller's transaction is not a
    new failure mode in this codebase, it is a recurring one.
    """
    route_id: uuid.UUID | None = None
    # One exit that commits, rather than a commit reachable from only one of
    # the branches that write. The clear_if_unmatched path used to `return`
    # from inside the SAVEPOINT below, which skipped the commit at the end
    # entirely: the clear was visible to the calling session and then rolled
    # back at close, so an explicit rematch appeared to work and changed
    # nothing. Releasing a SAVEPOINT only merges into the still-open outer
    # transaction; durability is this function's own commit.
    changed = False
    try:
        async with db.begin_nested():
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
                    activity.route_id = None
                    activity.match_confidence = None
                    changed = True
            else:
                route_id = row.id
                activity.route_id = route_id
                activity.match_confidence = float(row.confidence)
                changed = True
    except Exception:  # noqa: BLE001 - see the "Never raises" docstring note above
        logger.warning("route match failed for activity %s", activity_id, exc_info=True)
        # Deliberately no rollback here: exiting the SAVEPOINT has already
        # unwound the failed statement, and a plain rollback would expire every
        # object the caller had loaded - which is the bug the SAVEPOINT was
        # introduced to avoid in the first place, documented above.
        return None

    # Its own try, because a failed COMMIT is a different failure from a failed
    # query and needs the opposite handling. Nothing flushes until this runs,
    # so it is the statement most exposed to a real operational fault - a
    # dropped connection, a deadlock, a statement timeout - and it briefly sat
    # outside the block above, which made "never raises" false for exactly the
    # call most likely to fail. _rematch_linked_activities iterates this with
    # no try/except of its own, from a PATCH whose route save has already
    # committed, so an escape there 500s a request that actually succeeded and
    # silently abandons every remaining ride's re-derive.
    #
    # Guarded on `changed` so a no-op match does not commit a caller's
    # unrelated pending work as a side effect of asking a question.
    if changed:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001 - the contract above
            logger.warning("route match commit failed for activity %s", activity_id, exc_info=True)
            # Here a rollback IS required: a failed commit leaves the
            # transaction unusable, so the caller cannot continue without one,
            # and there is no half-applied state worth preserving. Guarded
            # because rollback can itself raise on a dead connection.
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.warning("rollback after a failed match commit also failed", exc_info=True)
            return None

    return route_id
