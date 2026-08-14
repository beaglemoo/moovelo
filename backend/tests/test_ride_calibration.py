"""Ride-time calibration: fitting flat_speed_kmh from the rider's own
matched rides - services/ride_calibration.py, and the read-only
GET /api/settings/ride-time-suggestion wired to it.

Known-speed fixtures are built by running compute_ride_time itself at a
chosen speed and feeding its own total back in as a ride's moving_time_s,
so the expected answer is exact rather than eyeballed - the technique the
module's own docstring points at, and the one the project's other golden
fixtures (e.g. test_ride_time.py) already use for anything derived from the
model rather than hand-picked.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Route, User
from app.schemas import ElevationPoint, UserSettingsResponse
from app.services.ride_calibration import (
    MAX_FLAT_SPEED_KMH,
    MIN_FLAT_SPEED_KMH,
    MIN_USABLE_RIDES,
    solve_flat_speed_kmh,
    suggest_flat_speed_kmh,
)
from app.services.ride_time import compute_ride_time
from tests.conftest import register

LAT, LON = 51.8000, -0.6500

# A mixed profile - climb, descent, flat - rather than one constant grade,
# so the fit exercises compute_ride_time's gradient factor in more than one
# band. Grades stay under 12% (nowhere near MIN_SPEED_KMH's floor at any
# speed in the 5-60 range) so the monotonicity test below has no flat
# region to trip over.
_ELEVATION = [
    ElevationPoint(dist_m=0.0, elev_m=100.0),
    ElevationPoint(dist_m=1000.0, elev_m=160.0),  # +6% climb
    ElevationPoint(dist_m=2000.0, elev_m=100.0),  # -6% descent
    ElevationPoint(dist_m=3000.0, elev_m=100.0),  # flat
]


def _settings(flat_speed_kmh: float = 22.0) -> UserSettingsResponse:
    return UserSettingsResponse(weight_kg=78.0, flat_speed_kmh=flat_speed_kmh, ftp_watts=None)


def _line_wkt(offset: float = 0.0) -> str:
    return f"SRID=4326;LINESTRING({LON + offset} {LAT}, {LON + offset} {LAT + 0.01})"


def _route(user_id: uuid.UUID, elevation: list[ElevationPoint] | None = None) -> Route:
    points = elevation if elevation is not None else _ELEVATION
    return Route(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Calibration loop",
        preset="road",
        source="planned",
        waypoints=[],
        legs=[],
        elevation=[p.model_dump() for p in points],
        distance_m=points[-1].dist_m if points else 0.0,
        duration_s=200.0,
        ascent_m=60.0,
        descent_m=60.0,
        geom=_line_wkt(),
    )


def _activity(
    user_id: uuid.UUID,
    *,
    route_id: uuid.UUID | None,
    moving_time_s: float | None,
    name: str = "Ride",
    offset: float = 0.0,
) -> Activity:
    return Activity(
        id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        distance_m=3000.0,
        ascent_m=60.0,
        descent_m=60.0,
        moving_time_s=moving_time_s,
        elapsed_time_s=moving_time_s,
        elevation=[],
        geom=_line_wkt(offset),
        route_id=route_id,
        match_confidence=0.9 if route_id is not None else None,
        match_locked=route_id is not None,
    )


async def _user_id(db: AsyncSession, email: str) -> uuid.UUID:
    return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


def _known_actual_time_s(known_speed_kmh: float) -> float:
    points = compute_ride_time(_ELEVATION, None, _settings(known_speed_kmh))
    return points[-1].time_s


class TestSolveFlatSpeedKmh:
    def test_recovers_a_known_speed(self) -> None:
        known_speed = 24.0
        actual_time_s = _known_actual_time_s(known_speed)

        # Starting settings' own flat_speed_kmh (22.0) is irrelevant to the
        # fit - only weight/FTP carry through, both defaults here - and
        # deliberately different from known_speed so the test cannot pass
        # by the solver simply echoing its input back.
        solved = solve_flat_speed_kmh(_ELEVATION, None, _settings(22.0), actual_time_s)

        assert solved is not None
        assert solved == pytest.approx(known_speed, abs=0.1)

    def test_a_second_known_speed_is_also_recovered(self) -> None:
        known_speed = 30.5
        actual_time_s = _known_actual_time_s(known_speed)

        solved = solve_flat_speed_kmh(_ELEVATION, None, _settings(15.0), actual_time_s)

        assert solved is not None
        assert solved == pytest.approx(known_speed, abs=0.1)

    def test_a_ride_slower_than_the_range_clamps_to_the_minimum(self) -> None:
        # No flat_speed_kmh in [5, 60] predicts a total this long over this
        # short a profile - the slowest end of the range is the closest
        # representable answer, not a failure to solve.
        solved = solve_flat_speed_kmh(_ELEVATION, None, _settings(22.0), 100_000.0)
        assert solved == MIN_FLAT_SPEED_KMH

    def test_a_ride_faster_than_the_range_clamps_to_the_maximum(self) -> None:
        solved = solve_flat_speed_kmh(_ELEVATION, None, _settings(22.0), 1.0)
        assert solved == MAX_FLAT_SPEED_KMH

    def test_too_short_for_a_gradient_returns_none(self) -> None:
        one_point = [ElevationPoint(dist_m=0.0, elev_m=100.0)]
        assert solve_flat_speed_kmh(one_point, None, _settings(22.0), 100.0) is None

    def test_non_positive_actual_time_returns_none(self) -> None:
        assert solve_flat_speed_kmh(_ELEVATION, None, _settings(22.0), 0.0) is None
        assert solve_flat_speed_kmh(_ELEVATION, None, _settings(22.0), -5.0) is None

    def test_monotonically_decreasing_over_the_full_range(self) -> None:
        """The property bisection depends on: raising flat_speed_kmh can only
        shorten the predicted total, never lengthen it, over the whole 5-60
        range PATCH /api/settings would accept. Asserted directly rather
        than assumed, per the module's own docstring."""
        totals = [
            compute_ride_time(_ELEVATION, None, _settings(float(v)))[-1].time_s
            for v in range(int(MIN_FLAT_SPEED_KMH), int(MAX_FLAT_SPEED_KMH) + 1)
        ]
        assert len(totals) == int(MAX_FLAT_SPEED_KMH) - int(MIN_FLAT_SPEED_KMH) + 1
        assert all(earlier > later for earlier, later in zip(totals, totals[1:], strict=False))


