"""services/route_match.py: matching a recorded ride to the saved route it
followed, plus the API surface around it (PUT .../route, POST .../rematch).

Routes and activities are seeded directly with WKT/EWKT geometry, the same
technique test_coverage.py uses for CycleWay/CycleWayMember - both are
tables the app writes to itself, but building the exact shapes this module's
Frechet-distance maths needs is easier as a straight line than as a real
Valhalla response.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Route, User
from app.services.geo import Point, destination_point
from app.services.route_match import MAX_MATCH_DISTANCE_M, match_activity_to_route
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
# original's: the cheap `&&` prefilter would reject it before the Frechet
# maths this module exists to test ever ran. Tilting the run gives it a
# real bounding-box width (RUN_LENGTH_M * sin(8 deg) =~ 280m here) while a
# uniform east shift of the start point keeps both lines parallel, so the
# Frechet distance between them stays exactly the shift distance.
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
    """A ride that closely tracks a saved route matches it, and the stored
    confidence is a small, sane number of metres - not the route's whole
    length and not zero-but-suspiciously-exact."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    # 30m off the route's line for the whole run - a plausible real-world
    # GPS/road-position deviation, comfortably under MAX_MATCH_DISTANCE_M.
    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=30.0), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(db, activity.id)

    assert matched == route.id
    await db.refresh(activity)
    assert activity.route_id == route.id
    assert activity.match_confidence is not None
    assert activity.match_confidence == pytest.approx(30.0, rel=0.15)


async def test_a_ride_on_a_different_road_does_not_match(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Comfortably over MAX_MATCH_DISTANCE_M is not "the same road with GPS
    noise" - it must not match. The offset is kept under RUN_WIDTH_M so this
    still passes the cheap bbox/distance-ratio prefilter (same length,
    overlapping bounding boxes) and is rejected by the Frechet confirmation
    itself, not merely filtered out earlier for an unrelated reason."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    offset_m = RUN_WIDTH_M * 0.7
    assert offset_m > MAX_MATCH_DISTANCE_M, "fixture must actually exceed the threshold"

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
    """Frechet distance is start-to-start: comparing the activity directly
    against a route stored in the opposite point order (planned one way,
    ridden the other) would score as if they were unrelated shapes. Taking
    the minimum against ST_Reverse(route.geom) is what recovers this."""
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
    assert activity.match_confidence < MAX_MATCH_DISTANCE_M


async def test_mercator_distortion_is_corrected_to_true_ground_metres(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The test the module docstring promises: an exactly-known 100m offset
    at this UK latitude must read back as ~100m, not the ~162m
    (100 / cos(51.8 deg)) EPSG:3857 would report uncorrected. Both numbers
    are on the same side of MAX_MATCH_DISTANCE_M (150) as each other in a
    way that makes the bug observable through the public behaviour, not just
    the raw number: uncorrected, this fixture would NOT match at all."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    true_offset_m = 100.0
    uncorrected_estimate_m = true_offset_m / 0.6184  # cos(51.8 deg), rounded

    route = _route(user_id, _north_leg(), RUN_LENGTH_M)
    activity = _activity(user_id, _north_leg(offset_east_m=true_offset_m), RUN_LENGTH_M)
    db.add_all([route, activity])
    await db.commit()

    matched = await match_activity_to_route(db, activity.id)

    assert matched == route.id, (
        "with the cos(latitude) correction removed this offset reads as "
        f"~{uncorrected_estimate_m:.0f}m, over the {MAX_MATCH_DISTANCE_M}m "
        "threshold, and would not match at all"
    )
    await db.refresh(activity)
    assert activity.match_confidence == pytest.approx(true_offset_m, rel=0.1)
    assert activity.match_confidence < uncorrected_estimate_m * 0.8


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
