"""GET /api/coverage/cycle-network, and the backfill endpoints.

CycleWay and CycleWayMember are seeded directly, the same way
test_cycle_network.py already does for CycleWay - they are indexer-owned
tables the app never writes to itself.
"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, ActivityWay, CycleWay, CycleWayMember, SearchIndexMeta, User
from tests.conftest import register

# A point on the Chilterns, matching test_cycle_network.py's own choice so a
# reviewer already familiar with that fixture recognises this one.
LAT, LON = 51.8000, -0.6500

BBOX = {"min_lat": LAT - 0.05, "min_lon": LON - 0.05, "max_lat": LAT + 0.05, "max_lon": LON + 0.05}
ELSEWHERE = {"min_lat": 55.0, "min_lon": 2.0, "max_lat": 55.1, "max_lon": 2.1}


def _route(relation_id: int, network: str, at: tuple[float, float] = (LAT, LON)) -> CycleWay:
    lat, lon = at
    return CycleWay(
        id=relation_id,
        ref=f"{network.upper()} {relation_id}",
        name=None,
        network=network,
        operator=None,
        geom=f"SRID=4326;MULTILINESTRING(({lon} {lat},{lon + 0.01} {lat + 0.01}))",
    )


def _member(relation_id: int, way_id: int, length_m: float) -> CycleWayMember:
    return CycleWayMember(relation_id=relation_id, way_id=way_id, length_m=length_m)


def _meta(member_count: int | None) -> SearchIndexMeta:
    return SearchIndexMeta(
        built_at=datetime.now(UTC),
        source_files=["tring.osm.pbf"],
        place_count=0,
        poi_count=0,
        cycle_way_count=2,
        cycle_way_member_count=member_count,
    )


async def _seed_network(db: AsyncSession) -> None:
    """One NCN route (two ways, 1000m + 500m) and one RCN route (one way,
    800m), both near LAT/LON, plus a fully-built index."""
    db.add_all(
        [
            _route(1, "ncn"),
            _member(1, 100, 1000.0),
            _member(1, 101, 500.0),
            _route(2, "rcn"),
            _member(2, 200, 800.0),
            _meta(3),
        ]
    )
    await db.commit()


async def _user_id(db: AsyncSession, email: str) -> object:
    return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


# --- /cycle-network -----------------------------------------------------------


async def test_needs_a_session(client: AsyncClient) -> None:
    response = await client.get("/api/coverage/cycle-network", params=BBOX)
    assert response.status_code == 401


async def test_an_unbuilt_index_says_so_rather_than_reporting_zero(client: AsyncClient) -> None:
    """0% reads as 'you have ridden nothing'. An index that has never been
    built is a different claim entirely, and must say so."""
    await register(client)

    response = await client.get("/api/coverage/cycle-network", params=BBOX)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "not been built" in body["reason"]
    assert body["networks"] == []


async def test_an_index_that_predates_member_lengths_says_so(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The v0.3.0 upgrade path repeats itself: an index built before this
    feature has cycle_ways but no cycle_way_members, and reporting 0% would
    be just as dishonest as reporting nothing was ever indexed."""
    db.add(_meta(None))
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/cycle-network", params=BBOX)

    body = response.json()
    assert body["available"] is False
    assert "re-index" in body["reason"]