async def test_suggestion_recovers_a_known_speed_across_five_rides(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    known_speed = 26.0
    actual_time_s = _known_actual_time_s(known_speed)

    route = _route(user_id)
    db.add(route)
    await db.commit()
    for i in range(MIN_USABLE_RIDES):
        db.add(
            _activity(
                user_id,
                route_id=route.id,
                moving_time_s=actual_time_s,
                name=f"Ride {i}",
                offset=i * 0.001,
            )
        )
    await db.commit()

    response = await client.get("/api/settings/ride-time-suggestion")
    assert response.status_code == 200
    body = response.json()
    assert body is not None
    assert body["sample_size"] == MIN_USABLE_RIDES
    assert body["suggested_kmh"] == pytest.approx(known_speed, abs=0.2)
    # A fresh user's settings row does not exist yet, so current_kmh is the
    # in-code default rather than anything applied.
    assert body["current_kmh"] == pytest.approx(22.0)


async def test_the_median_rejects_a_single_outlier_ride(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    known_speed = 26.0
    actual_time_s = _known_actual_time_s(known_speed)

    route = _route(user_id)
    db.add(route)
    await db.commit()
    for i in range(MIN_USABLE_RIDES):
        db.add(
            _activity(
                user_id,
                route_id=route.id,
                moving_time_s=actual_time_s,
                name=f"Ride {i}",
                offset=i * 0.001,
            )
        )
    # One absurd ride - a near-instant "ride" implying the fastest speed the
    # model can express. A mean would drag the suggestion well above
    # known_speed; the median must not move it far at all.
    db.add(
        _activity(
            user_id,
            route_id=route.id,
            moving_time_s=1.0,
            name="Outlier",
            offset=0.999,
        )
    )
    await db.commit()

    response = await client.get("/api/settings/ride-time-suggestion")
    assert response.status_code == 200
    body = response.json()
    assert body is not None
    assert body["sample_size"] == MIN_USABLE_RIDES + 1
    assert body["suggested_kmh"] == pytest.approx(known_speed, abs=0.2)


async def test_under_the_floor_returns_no_suggestion(client: AsyncClient, db: AsyncSession) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id)
    db.add(route)
    await db.commit()
    for i in range(MIN_USABLE_RIDES - 1):
        db.add(
            _activity(
                user_id,
                route_id=route.id,
                moving_time_s=_known_actual_time_s(26.0),
                name=f"Ride {i}",
                offset=i * 0.001,
            )
        )
    await db.commit()

    response = await client.get("/api/settings/ride-time-suggestion")
    assert response.status_code == 200
    assert response.json() is None


async def test_unmatched_and_timeless_rides_do_not_count_toward_the_floor(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A route_id with no moving_time_s, and moving_time_s with no route_id,
    both look like usable data at a glance - and neither is. Enough of each
    to clear the floor on their own if they wrongly counted, alongside just
    under the floor's worth of genuinely usable rides."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id)
    db.add(route)
    await db.commit()

    for i in range(MIN_USABLE_RIDES - 1):
        db.add(
            _activity(
                user_id,
                route_id=route.id,
                moving_time_s=_known_actual_time_s(26.0),
                name=f"Usable {i}",
                offset=i * 0.001,
            )
        )
    for i in range(3):
        db.add(
            _activity(
                user_id,
                route_id=route.id,
                moving_time_s=None,
                name=f"No moving time {i}",
                offset=0.5 + i * 0.001,
            )
        )
    for i in range(3):
        db.add(
            _activity(
                user_id,
                route_id=None,
                moving_time_s=_known_actual_time_s(26.0),
                name=f"Unmatched {i}",
                offset=0.7 + i * 0.001,
            )
        )
    await db.commit()

    response = await client.get("/api/settings/ride-time-suggestion")
    assert response.status_code == 200
    assert response.json() is None


async def test_suggestion_never_draws_on_another_users_rides(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")

    known_speed = 28.0
    actual_time_s = _known_actual_time_s(known_speed)
    route = _route(owner_id)
    db.add(route)
    await db.commit()
    for i in range(MIN_USABLE_RIDES):
        db.add(
            _activity(
                owner_id,
                route_id=route.id,
                moving_time_s=actual_time_s,
                name=f"Owner ride {i}",
                offset=i * 0.001,
            )
        )
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    stranger_response = await client.get("/api/settings/ride-time-suggestion")
    assert stranger_response.status_code == 200
    assert stranger_response.json() is None

    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse-9"}
    )
    owner_response = await client.get("/api/settings/ride-time-suggestion")
    assert owner_response.status_code == 200
    body = owner_response.json()
    assert body is not None
    assert body["sample_size"] == MIN_USABLE_RIDES
    assert body["suggested_kmh"] == pytest.approx(known_speed, abs=0.2)


async def test_suggestion_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/api/settings/ride-time-suggestion")).status_code == 401


async def test_low_level_suggest_function_only_sees_the_given_user(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same isolation as the endpoint test above, exercised one layer down
    against suggest_flat_speed_kmh directly - the join filters on both
    Activity.user_id and Route.user_id, not on route_id alone."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    await register(client, "stranger@example.com")
    stranger_id = await _user_id(db, "stranger@example.com")

    known_speed = 28.0
    actual_time_s = _known_actual_time_s(known_speed)
    route = _route(owner_id)
    db.add(route)
    await db.commit()
    for i in range(MIN_USABLE_RIDES):
        db.add(
            _activity(
                owner_id,
                route_id=route.id,
                moving_time_s=actual_time_s,
                name=f"Owner ride {i}",
                offset=i * 0.001,
            )
        )
    await db.commit()

    stranger_suggestion = await suggest_flat_speed_kmh(db, stranger_id, _settings())
    assert stranger_suggestion is None

    owner_suggestion = await suggest_flat_speed_kmh(db, owner_id, _settings())
    assert owner_suggestion is not None
    assert owner_suggestion.sample_size == MIN_USABLE_RIDES


async def test_a_ride_linked_to_another_users_route_is_not_used(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query filters user_id on BOTH tables, and this is what that second
    filter is for.

    Nothing in the app can currently produce this row: PUT
    /api/activities/{id}/route checks both sides, and route_match.py only
    ever considers the rider's own routes. The foreign key does not care,
    though, so the state is one bad query or one future endpoint away - and
    the module's docstring claims isolation here does not depend on those
    call sites staying correct. Without this test that claim is prose:
    deleting `Route.user_id == user_id` leaves every other test in this file
    passing, because `Activity.user_id` alone still filters the rows they
    seed.

    Seeded directly through the session for that reason - going through the
    API could not create it.
    """
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    await client.post("/api/auth/logout")
    await register(client, "stranger@example.com")
    stranger_id = await _user_id(db, "stranger@example.com")

    stranger_route = _route(stranger_id)
    db.add(stranger_route)
    await db.commit()

    # Enough of them to clear the floor, so a leak would surface as a real
    # suggestion rather than being hidden by MIN_USABLE_RIDES.
    actual_time_s = _known_actual_time_s(28.0)
    for i in range(MIN_USABLE_RIDES):
        db.add(
            _activity(
                owner_id,
                route_id=stranger_route.id,
                moving_time_s=actual_time_s,
                name=f"Cross-linked ride {i}",
                offset=i * 0.001,
            )
        )
    await db.commit()

    suggestion = await suggest_flat_speed_kmh(db, owner_id, _settings())

    assert suggestion is None, (
        "a ride pointing at another user's route must not feed the fit - "
        "its elevation and surface are that other rider's data"
    )
