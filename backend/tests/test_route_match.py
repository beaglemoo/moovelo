"""services/route_match.py: matching a recorded ride to the saved route it
followed, plus the API surface around it (PUT .../route, POST .../rematch).

Routes and activities are seeded directly with WKT/EWKT geometry, the same
technique test_coverage.py uses for CycleWay/CycleWayMember - both are
tables the app writes to itself, but building the exact shapes this
module's coverage maths needs is easier as straight lines than as a real
Valhalla response.

The algorithm here is bidirectional coverage, not ST_FrechetDistance -
Frechet was the original plan, and it was dropped after being measured
against real dev Postgres and found to reject ordinary rides with ordinary
outliers (a single ~300m detour) and, unsimplified, to be able to kill the
Postgres backend process outright on a real multi-thousand-point trace.
Both findings have a dedicated test below: test_a_single_detour_does_not_
break_the_match and test_a_huge_trace_is_simplified_rather_than_crashing_
postgres. test_a_large_relative_detour_on_a_short_route_does_not_match
documents a boundary of the coverage approach found while testing it: a
fixed-size detour's tolerance is proportional to the route's own length,
not absolute.

test_a_match_query_failure_does_not_break_the_import_response guards a
third, unrelated finding: a failed match query leaves the session's
transaction in a state a plain try/except at the call site cannot safely
carry on from - see match_activity_to_route's own docstring for both fixes
it took to actually deliver "a matching failure must never fail the
import".
"""

import inspect
import math
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.route_match as match_route_module
import app.services.route_match as route_match
from app.models import Activity, Route, User
from app.schemas import RouteResponse
from app.services.geo import Point, destination_point
from app.services.route_match import (
    COVERAGE_BUFFER_M,
    MAX_CANDIDATES,
    MIN_COVERAGE,
    match_activity_to_route,
)
from tests.conftest import register
from tests.test_activities_api import GPX_RIDE
from tests.test_auth_routes import WAYPOINTS

# Same Chilterns point test_coverage.py uses, deliberately: it sits at
# ~51.8N, close to the UK's centre of population, which is exactly the
# latitude the Mercator-correction test below needs to be meaningful - the
# distortion this module corrects for is latitude-dependent and negligible
# near the equator.
LAT, LON = 51.8000, -0.6500

RUN_LENGTH_M = 2000.0
# Off true north (bearing 0) deliberately. A perfectly due-north line has
# zero width in longitude, so shifting a copy of it east by any amount at
# all - even 1m - produces a bounding box that never touches the
# original's: the cheap `&&` prefilter would reject it before the coverage
# maths this module exists to test ever ran. Tilting the run gives it a
# real bounding-box width (RUN_LENGTH_M * sin(8 deg) =~ 280m here) while a
# uniform east shift of the start point keeps both lines parallel.
RUN_BEARING_DEG = 8.0
RUN_WIDTH_M = RUN_LENGTH_M * 0.1392  # sin(8 deg), for tests picking a safe offset


def _line_wkt(points: list[Point]) -> str:
    coords = ", ".join(f"{lon} {lat}" for lat, lon in points)
    return f"SRID=4326;LINESTRING({coords})"


def _route(user_id: uuid.UUID, points: list[Point], distance_m: float) -> Route:
    return Route(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Test route",
        preset="gravel",
        source="planned",
        waypoints=[],
        legs=[],
        elevation=[],
        distance_m=distance_m,
        duration_s=distance_m / 5.0,
        ascent_m=0.0,
        descent_m=0.0,
        geom=_line_wkt(points),
    )


def _activity(
    user_id: uuid.UUID,
    points: list[Point],
    distance_m: float,
    match_locked: bool = False,
) -> Activity:
    return Activity(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Test ride",
        distance_m=distance_m,
        ascent_m=0.0,
        descent_m=0.0,
        elevation=[],
        geom=_line_wkt(points),
        match_locked=match_locked,
    )


async def _user_id(db: AsyncSession, email: str) -> uuid.UUID:
    return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


def _north_leg(offset_east_m: float = 0.0) -> list[Point]:
    """A straight RUN_LENGTH_M line on RUN_BEARING_DEG from (LAT, LON),
    optionally shifted `offset_east_m` due east first - the shape every test
    below starts from, so the offset is the only thing that varies."""
    start = destination_point((LAT, LON), 90.0, offset_east_m) if offset_east_m else (LAT, LON)
    end = destination_point(start, RUN_BEARING_DEG, RUN_LENGTH_M)
    return [start, end]


# --- match_activity_to_route -----------------------------------------------


