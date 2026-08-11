"""GET /api/coverage/roads: the all-roads denominator alongside
/cycle-network. OsmWay is seeded directly, the same way test_coverage.py
seeds CycleWay/CycleWayMember - an indexer-owned table the app never writes
to itself.
"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, ActivityWay, OsmWay, SearchIndexMeta, User
from tests.conftest import register

# Same point as test_coverage.py's own choice, so a reviewer already
# familiar with that fixture recognises this one.
LAT, LON = 51.8000, -0.6500

BBOX = {"min_lat": LAT - 0.05, "min_lon": LON - 0.05, "max_lat": LAT + 0.05, "max_lon": LON + 0.05}
ELSEWHERE = {"min_lat": 55.0, "min_lon": 2.0, "max_lat": 55.1, "max_lon": 2.1}


def _way(
    way_id: int,
    highway: str,
    length_m: float,
    at: tuple[float, float] = (LAT, LON),
) -> OsmWay:
    """A road way with its own short shape - what a coverage bbox actually
    selects on, unlike a relation's envelope in the cycle-network case.
    osm_ways has no relation to be confused by, but the bbox still has to
    test the way's own geometry rather than anything wider."""
    lat, lon = at
    return OsmWay(
        way_id=way_id,
        highway=highway,
        name=None,
        length_m=length_m,
        geom=f"SRID=4326;LINESTRING({lon} {lat},{lon + 0.001} {lat + 0.001})",
    )


def _meta(way_count: int | None) -> SearchIndexMeta:
    return SearchIndexMeta(
        built_at=datetime.now(UTC),
        source_files=["tring.osm.pbf"],
        place_count=0,
        poi_count=0,
        cycle_way_count=0,
        cycle_way_member_count=None,
        osm_way_count=way_count,
    )


async def _seed_roads(db: AsyncSession) -> None:
    """A residential street (1000m) and a footway (500m), both near
    LAT/LON, plus a fully-built index."""
    db.add_all(
        [
            _way(100, "residential", 1000.0),
            _way(101, "footway", 500.0),
            _meta(2),
        ]
    )
    await db.commit()


async def _user_id(db: AsyncSession, email: str) -> object:
    return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_needs_a_session(client: AsyncClient) -> None:
    response = await client.get("/api/coverage/roads", params=BBOX)
    assert response.status_code == 401


async def test_an_unbuilt_index_says_so_rather_than_reporting_zero(client: AsyncClient) -> None:
    await register(client)

    response = await client.get("/api/coverage/roads", params=BBOX)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "not been built" in body["reason"]
    assert body["highways"] == []


async def test_an_index_that_predates_osm_ways_says_so(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An index built before this feature has places/pois/cycle routes but
    no osm_ways at all - reporting 0% would be as dishonest as reporting
    nothing was ever indexed, the same reasoning as cycle-network coverage's
    own predates-the-feature case."""
    db.add(_meta(None))
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/roads", params=BBOX)

    body = response.json()
    assert body["available"] is False
    assert "re-index" in body["reason"]


async def test_a_rider_with_no_activities_gets_an_honest_zero(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _seed_roads(db)
    await register(client)

    response = await client.get("/api/coverage/roads", params=BBOX)

    body = response.json()
    assert body["available"] is True
    by_highway = {row["highway"]: row for row in body["highways"]}
    assert by_highway["residential"]["ridden_m"] == pytest.approx(0.0)
    assert by_highway["residential"]["total_m"] == pytest.approx(1000.0)
    assert by_highway["footway"]["ridden_m"] == pytest.approx(0.0)
    assert by_highway["footway"]["total_m"] == pytest.approx(500.0)


async def test_a_bbox_with_no_roads_in_it_is_an_empty_list_not_a_failure(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _seed_roads(db)
    await register(client)

    response = await client.get("/api/coverage/roads", params=ELSEWHERE)

    body = response.json()
    assert body["available"] is True
    assert body["highways"] == []


async def test_coverage_arithmetic(client: AsyncClient, db: AsyncSession) -> None:
    await _seed_roads(db)
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    db.add(ActivityWay(user_id=user_id, way_id=100, first_ridden_at=datetime.now(UTC)))
    await db.commit()

    response = await client.get("/api/coverage/roads", params=BBOX)

    by_highway = {row["highway"]: row for row in response.json()["highways"]}
    assert by_highway["residential"]["ridden_m"] == pytest.approx(1000.0)
    assert by_highway["residential"]["total_m"] == pytest.approx(1000.0)
    assert by_highway["footway"]["ridden_m"] == pytest.approx(0.0)
    assert by_highway["footway"]["total_m"] == pytest.approx(500.0)


async def test_the_bbox_selects_ways_by_their_own_geometry(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A way far from the box must not be pulled in just because another way
    of the same class is near it."""
    db.add_all(
        [
            _way(100, "track", 1000.0),
            _way(101, "track", 9000.0, at=(54.0, -1.5)),  # Yorkshire, not the Chilterns
            _meta(2),
        ]
    )
    await db.commit()
    await register(client, "rider@example.com")

    body = (await client.get("/api/coverage/roads", params=BBOX)).json()

    track = next(row for row in body["highways"] if row["highway"] == "track")
    assert track["total_m"] == 1000.0, "the Yorkshire way is not near the Chilterns"


async def test_one_riders_coverage_never_counts_another_riders_ways(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """The worst bug this feature could ship - see the PR description for
    proof this bites: run once against a version of the road coverage SQL
    with the activity_ways.user_id filter removed, one rider's ridden metres
    leaking into every other rider's answer."""
    await _seed_roads(db)
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)

    await register(client, "one@example.com")
    one_id = await _user_id(db, "one@example.com")
    db.add(ActivityWay(user_id=one_id, way_id=100, first_ridden_at=datetime.now(UTC)))
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    response = await client.get("/api/coverage/roads", params=BBOX)

    by_highway = {row["highway"]: row for row in response.json()["highways"]}
    assert by_highway["residential"]["ridden_m"] == pytest.approx(0.0)


async def test_the_default_bbox_centres_on_the_riders_own_activities(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _seed_roads(db)
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    db.add(
        Activity(
            user_id=user_id,
            name="Near the roads",
            distance_m=1000.0,
            ascent_m=0.0,
            descent_m=0.0,
            elevation=[],
            geom=f"SRID=4326;LINESTRING({LON} {LAT}, {LON + 0.001} {LAT + 0.001})",
        )
    )
    await db.commit()

    response = await client.get("/api/coverage/roads")

    body = response.json()
    assert body["available"] is True
    assert {row["highway"] for row in body["highways"]} == {"residential", "footway"}


async def test_no_bbox_and_no_activities_says_to_import_a_ride(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(_meta(0))
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/roads")

    body = response.json()
    assert body["available"] is False
    assert "Import a ride" in body["reason"]


async def test_a_half_specified_bbox_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.get("/api/coverage/roads", params={"min_lat": LAT, "min_lon": LON})

    assert response.status_code == 422