async def test_a_rider_with_no_activities_gets_an_honest_zero(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The empty case: a real, built index with real routes in the bbox, and
    a rider who has ridden none of them. 0% here is the correct answer, not
    the dishonest one the two tests above must avoid."""
    await _seed_network(db)
    await register(client)

    response = await client.get("/api/coverage/cycle-network", params=BBOX)

    body = response.json()
    assert body["available"] is True
    by_network = {row["network"]: row for row in body["networks"]}
    assert by_network["ncn"]["ridden_m"] == pytest.approx(0.0)
    assert by_network["ncn"]["total_m"] == pytest.approx(1500.0)
    assert by_network["rcn"]["ridden_m"] == pytest.approx(0.0)
    assert by_network["rcn"]["total_m"] == pytest.approx(800.0)


async def test_a_bbox_with_no_routes_in_it_is_an_empty_list_not_a_failure(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _seed_network(db)
    await register(client)

    response = await client.get("/api/coverage/cycle-network", params=ELSEWHERE)

    body = response.json()
    assert body["available"] is True
    assert body["networks"] == []


async def test_coverage_arithmetic(client: AsyncClient, db: AsyncSession) -> None:
    await _seed_network(db)
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    db.add(ActivityWay(user_id=user_id, way_id=100, first_ridden_at=datetime.now(UTC)))
    await db.commit()

    response = await client.get("/api/coverage/cycle-network", params=BBOX)

    by_network = {row["network"]: row for row in response.json()["networks"]}
    # Way 100 (1000m of the NCN's 1500m) was ridden; way 101 was not.
    assert by_network["ncn"]["ridden_m"] == pytest.approx(1000.0)
    assert by_network["ncn"]["total_m"] == pytest.approx(1500.0)
    # The RCN's only way was never ridden.
    assert by_network["rcn"]["ridden_m"] == pytest.approx(0.0)
    assert by_network["rcn"]["total_m"] == pytest.approx(800.0)


async def test_a_way_ridden_as_part_of_two_routes_credits_both(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Way 100 belongs to both routes below (an NCN and an RCN sharing one
    physical road, a common real shape) - riding it once must credit both
    routes' own tallies, not just one."""
    db.add_all(
        [
            _route(1, "ncn"),
            _member(1, 100, 1000.0),
            _route(2, "rcn"),
            _member(2, 100, 1000.0),
            _meta(2),
        ]
    )
    await db.commit()
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    db.add(ActivityWay(user_id=user_id, way_id=100, first_ridden_at=datetime.now(UTC)))
    await db.commit()

    response = await client.get("/api/coverage/cycle-network", params=BBOX)

    by_network = {row["network"]: row for row in response.json()["networks"]}
    assert by_network["ncn"]["ridden_m"] == pytest.approx(1000.0)
    assert by_network["rcn"]["ridden_m"] == pytest.approx(1000.0)


async def test_one_riders_coverage_never_counts_another_riders_ways(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """The worst bug this feature could ship - see the PR description for
    proof this bites: run once against a version of the coverage SQL with
    the activity_ways.user_id filter removed, every rider's ridden metres
    summed into every other rider's answer."""
    await _seed_network(db)
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)

    await register(client, "one@example.com")
    one_id = await _user_id(db, "one@example.com")
    db.add(ActivityWay(user_id=one_id, way_id=100, first_ridden_at=datetime.now(UTC)))
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    response = await client.get("/api/coverage/cycle-network", params=BBOX)

    by_network = {row["network"]: row for row in response.json()["networks"]}
    assert by_network["ncn"]["ridden_m"] == pytest.approx(0.0)


async def test_the_default_bbox_centres_on_the_riders_own_activities(
    client: AsyncClient, db: AsyncSession
) -> None:
    """No bbox given at all is what the /activities card uses, since it has
    no map to draw one from."""
    await _seed_network(db)
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")
    db.add(
        Activity(
            user_id=user_id,
            name="Near the network",
            distance_m=1000.0,
            ascent_m=0.0,
            descent_m=0.0,
            elevation=[],
            geom=f"SRID=4326;LINESTRING({LON} {LAT}, {LON + 0.001} {LAT + 0.001})",
        )
    )
    await db.commit()

    response = await client.get("/api/coverage/cycle-network")

    body = response.json()
    assert body["available"] is True
    assert {row["network"] for row in body["networks"]} == {"ncn", "rcn"}


async def test_no_bbox_and_no_activities_says_to_import_a_ride(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(_meta(0))
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/cycle-network")

    body = response.json()
    assert body["available"] is False
    assert "Import a ride" in body["reason"]


async def test_a_half_specified_bbox_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.get(
        "/api/coverage/cycle-network", params={"min_lat": LAT, "min_lon": LON}
    )

    assert response.status_code == 422


# --- /backfill ------------------------------------------------------------


async def test_backfill_needs_a_session(client: AsyncClient) -> None:
    response = await client.post("/api/coverage/backfill")
    assert response.status_code == 401


async def test_backfill_queues_a_job(client: AsyncClient) -> None:
    await register(client)

    response = await client.post("/api/coverage/backfill")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] in ("queued", "running", "done")

    status = await client.get(f"/api/coverage/backfill/{body['id']}")
    assert status.status_code == 200
    assert status.json()["id"] == body["id"]


async def test_polling_an_unknown_backfill_job_is_a_404(client: AsyncClient) -> None:
    await register(client)
    response = await client.get("/api/coverage/backfill/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_another_rider_cannot_poll_someone_elses_backfill_job(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    job = (await client.post("/api/coverage/backfill")).json()
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    response = await client.get(f"/api/coverage/backfill/{job['id']}")

    assert response.status_code == 404