async def test_a_ride_following_a_route_matches(client: AsyncClient, db: AsyncSession) -> None:
    """A ride that closely tracks a saved route matches it. 25m off the
    line - a plausible real-world GPS/road-position deviation, comfortably
    under COVERAGE_BUFFER_M - means the whole ride sits inside the buffered
    corridor both ways, so the confidence should read close to a clean 1.0,
    not merely "high"."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=25.0), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == route.id
    await db.refresh(activity)
    assert activity.route_id == route.id
    assert activity.match_confidence is not None
    assert activity.match_confidence == pytest.approx(1.0, rel=0.03)


async def test_a_ride_on_a_different_road_does_not_match(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Comfortably over COVERAGE_BUFFER_M is not "the same road with GPS
    noise" - it must not match. The offset is kept under RUN_WIDTH_M so this
    still passes the cheap bbox/distance-ratio prefilter (same length,
    overlapping bounding boxes) and is rejected by the coverage confirmation
    itself, not merely filtered out earlier for an unrelated reason."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    offset_m = RUN_WIDTH_M * 0.7
    assert offset_m > COVERAGE_BUFFER_M, "fixture must actually exceed the buffer"

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=offset_m), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched is None
    await db.refresh(activity)
    assert activity.route_id is None
    assert activity.match_confidence is None


async def test_a_route_ridden_in_reverse_still_matches(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Coverage is direction-agnostic by construction - buffering and
    intersecting two lines does not care which end either one starts from -
    so a route stored in the opposite point order (planned one way, ridden
    the other) needs no special handling the way ST_FrechetDistance's
    start-to-start comparison would have. This documents that it just
    works, rather than testing an arm that no longer exists."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    forward = _north_leg()
    route = _route(user_id, list(reversed(forward)), RUN_LENGTH_M)
    activity = _activity(user_id, forward, RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == route.id
    await db.refresh(activity)
    assert activity.match_confidence is not None
    assert activity.match_confidence == pytest.approx(1.0, rel=0.03)


async def test_mercator_distortion_is_corrected_to_true_ground_metres(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The test the module docstring promises: a 32m true offset at this UK
    latitude must still fall inside a 40m corridor once COVERAGE_BUFFER_M is
    correctly converted to true ground metres. Uncorrected, the buffer would
    only reach cos(51.8 deg) * 40 =~ 24.7m of true ground distance - less
    than the 32m offset - and this fixture would not match at all: the
    required EPSG:3857 reach for a 32m true offset is 32 / cos(51.8 deg)
    =~ 51.8 map units, comfortably more than an uncorrected 40, and
    comfortably less than the corrected 40 / cos(51.8 deg) =~ 64.7."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    true_offset_m = 32.0
    assert true_offset_m > COVERAGE_BUFFER_M * 0.6184, (
        "fixture must exceed what an uncorrected buffer could reach at this latitude"
    )

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=true_offset_m), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == route.id, (
        "with the cos(latitude) correction removed, a 40m buffer only reaches "
        "~24.7m of true ground distance at this latitude - under the 32m "
        "offset - and this fixture would not match at all"
    )


async def test_a_single_detour_does_not_break_the_match(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The finding that changed the algorithm: a ride that follows its route
    exactly except for one ~300m out-and-back detour (a shop stop, a wrong
    turn) measured a 300.6m ST_FrechetDistance against it in real testing -
    over any sane threshold - despite the overwhelming majority of the ride
    genuinely being on the route. A maximum-based metric lets a single
    outlier veto the whole match; bidirectional coverage does not, because
    the detour only drags the ratio down rather than setting the score on
    its own. This fixture would fail to match under a Frechet-threshold
    implementation and must pass under coverage."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    run_length_m = 4000.0
    detour_m = 300.0
    start = (LAT, LON)
    at_45pct = destination_point(start, RUN_BEARING_DEG, run_length_m * 0.45)
    detour_peak = destination_point(at_45pct, RUN_BEARING_DEG + 90.0, detour_m)
    end = destination_point(start, RUN_BEARING_DEG, run_length_m)

    route = _route(user_id, [start, end], run_length_m)
    # start -> 45% -> 300m detour out -> back to 45% -> end. Total length is
    # the route's 4000m plus the 600m round trip of the detour.
    activity = _activity(
        user_id, [start, at_45pct, detour_peak, at_45pct, end], run_length_m + 2 * detour_m
    )
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == route.id
    await db.refresh(activity)
    assert activity.match_confidence is not None
    assert activity.match_confidence >= MIN_COVERAGE


async def test_a_large_relative_detour_on_a_short_route_does_not_match(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A documented boundary, not a bug: bidirectional coverage's tolerance
    to a fixed-size detour is proportional to how large the detour is
    *relative to the route*, not absolute. The same 300m excursion that
    comfortably passes on the 4000m route above (7.5% of its length) does
    not clear MIN_COVERAGE on a 2400m route (12.5% of its length) - measured
    directly against real Postgres at ride_covered=0.668, route_covered=0.723
    (both under the 0.80 bar) for this exact fixture: a single mid-ride
    waypoint pushed 300m sideways rather than a there-and-back spike, on a
    6-leg/2400m route.

    This is deliberately NOT "fixed" by widening COVERAGE_BUFFER_M to
    whatever this fixture needs (~150m, measured separately) - that would
    directly undermine the buffer's other job, keeping it "tight enough to
    separate parallel roads". A 40m corridor that also had to swallow a
    150m excursion could no longer tell a rider on the road next to their
    planned route from a rider on the route itself. The fixture the
    algorithm change was justified against (test_a_single_detour_does_not_
    break_the_match above) represents a detour as a small fraction of a
    real-length ride, matching the "3 points out of 100" scale of the
    finding that motivated the change; this one is a stress case at the
    opposite end, kept here so the boundary is measured and pinned rather
    than merely asserted.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    leg_m = 400.0
    points = [(LAT, LON)]
    for _ in range(6):
        points.append(destination_point(points[-1], RUN_BEARING_DEG, leg_m))
    total = leg_m * 6
    detoured = list(points)
    mid = len(detoured) // 2
    detoured[mid] = destination_point(detoured[mid], 90.0, 300.0)

    route = _route(user_id, points, total)
    activity = _activity(user_id, detoured, total + 600.0)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched is None
    await db.refresh(activity)
    assert activity.route_id is None
    assert activity.match_confidence is None


async def test_a_match_query_failure_does_not_break_the_import_response(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduced against real Postgres twice, in two different ways, before
    this passed: catching the exception at the call site alone was not
    enough (a failed statement leaves the session's transaction *aborted*,
    so the response's own re-read of the activity failed too, 500ing the
    request), and neither was a plain `db.rollback()` inside
    match_activity_to_route (it expires every already-loaded object in the
    session, including the caller's own `activity` reference, so the very
    next attribute access on it tried an implicit lazy reload outside an
    async context and raised `MissingGreenlet`). Only a SAVEPOINT
    (`db.begin_nested()`) actually delivers "a matching failure must never
    fail the import" - see match_activity_to_route's own docstring."""
    monkeypatch.setattr(route_match, "_MATCH_SQL", text("SELECT 1/0"))
    await register(client)

    response = await client.post(
        "/api/activities/import",
        files={"file": ("ride.gpx", GPX_RIDE, "application/gpx+xml")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["route_id"] is None

    # The session must still be usable afterward - proves the transaction
    # was never left aborted and no already-loaded object was left stranded.
    listed = await client.get("/api/activities")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def _tilted_points() -> list[Point]:
    """The RUN_BEARING_DEG leg as three points, for building a GPX body.

    Not due north, and the reason is load-bearing rather than cosmetic: a
    perfectly north-south line has zero width in longitude, and a route
    seeded on exactly the ride's own due-north points does NOT match - the
    coverage maths degenerates and returns no candidate. Measured through
    the real import endpoint while writing the test below, which is why this
    fixture exists instead of reusing GPX_RIDE.
    """
    mid = destination_point((LAT, LON), RUN_BEARING_DEG, RUN_LENGTH_M / 2)
    end = destination_point((LAT, LON), RUN_BEARING_DEG, RUN_LENGTH_M)
    return [(LAT, LON), mid, end]


def _gpx_of(points: list[Point]) -> bytes:
    trkpts = "".join(
        f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>100.0</ele>'
        f"<time>2026-05-03T08:0{i}:00Z</time></trkpt>"
        for i, (lat, lon) in enumerate(points)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>Tilted</name><trkseg>{trkpts}</trkseg></trk></gpx>"
    ).encode()


async def test_a_match_commit_failure_does_not_break_the_import_response(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit path, which took three further rounds to get right.

    Failing the match's COMMIT is a different fault from failing its query
    and it broke the import three separate times, each fix uncovering the
    next case one branch deeper: a bare commit outside the guard; then a
    guarded commit whose `rollback()` expired the caller's `activity` and
    turned `_detail`'s next attribute read into a `MissingGreenlet`; then a
    rollback-plus-reload to repopulate that object, which still stranded the
    caller whenever the RELOAD itself failed - the ordinary behaviour of a
    dropped connection, since it is the very next statement after the commit
    that dropped it.

    So this simulates the double fault: the match's commit fails, AND any
    recovery read it attempts afterwards fails too. The fix is not another
    branch but a mechanism - match_activity_to_route owns a private session,
    so no failure inside it can reach the caller's objects at all.

    Deliberately driven through the real endpoint rather than by calling the
    service, so it pins the observable contract (the rider gets their ride)
    rather than an implementation shape, and stays a valid reproduction
    across the mechanism change.

    Failures are selected by CALL SITE, not by a global call counter: the
    WayMatchQueue worker commits concurrently on its own session, so a
    counter races it and misses.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    # A route the imported ride genuinely matches. Without one the match
    # finds no candidate, writes nothing and never reaches its commit at
    # all - the first version of this test omitted it and passed against
    # the very code it was meant to catch.
    db.add(_route(user_id, _tilted_points(), RUN_LENGTH_M))
    await db.commit()

    real_commit = AsyncSession.commit
    real_get = AsyncSession.get
    commit_failures = 0
    gets_from_match = 0

    def _called_by_match() -> bool:
        # Two frames up, not one: this helper is called BY failing_commit /
        # failing_get, so f_back is the patch and f_back.f_back is the code
        # that called the patched method.
        #
        # Keyed on the FILE, not on match_activity_to_route by name. It was
        # keyed on the name, and then the TOCTOU fix moved the commit into
        # _store_match in the same module - at which point the injection
        # stopped firing and this test would have gone silently vacuous. The
        # commit_failures assertion below is what caught that; the predicate
        # is now written so an ordinary refactor within the module cannot
        # disarm it again.
        outer = frame.f_back if (frame := inspect.currentframe()) else None
        caller = outer.f_back if outer else None
        return caller is not None and caller.f_code.co_filename.endswith("route_match.py")

    async def failing_commit(self: AsyncSession, *args: object, **kwargs: object) -> None:
        nonlocal commit_failures
        if _called_by_match():
            commit_failures += 1
            raise RuntimeError("simulated dropped connection on the match commit")
        await real_commit(self)

    async def failing_get(self: AsyncSession, *args: object, **kwargs: object) -> object:
        nonlocal gets_from_match
        if _called_by_match():
            gets_from_match += 1
            # The first get is the match's own initial load, which succeeds;
            # any later one is a recovery attempt, and the connection that
            # just killed the commit kills that too.
            if gets_from_match > 1:
                raise RuntimeError("simulated dead connection on the recovery reload")
        return await real_get(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    monkeypatch.setattr(AsyncSession, "get", failing_get)

    response = await client.post(
        "/api/activities/import",
        files={"file": ("ride.gpx", _gpx_of(_tilted_points()), "application/gpx+xml")},
    )

    # Pinned explicitly: if the injection ever stops firing - a rename, a
    # refactor that moves the commit to a helper - the assertions below would
    # all still pass while proving nothing at all.
    assert commit_failures == 1, "the match's commit was never reached; test proves nothing"

    # The ride was durably committed before the match ran, so anything other
    # than a 201 hands the rider an error for an import that actually worked.
    assert response.status_code == 201, response.text
    assert response.json()["route_id"] is None

    listed = await client.get("/api/activities")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


async def test_the_best_covering_candidate_wins(client: AsyncClient, db: AsyncSession) -> None:
    """Two candidates that both clear MIN_COVERAGE, with a large gap between
    their scores - a perfect match (confidence ~1.0) and a route covering
    only the first 85% of the ride (confidence ~0.87, still passing). The
    higher-covering one must win. This is exactly the kind of bug an
    ORDER BY ... ASC left over from a distance-based metric would produce -
    the old Frechet query correctly sorted ascending on a distance; this one
    has to sort descending on a coverage fraction, and a leftover ASC would
    make this test pick the worse candidate."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    ride_points = _north_leg()
    partial_end = destination_point((LAT, LON), RUN_BEARING_DEG, RUN_LENGTH_M * 0.85)

    perfect = _route(user_id, ride_points, RUN_LENGTH_M)
    partial = _route(user_id, [(LAT, LON), partial_end], RUN_LENGTH_M * 0.85)
    activity = _activity(user_id, ride_points, RUN_LENGTH_M)
    db.add_all([perfect, partial, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == perfect.id
    await db.refresh(activity)
    assert activity.match_confidence is not None
    assert activity.match_confidence > 0.97


async def test_match_locked_blocks_auto_match(client: AsyncClient, db: AsyncSession) -> None:
    """A rider's own decision - even "no route" - must never be silently
    overwritten by a later auto-match run."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M, match_locked=True)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched is None
    await db.refresh(activity)
    assert activity.route_id is None
    assert activity.match_confidence is None


async def test_deleting_a_route_leaves_the_ride_intact_with_a_null_link(
    client: AsyncClient, db: AsyncSession
) -> None:
    """ON DELETE SET NULL, not CASCADE: the ride happened regardless of
    whether the route it was matched to still exists."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    activity_id = activity.id
    assert await match_activity_to_route(activity_id) == route.id

    await db.execute(delete(Route).where(Route.id == route.id))
    await db.commit()

    # A fresh column read, not a re-read of the same ORM object, so a stale
    # identity-mapped instance cannot hide a FK that never actually cleared.
    route_id_after = await db.scalar(select(Activity.route_id).where(Activity.id == activity_id))
    assert route_id_after is None

    # ON DELETE SET NULL is pure DDL - it nulls route_id and touches nothing
    # else - so without the migration's trigger the score describing the
    # now-deleted route survives it. A confidence for a route that does not
    # exist is a lie the UI would eventually render.
    confidence_after = await db.scalar(
        select(Activity.match_confidence).where(Activity.id == activity_id)
    )
    assert confidence_after is None


async def test_a_hand_cleared_link_keeps_its_lock_when_confidence_is_cleared(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The trigger must clear match_confidence without touching match_locked.

    Clearing a link by hand sets `route_id = NULL` and `match_locked = True`
    together - that pairing is how a rider says "there is no route for this
    ride, stop guessing". A trigger that also reset the flag would silently
    undo the decision and let the next auto-match re-link the ride.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    # Held as a plain value: the expire_all() below would otherwise make
    # reading activity.id an implicit lazy load outside async context.
    activity_id = activity.id
    assert await match_activity_to_route(activity_id) == route.id

    cleared = await client.put(f"/api/activities/{activity_id}/route", json={"route_id": None})
    assert cleared.status_code == 200, cleared.text

    row = (
        await db.execute(
            select(Activity.route_id, Activity.match_confidence, Activity.match_locked).where(
                Activity.id == activity_id
            )
        )
    ).one()
    assert row.route_id is None
    assert row.match_confidence is None
    assert row.match_locked is True

    # And the lock genuinely still holds the auto-matcher off. The PUT above
    # went through the API's own session, so this one's identity map still
    # holds the pre-clear Activity - expire it, or the matcher reads a stale
    # match_locked and the assertion passes or fails for the wrong reason.
    db.expire_all()
    assert await match_activity_to_route(activity_id) is None


async def test_the_true_route_wins_even_when_many_candidates_qualify(
    client: AsyncClient, db: AsyncSession
) -> None:
    """`LIMIT` without `ORDER BY` lets Postgres return any qualifying rows.

    With more overlapping same-length routes than MAX_CANDIDATES, the true
    match could be cut purely on physical row order - and a dropped
    candidate is indistinguishable from "nothing matched". The decoys are
    inserted FIRST here, which is the ordering that reproduced the failure.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    # Comfortably more than MAX_CANDIDATES, all overlapping the ride's bbox
    # (offsets stay under RUN_WIDTH_M) and all inside the distance band, so
    # every one is a genuine candidate that must be fetched and scored. They
    # sit well beyond COVERAGE_BUFFER_M, though, so none of them can actually
    # win - which is the point: only the true route should come back, and it
    # cannot if the cap dropped it before scoring.
    #
    # Each decoy's distance_m is nudged slightly further from the activity's
    # own than the true route's exact match - not tied with it - so the
    # ORDER BY genuinely discriminates rather than relying on how Postgres
    # happens to break a tie. distance_m is independent of the offset
    # geometry above; it is what the query actually sorts candidates by.
    decoys = [
        _route(user_id, _north_leg(offset_east_m=100.0 + i * 4.0), RUN_LENGTH_M + 50.0 + i * 2.0)
        for i in range(MAX_CANDIDATES + 10)
    ]
    true_route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([*decoys, true_route, activity])
    await db.commit()

    assert await match_activity_to_route(activity.id) == true_route.id


async def test_a_huge_trace_is_simplified_rather_than_crashing_postgres(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The severe finding: an unsimplified ~14,000-point trace - an ordinary
    four-hour ride recorded at 1Hz - run through ST_FrechetDistance against
    itself killed the Postgres backend process outright in real testing
    ("server closed the connection unexpectedly"). MAX_SIMPLIFIED_VERTICES
    plus the tolerance ladder must keep every geometry this matcher touches
    under that cap before ST_Buffer/ST_Intersection ever run on it. The
    assertion that matters is that this completes and the database
    connection is still alive afterward - a match is a bonus, not the
    point."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    # A noisy-but-essentially-straight 14,000-point line, generated in
    # Postgres itself (the same technique used to find the crash): small
    # zigzag (a few metres either side) so it is representative of a real
    # recording, monotonically progressing so it stays a simple line.
    huge_wkt = await db.scalar(
        text("""
            SELECT ST_AsText(ST_MakeLine(ARRAY(
                SELECT ST_MakePoint(:lon + (n % 9 - 4) * 0.00002, :lat + n * 0.00003)
                FROM generate_series(1, :n) AS n
            )))
        """),
        {"lon": LON, "lat": LAT, "n": 14_000},
    )
    assert huge_wkt is not None
    huge_geom = f"SRID=4326;{huge_wkt}"

    route = _route(user_id, [], 40_000.0)
    route.geom = huge_geom
    activity = _activity(user_id, [], 40_000.0)
    activity.geom = huge_geom
    db.add_all([route, activity])
    await db.commit()
    activity_id = activity.id

    # Must not raise, and must not take the connection down with it.
    result = await match_activity_to_route(activity_id)

    assert result == route.id
    # A fresh query on the same session - proves the backend process
    # survived, not merely that this one call happened to return.
    assert await db.scalar(select(Activity.id).where(Activity.id == activity_id)) == activity_id


# --- cross-user isolation ---------------------------------------------------


async def test_auto_match_never_crosses_users(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """User B's ride must never match against user A's route, even when it
    physically overlaps it - the candidate query scopes to the activity's
    own user_id, not a filter bolted on afterwards."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    one_id = await _user_id(db, "one@example.com")
    await register(client, "two@example.com")
    two_id = await _user_id(db, "two@example.com")

    route = _route(one_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(two_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched is None
    await db.refresh(activity)
    assert activity.route_id is None


async def test_cannot_link_own_activity_to_another_users_route(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    one_id = await _user_id(db, "one@example.com")
    route = _route(one_id, _north_leg(), RUN_LENGTH_M)
    db.add(route)
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    two_id = await _user_id(db, "two@example.com")
    activity = _activity(two_id, _north_leg(), RUN_LENGTH_M)
    db.add(activity)
    await db.commit()

    response = await client.put(
        f"/api/activities/{activity.id}/route", json={"route_id": str(route.id)}
    )

    assert response.status_code == 404
    stored = await db.scalar(select(Activity.route_id).where(Activity.id == activity.id))
    assert stored is None


# --- API: PUT /route, POST /rematch ----------------------------------------


async def test_set_and_clear_route_locks_out_auto_match(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=1000.0), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    response = await client.put(
        f"/api/activities/{activity.id}/route", json={"route_id": str(route.id)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route_id"] == str(route.id)
    assert body["route_name"] == route.name
    assert body["match_locked"] is True
    assert body["match_confidence"] is None

    # A rematch must not override a locked, manually-set link even though
    # this pair is 1000m apart and would never have auto-matched.
    rematch = await client.post(f"/api/activities/{activity.id}/rematch")
    assert rematch.status_code == 200, rematch.text
    assert rematch.json()["route_id"] == str(route.id)

    clear = await client.put(f"/api/activities/{activity.id}/route", json={"route_id": None})
    assert clear.status_code == 200, clear.text
    assert clear.json()["route_id"] is None
    assert clear.json()["match_locked"] is True


async def test_linking_an_unknown_route_is_a_404(client: AsyncClient) -> None:
    await register(client)
    fake_activity = uuid.uuid4()

    response = await client.put(
        f"/api/activities/{fake_activity}/route",
        json={"route_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404


async def test_rematch_finds_a_route_for_an_unmatched_ride(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=10.0), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    response = await client.post(f"/api/activities/{activity.id}/rematch")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route_id"] == str(route.id)
    assert body["match_locked"] is False
    assert body["match_confidence"] is not None
    assert 0.0 < body["match_confidence"] <= 1.0


async def test_list_activities_carries_the_matched_route_name(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    assert await match_activity_to_route(activity.id) == route.id

    response = await client.get("/api/activities")

    assert response.status_code == 200
    row = next(item for item in response.json() if item["id"] == str(activity.id))
    assert row["route_id"] == str(route.id)
    assert row["route_name"] == route.name


async def test_a_cross_linked_row_never_leaks_the_other_users_route_name(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """The list and detail reads filter the route's owner as well as the ride's.

    Nothing in the app can produce this row: PUT .../route checks both sides
    (the test above pins that) and auto-matching only ever considers the
    rider's own routes. The foreign key permits it though, so it is one bad
    query or one future endpoint away - and these are read paths, which do
    not get to depend on every present and future writer staying correct.
    GET /api/routes/{id}/activities and services/ride_calibration.py already
    filter both tables for this reason; these two were the pair that did not,
    and without the filter a stranger's private route name is handed to
    whoever owns the ride.

    Seeded through the session because the API cannot create it.
    """
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "stranger@example.com")
    stranger_id = await _user_id(db, "stranger@example.com")
    secret = _route(stranger_id, _north_leg(), RUN_LENGTH_M)
    secret.name = "SECRET-STRANGER-ROUTE"
    db.add(secret)
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    activity = _activity(owner_id, _north_leg(), RUN_LENGTH_M)
    activity.route_id = secret.id
    db.add(activity)
    await db.commit()

    listed = await client.get("/api/activities")
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["id"] == str(activity.id))
    assert row["route_name"] != "SECRET-STRANGER-ROUTE"
    assert row["route_name"] is None

    detail = await client.get(f"/api/activities/{activity.id}")
    assert detail.status_code == 200
    assert detail.json()["route_name"] != "SECRET-STRANGER-ROUTE"
    assert detail.json()["route_name"] is None

    # The name was filtered and the raw UUID was not, so both reads handed
    # back a stranger's route id beside a correctly-nulled name. route_id is
    # now derived from the same owner-filtered fact as the name, so the two
    # cannot disagree; match_confidence goes with them, since a confidence
    # for a route the caller may not see describes nothing they can act on.
    assert row["route_id"] is None
    assert row["match_confidence"] is None
    assert detail.json()["route_id"] is None
    assert detail.json()["match_confidence"] is None


async def test_re_routing_a_saved_route_clears_the_matches_it_invalidated(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A saved route is re-routed in place, so its matches go stale silently.

    Nothing else re-derives them: the import-time pass only runs when a ride
    arrives, and its "no candidate qualified means leave the link alone"
    policy is exactly wrong once the geometry the match was made against no
    longer exists. Left stale, planned-vs-actual shows the old confidence
    beside a predicted time computed from the new shape, and ride-time
    calibration solves that new elevation against this ride's real moving
    time - corrupting the rider's suggested speed with no visible error.
    """
    from app.api.routes import _rematch_linked_activities

    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    activity_id, route_id = activity.id, route.id
    assert await match_activity_to_route(activity_id) == route_id

    # The rider re-routes the saved route somewhere else entirely and saves;
    # same row, same id, different road.
    far = destination_point((LAT, LON), 90.0, 5_000.0)
    route.geom = _line_wkt([far, destination_point(far, RUN_BEARING_DEG, RUN_LENGTH_M)])
    await db.commit()

    await _rematch_linked_activities(route_id, db)

    db.expire_all()
    row = (
        await db.execute(
            select(Activity.route_id, Activity.match_confidence).where(Activity.id == activity_id)
        )
    ).one()
    assert row.route_id is None, "a match against geometry that no longer exists must not survive"
    assert row.match_confidence is None


async def test_re_routing_leaves_a_hand_picked_match_alone(
    client: AsyncClient, db: AsyncSession
) -> None:
    """match_locked is the rider's decision, and re-routing is not a reason to
    overrule it - the re-derive goes through match_activity_to_route precisely
    so it inherits that check rather than reimplementing it."""
    from app.api.routes import _rematch_linked_activities

    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    activity_id, route_id = activity.id, route.id

    linked = await client.put(
        f"/api/activities/{activity_id}/route", json={"route_id": str(route_id)}
    )
    assert linked.status_code == 200
    assert linked.json()["match_locked"] is True

    far = destination_point((LAT, LON), 90.0, 5_000.0)
    route.geom = _line_wkt([far, destination_point(far, RUN_BEARING_DEG, RUN_LENGTH_M)])
    await db.commit()
    # The PUT above went through the API's own session, so this one still
    # holds the pre-lock Activity; without expiring it the re-derive reads a
    # stale match_locked and the assertion below passes or fails for the
    # wrong reason. In the app both run on the same session, so this is a
    # test artifact rather than the behaviour under test.
    db.expire_all()

    await _rematch_linked_activities(route_id, db)

    db.expire_all()
    stored = await db.scalar(select(Activity.route_id).where(Activity.id == activity_id))
    assert stored == route_id


async def test_saving_a_re_routed_route_triggers_the_re_derive(
    client: AsyncClient, snapshot: RouteResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH with a new snapshot must actually call the re-derive.

    The two tests above pin what `_rematch_linked_activities` does; this one
    pins that the endpoint calls it, which is a separate failure - the helper
    could be perfect and simply never run. It also pins the other half: a
    PATCH that only renames a route changes no geometry, so nothing needs
    re-deriving and the matches must be left alone.
    """
    import app.api.routes as routes_api

    await register(client, "rider@example.com")
    saved = await client.post(
        "/api/routes",
        json={
            "name": "Re-routed",
            "waypoints": WAYPOINTS,
            "preset": "gravel",
            "snapshot": snapshot.model_dump(),
        },
    )
    assert saved.status_code == 201, saved.text
    created = saved.json()

    called: list[uuid.UUID] = []

    async def _spy(route_id: uuid.UUID, db: object) -> None:
        called.append(route_id)

    monkeypatch.setattr(routes_api, "_rematch_linked_activities", _spy)

    renamed = await client.patch(f"/api/routes/{created['id']}", json={"name": "Just a rename"})
    assert renamed.status_code == 200
    assert called == [], "a rename changes no geometry, so nothing is stale"

    resaved = await client.patch(
        f"/api/routes/{created['id']}", json={"snapshot": snapshot.model_dump()}
    )
    assert resaved.status_code == 200
    assert called == [uuid.UUID(created["id"])]


async def test_an_explicit_rematch_clears_a_stale_match_and_it_persists(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The rematch endpoint is the rider saying "this link is wrong".

    Two separate failures hid behind each other here, and this pins both.
    The endpoint has to ASK for clear_if_unmatched - without it a link to a
    route that has since been re-routed elsewhere survives an explicit
    rematch, which is the exact case the flag exists for. And the clear has
    to be DURABLE: the branch that clears used to return from inside the
    SAVEPOINT, skipping the function's own commit, so the clear was visible
    to the calling session and then rolled back at close. The endpoint
    returned 200 either way, so nothing surfaced.

    The re-read below therefore goes through a fresh connection rather than
    the ORM object or the same session - a same-session read passes even
    when nothing was ever written.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    activity_id = activity.id
    assert await match_activity_to_route(activity_id) == route.id

    # The route is re-routed somewhere else entirely, so the stored match no
    # longer describes anything.
    far = destination_point((LAT, LON), 90.0, 5_000.0)
    await db.execute(
        text("UPDATE routes SET geom = ST_GeomFromEWKT(:wkt) WHERE id = :id"),
        {
            "wkt": _line_wkt([far, destination_point(far, RUN_BEARING_DEG, RUN_LENGTH_M)]),
            "id": route.id,
        },
    )
    await db.commit()

    response = await client.post(f"/api/activities/{activity_id}/rematch")
    assert response.status_code == 200, response.text
    assert response.json()["route_id"] is None

    db.expire_all()
    persisted = await db.scalar(select(Activity.route_id).where(Activity.id == activity_id))
    assert persisted is None, "the clear must survive the request, not just be visible inside it"
    confidence = await db.scalar(
        select(Activity.match_confidence).where(Activity.id == activity_id)
    )
    assert confidence is None


async def test_a_failing_commit_does_not_escape_the_matcher(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Never raises" has to include the commit, which is the risky part.

    The attribute assignments do not flush until the commit runs, so that one
    call is the most exposed to a real operational failure - a dropped
    connection, a deadlock, a statement timeout - and it briefly sat outside
    the try/except that makes this function safe to call.

    It matters because _rematch_linked_activities iterates this with no
    try/except of its own, from a PATCH whose route save has already
    committed: an escape there 500s a request that actually succeeded and
    silently abandons every remaining ride's re-derive.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    activity_id = activity.id

    original = AsyncSession.commit

    async def _boom(self: AsyncSession) -> None:
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(AsyncSession, "commit", _boom)
    try:
        matched = await match_activity_to_route(activity_id)
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original)

    assert matched is None, "a failed commit is a failed match, not an exception"


async def test_a_failed_match_commit_leaves_the_caller_object_usable(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback must not strand the caller holding an expired object.

    A rollback expires every object in the session - true regardless of
    expire_on_commit, which only governs the success path - including the
    Activity the caller loaded before calling this and goes on using
    afterwards. import_activity does exactly that: it matches, then builds its
    response from the same object. An expired one turns the next attribute
    read into an implicit lazy load, which AsyncSession refuses outside a
    greenlet, so the rider gets a 500 for a ride that was already durably
    committed - the very bug the SAVEPOINT exists to prevent, reintroduced on
    the commit-failure path when that path learned to roll back.

    The matcher therefore re-loads the activity after rolling back,
    repopulating the caller's own identity-mapped instance.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    real_commit = AsyncSession.commit

    async def _boom(self: AsyncSession) -> None:
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(AsyncSession, "commit", _boom)
    try:
        matched = await match_activity_to_route(activity.id)
    finally:
        monkeypatch.setattr(AsyncSession, "commit", real_commit)

    assert matched is None

    # The caller's own reference, used the way import_activity uses it. This
    # raises MissingGreenlet if the rollback left it expired.
    assert activity.name == "Test ride"
    assert activity.user_id == user_id


async def test_a_due_north_route_matches_its_own_ride(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An exactly axis-aligned route must match a ride of it.

    Round 6 found that it did not, and the cause was not a threshold: GEOS's
    default overlay returns LINESTRING EMPTY for
    ST_Intersection(ST_Buffer(line, tol), line) when the line is exactly due
    north or due east, so coverage read 0 and the ride silently never linked.
    ST_Simplify manufactures exactly that input from an ordinary near-north
    road, so canal towpaths, disused railways and Roman roads were the real
    population - see _OVERLAY_GRID_SIZE_M.

    Every other test in this file tilts its geometry by RUN_BEARING_DEG,
    which is precisely why none of them could see this. This one must stay
    exactly axis-aligned to keep its meaning.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    due_north = [(LAT, LON), (LAT + 0.009, LON), (LAT + 0.018, LON)]
    route = _route(user_id, due_north, RUN_LENGTH_M)
    activity = _activity(user_id, due_north, RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == route.id
    await db.refresh(activity)
    assert activity.match_confidence is not None
    assert activity.match_confidence == pytest.approx(1.0, rel=0.03)


async def test_a_due_east_route_matches_its_own_ride(client: AsyncClient, db: AsyncSession) -> None:
    """The other axis. The degeneracy is not specific to north - a bearing
    sweep measured covered_len 0 at both 0 and 90 degrees and full coverage
    at every bearing between, so fixing only the one this was first noticed
    on would leave the identical bug one axis over."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    due_east = [(LAT, LON), (LAT, LON + 0.012), (LAT, LON + 0.024)]
    route = _route(user_id, due_east, RUN_LENGTH_M)
    activity = _activity(user_id, due_east, RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == route.id


async def test_the_candidate_ranking_is_not_distorted_by_longitude(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Candidate ranking must be in ground metres, not raw degrees.

    A degree of longitude is ~68.8km at 51.8N against ~111.1km for a degree
    of latitude, so ST_Distance over SRID 4326 makes an east-west offset
    score ~1.6x larger than the identical real offset north-south. The
    candidate list is capped at MAX_CANDIDATES, so that distortion does not
    merely reorder - it decides which routes get scored at all, and a
    genuine match displaced east can be pushed off the end by decoys that
    are further away on the ground but nearer in degrees.

    The fixture is built around that asymmetry, and every part of it is
    load-bearing:

      - The line runs due EAST, so a north shift is pure cross-track (it
        breaks coverage) and an east shift is pure along-track (it barely
        touches coverage). On the tilted line the rest of this file uses,
        no combination of offsets can produce the required ordering at all.
      - The true route is 30m EAST: 0.000436 degrees, but only 30m of
        ground, and it still covers 1970/2000 = 0.985.
      - The decoys are ~44.5m NORTH: 0.000400 degrees - NEARER in degrees
        than the true route despite being FURTHER on the ground - and past
        COVERAGE_BUFFER_M, so not one of them can win on merit. They exist
        only to fill the LIMIT.

    Ranked in degrees all 25 decoys sort ahead of the true route and it
    never reaches the coverage stage; ranked in metres it sorts first.
    test_the_true_route_wins_even_when_many_candidates_qualify cannot catch
    this because it offsets every candidate along a single axis, and one
    axis scales uniformly.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    # Degrees per metre at this latitude, used to state the fixture in
    # metres and convert once, rather than hand-writing decimal degrees.
    deg_per_m_lon = 1.0 / (111320.0 * math.cos(math.radians(LAT)))
    deg_per_m_lat = 1.0 / 111132.0
    length_deg = RUN_LENGTH_M * deg_per_m_lon

    def _due_east(shift_east_m: float = 0.0, shift_north_m: float = 0.0) -> list[Point]:
        lat = LAT + shift_north_m * deg_per_m_lat
        lon = LON + shift_east_m * deg_per_m_lon
        return [(lat, lon), (lat, lon + length_deg)]

    activity = _activity(user_id, _due_east(), RUN_LENGTH_M)
    true_route = _route(user_id, _due_east(shift_east_m=30.0), RUN_LENGTH_M)
    db.add_all([activity, true_route])

    true_route_deg = 30.0 * deg_per_m_lon
    for i in range(MAX_CANDIDATES):
        # Anchored at the activity's own start and fanning north, NOT shifted
        # bodily north: a due-east line has a zero-height bounding box, so a
        # bodily-shifted decoy fails `geom && geom` and never becomes a
        # candidate at all - it cannot crowd a list it is not on. Sharing the
        # start point keeps the boxes overlapping. The far end is 2*north_m
        # away, so the centroid sits north_m off and most of the line is well
        # beyond the buffer.
        north_m = 44.5 + i * 0.02
        assert north_m * deg_per_m_lat < true_route_deg, (
            "decoy must rank ahead of the true route in raw degrees, "
            "or the fixture does not reproduce the bug"
        )
        far = _due_east(shift_north_m=2.0 * north_m)[1]
        db.add(_route(user_id, [(LAT, LON), far], RUN_LENGTH_M))
    await db.commit()

    matched = await match_activity_to_route(activity.id)

    assert matched == true_route.id


async def test_a_manual_pick_during_a_running_match_is_not_clobbered(
    client: AsyncClient,
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rider locking the activity mid-match must win.

    match_locked used to be read once, immediately after the activity was
    loaded, and the write then went ahead unconditionally - with the whole
    bidirectional-coverage query (simplify, buffer and intersect over up to
    MAX_CANDIDATES routes) sitting in between. A manual PUT .../route
    landing in that window was lost, and lost invisibly: SQLAlchemy's unit
    of work only emits columns it saw change, so the rider's
    match_locked=true survived while their route_id was overwritten by the
    auto-matcher's guess. The result is a row nothing can tell from a
    genuine manual pick - locked against every future pass, pointing at the
    wrong route. The guard now lives in the UPDATE's WHERE clause, so there
    is no window to lose.

    The race is driven for real, not simulated: a second independent
    session commits the manual pick from inside the coverage query itself.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    auto_route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    manual_route = _route(user_id, _north_leg(offset_east_m=5000.0), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([auto_route, manual_route, activity])
    await db.commit()
    activity_id, manual_id = activity.id, manual_route.id

    real_execute = AsyncSession.execute
    raced = False

    async def racing_execute(self: AsyncSession, statement: object, *a: object, **k: object):  # type: ignore[no-untyped-def]
        result = await real_execute(self, statement, *a, **k)  # type: ignore[arg-type]
        nonlocal raced
        # Fire once, after the coverage CTE has run but before the write -
        # exactly the window the old read-once check left open.
        if not raced and "act_tol" in str(statement):
            raced = True
            async with db_factory() as other:
                await other.execute(
                    update(Activity)
                    .where(Activity.id == activity_id)
                    .values(route_id=manual_id, match_locked=True, match_confidence=None)
                )
                await other.commit()
        return result

    monkeypatch.setattr(AsyncSession, "execute", racing_execute)
    matched = await match_activity_to_route(activity_id)
    monkeypatch.undo()

    assert raced, "the manual pick never landed; the race was not reproduced"
    # Nothing of ours was written, so nothing of ours may be reported.
    assert matched is None

    async with db_factory() as check:
        row = (
            await check.execute(
                select(Activity.route_id, Activity.match_locked, Activity.match_confidence).where(
                    Activity.id == activity_id
                )
            )
        ).one()
    assert row.route_id == manual_id, "the rider's pick was overwritten"
    assert row.match_locked is True
    assert row.match_confidence is None


async def test_rematch_of_a_concurrently_deleted_activity_404s_rather_than_500s(
    client: AsyncClient,
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the ride while its rematch runs must not 500.

    rematch_activity does slow work (the whole coverage query) and then
    refreshes the activity to build its response. A DELETE landing in that
    gap made the refresh raise ObjectDeletedError, which surfaced as a 500.
    404 is the honest answer - the work happened, the resource is gone.
    """
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()
    activity_id = activity.id

    real = match_route_module.match_activity_to_route
    deleted = False

    async def deleting_match(*a: object, **k: object) -> uuid.UUID | None:
        nonlocal deleted
        result = await real(*a, **k)  # type: ignore[arg-type]
        async with db_factory() as other:
            await other.execute(delete(Activity).where(Activity.id == activity_id))
            await other.commit()
        deleted = True
        return result

    monkeypatch.setattr("app.api.activities.match_activity_to_route", deleting_match)
    response = await client.post(f"/api/activities/{activity_id}/rematch")

    assert deleted, "the delete never landed; the race was not reproduced"
    assert response.status_code == 404, response.text
