"""GET /api/routes/{id}/activities: the rides matched to a route, plus the
route's own predicted ride time - the read side of Phase 11's
planned-vs-actual comparison.

Routes and activities are seeded directly, the same technique
test_route_match.py uses: straight-line WKT geometry is easier to build
than a real Valhalla response, and this endpoint does not exercise the
matching algorithm itself (that is test_route_match.py's job) - here the
activity is simply given a route_id and a match_confidence up front.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Route, User
from tests.conftest import register

LAT, LON = 51.8000, -0.6500


def _line_wkt(offset: float = 0.0) -> str:
    return f"SRID=4326;LINESTRING({LON + offset} {LAT}, {LON + offset} {LAT + 0.01})"


def _route(user_id: uuid.UUID, *, with_elevation: bool = True) -> Route:
    return Route(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Chilterns loop",
        preset="gravel",
        source="planned",
        waypoints=[],
        legs=[],
        elevation=(
            [{"dist_m": 0.0, "elev_m": 100.0}, {"dist_m": 1000.0, "elev_m": 120.0}]
            if with_elevation
            else []
        ),
        distance_m=1000.0,
        duration_s=200.0,
        ascent_m=20.0,
        descent_m=0.0,
        geom=_line_wkt(),
    )


def _activity(
    user_id: uuid.UUID,
    *,
    route_id: uuid.UUID | None = None,
    match_confidence: float | None = None,
    name: str = "Test ride",
) -> Activity:
    return Activity(
        id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        distance_m=1050.0,
        ascent_m=22.0,
        descent_m=2.0,
        moving_time_s=210.0,
        elapsed_time_s=240.0,
        elevation=[],
        geom=_line_wkt(),
        route_id=route_id,
        match_confidence=match_confidence,
        match_locked=route_id is not None,
    )


async def _user_id(db: AsyncSession, email: str) -> uuid.UUID:
    return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_lists_matched_rides_newest_first_with_predicted_time(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id)
    db.add(route)
    await db.commit()
    # route.id has to exist in the database before an activity referencing
    # it can be inserted - activities.route_id is a real foreign key.
    older = _activity(user_id, route_id=route.id, match_confidence=0.91, name="Older ride")
    newer = _activity(user_id, route_id=route.id, match_confidence=0.97, name="Newer ride")
    db.add_all([older, newer])
    await db.commit()
    # started_at is null on both, so ordering falls back to created_at -
    # force it apart rather than relying on insertion order.
    await db.execute(
        update(Activity)
        .where(Activity.id == older.id)
        .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await db.execute(
        update(Activity)
        .where(Activity.id == newer.id)
        .values(created_at=datetime(2026, 6, 1, tzinfo=UTC))
    )
    await db.commit()

    response = await client.get(f"/api/routes/{route.id}/activities")
    assert response.status_code == 200
    body = response.json()

    assert [a["name"] for a in body["activities"]] == ["Newer ride", "Older ride"]
    assert body["activities"][0]["match_confidence"] == pytest.approx(0.97)
    assert body["activities"][1]["distance_m"] == pytest.approx(1050.0)

    # A plausible number of seconds for a flat ~1km route at the default
    # rider settings (~22 km/h), not merely non-null - see ride_time.py's
    # DEFAULT_FLAT_SPEED_KMH. 1000m at 22 km/h is ~164s; a wide but sane band.
    assert body["predicted_time_s"] is not None
    assert 60.0 < body["predicted_time_s"] < 600.0


async def test_empty_when_nothing_has_matched(client: AsyncClient, db: AsyncSession) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id)
    unrelated = _activity(user_id, route_id=None)
    db.add_all([route, unrelated])
    await db.commit()

    response = await client.get(f"/api/routes/{route.id}/activities")
    assert response.status_code == 200
    body = response.json()
    assert body["activities"] == []
    # The route still has a predicted time even with no rides against it -
    # that half of the comparison does not depend on a match existing.
    assert body["predicted_time_s"] is not None


async def test_predicted_time_is_null_with_no_elevation(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    route = _route(user_id, with_elevation=False)
    db.add(route)
    await db.commit()

    response = await client.get(f"/api/routes/{route.id}/activities")
    assert response.status_code == 200
    assert response.json()["predicted_time_s"] is None


async def test_a_missing_route_is_a_404(client: AsyncClient) -> None:
    await register(client)
    response = await client.get(f"/api/routes/{uuid.uuid4()}/activities")
    assert response.status_code == 404


async def test_another_users_route_is_404_for_a_stranger(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst bug this endpoint could ship: user B asking for user A's
    route id must 404, and must never leak A's ride names or times."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    route = _route(owner_id)
    db.add(route)
    await db.commit()
    mine = _activity(owner_id, route_id=route.id, match_confidence=0.9, name="Owner's ride")
    db.add(mine)
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    response = await client.get(f"/api/routes/{route.id}/activities")
    assert response.status_code == 404
    assert "Owner's ride" not in response.text

    # And the owner still sees their own ride, which the 404 above could
    # have been hiding a leak behind rather than a real 404.
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse-9"}
    )
    again = await client.get(f"/api/routes/{route.id}/activities")
    assert again.status_code == 200
    assert [a["name"] for a in again.json()["activities"]] == ["Owner's ride"]
