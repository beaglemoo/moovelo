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
postgres.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Route, User
from app.services.geo import Point, destination_point
from app.services.route_match import (
    COVERAGE_BUFFER_M,
    MIN_COVERAGE,
    match_activity_to_route,
)
from tests.conftest import register

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

    matched = await match_activity_to_route(db, activity.id)

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

    matched = await match_activity_to_route(db, activity.id)

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

    matched = await match_activity_to_route(db, activity.id)

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

    matched = await match_activity_to_route(db, activity.id)

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

    matched = await match_activity_to_route(db, activity.id)

    assert matched == route.id
    await db.refresh(activity)
    assert activity.match_confidence is not None
    assert activity.match_confidence >= MIN_COVERAGE


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

    matched = await match_activity_to_route(db, activity.id)

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

    matched = await match_activity_to_route(db, activity.id)

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
    assert await match_activity_to_route(db, activity_id) == route.id

    await db.execute(delete(Route).where(Route.id == route.id))
    await db.commit()

    # A fresh column read, not a re-read of the same ORM object, so a stale
    # identity-mapped instance cannot hide a FK that never actually cleared.
    route_id_after = await db.scalar(select(Activity.route_id).where(Activity.id == activity_id))
    assert route_id_after is None


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
    result = await match_activity_to_route(db, activity_id)

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

    matched = await match_activity_to_route(db, activity.id)

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
    assert await match_activity_to_route(db, activity.id) == route.id

    response = await client.get("/api/activities")

    assert response.status_code == 200
    row = next(item for item in response.json() if item["id"] == str(activity.id))
    assert row["route_id"] == str(route.id)
    assert row["route_name"] == route.name
