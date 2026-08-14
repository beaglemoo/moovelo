"""services/climb_log.py: the personal climb log, deduplicating the same
hill across many rides into one entry.

Activities are seeded directly through the session with straight-line
geometry and hand-built Climb payloads, the same technique
test_route_match.py uses for routes/activities - building the exact shapes
this module's clustering needs is easier as straight lines than as a real
parsed file. test_migration_0018 is the one exception: it runs the actual
Alembic migration against a scratch database, because a JSONB backfill's
correctness cannot be pinned any other way.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.config as app_config
from alembic import command
from app.models import Activity
from app.schemas import Climb
from app.services.climb_log import CLUSTER_DISTANCE_TOLERANCE_M, climb_log
from app.services.geo import Point, destination_point, haversine
from tests.conftest import ADMIN_URL, TEST_DB_HOST, register
from tests.test_climbs import _profile
from tests.test_route_match import _user_id

# Same Chilterns point test_route_match.py and test_coverage.py use.
LAT, LON = 51.8000, -0.6500


def _line_wkt(points: list[Point]) -> str:
    coords = ", ".join(f"{lon} {lat}" for lat, lon in points)
    return f"SRID=4326;LINESTRING({coords})"


def _climb(
    start_dist_m: float = 0.0,
    end_dist_m: float = 1000.0,
    length_m: float = 1000.0,
    gain_m: float = 60.0,
    avg_grade_pct: float = 6.0,
    max_grade_pct: float = 8.0,
    score: float = 75.0,
    category: str = "3",
) -> Climb:
    return Climb(
        start_dist_m=start_dist_m,
        end_dist_m=end_dist_m,
        length_m=length_m,
        gain_m=gain_m,
        avg_grade_pct=avg_grade_pct,
        max_grade_pct=max_grade_pct,
        score=score,
        category=category,
    )


def _activity(
    user_id: uuid.UUID,
    points: list[Point],
    distance_m: float,
    climbs: list[Climb],
    started_at: datetime | None = None,
    moving_time_s: float | None = None,
) -> Activity:
    return Activity(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Test ride",
        started_at=started_at,
        moving_time_s=moving_time_s,
        elapsed_time_s=moving_time_s,
        distance_m=distance_m,
        ascent_m=0.0,
        descent_m=0.0,
        elevation=[],
        geom=_line_wkt(points),
        climbs=[climb.model_dump() for climb in climbs],
    )


# --- clustering ---------------------------------------------------------


async def test_two_rides_over_the_same_hill_collapse_to_one_entry(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    points = [(LAT, LON), destination_point((LAT, LON), 0.0, 1000.0)]
    climb = _climb()

    first = _activity(
        user_id,
        points,
        1000.0,
        [climb],
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        moving_time_s=200.0,
    )
    second = _activity(
        user_id,
        points,
        1000.0,
        [climb],
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
        moving_time_s=220.0,
    )
    db.add_all([first, second])
    await db.commit()

    entries = await climb_log(db, user_id)

    assert len(entries) == 1
    assert entries[0].ascent_count == 2
    assert {ascent.activity_id for ascent in entries[0].ascents} == {first.id, second.id}
    # Oldest ascent first, matching the query's own ORDER BY.
    assert entries[0].ascents[0].activity_id == first.id
    # Proportional time estimate: the whole ride IS the climb here (1000m
    # climb over a 1000m ride), so the ascent time is exactly the ride's
    # own moving time.
    assert entries[0].ascents[0].time_s == pytest.approx(200.0)
    assert entries[0].ascents[1].time_s == pytest.approx(220.0)


async def test_two_different_hills_stay_separate(client: AsyncClient, db: AsyncSession) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    near_points = [(LAT, LON), destination_point((LAT, LON), 0.0, 1000.0)]
    # Comfortably over CLUSTER_DISTANCE_TOLERANCE_M so this is unambiguously
    # a different hill, not a boundary case.
    far_start = destination_point((LAT, LON), 90.0, CLUSTER_DISTANCE_TOLERANCE_M * 20)
    far_points = [far_start, destination_point(far_start, 0.0, 1000.0)]

    near = _activity(user_id, near_points, 1000.0, [_climb()])
    far = _activity(user_id, far_points, 1000.0, [_climb()])
    db.add_all([near, far])
    await db.commit()

    entries = await climb_log(db, user_id)

    assert len(entries) == 2
    assert all(entry.ascent_count == 1 for entry in entries)


async def test_similar_start_point_but_a_very_different_length_stays_separate(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Same start, wildly different length - a valley road climb and a
    short punchy ramp starting at the same junction are not "the same
    hill", even though CLUSTER_DISTANCE_TOLERANCE_M alone would merge
    them."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    points = [(LAT, LON), destination_point((LAT, LON), 0.0, 1000.0)]
    short = _activity(user_id, points, 1000.0, [_climb(length_m=300.0, end_dist_m=300.0)])
    long_ = _activity(user_id, points, 1000.0, [_climb(length_m=3000.0, end_dist_m=3000.0)])
    db.add_all([short, long_])
    await db.commit()

    entries = await climb_log(db, user_id)

    assert len(entries) == 2


# --- coordinate recovery -------------------------------------------------


async def test_recovered_coordinate_matches_the_expected_fraction(
    client: AsyncClient, db: AsyncSession
) -> None:
    """ST_LineInterpolatePoint on a single-segment line is a plain linear
    split of the two endpoint coordinates - computed independently here in
    Python and checked against a known value, not merely asserted non-null."""
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    start = (LAT, LON)
    end = (LAT + 0.01, LON + 0.02)
    distance_m = haversine(start, end)
    fraction = 0.3
    climb = _climb(
        start_dist_m=fraction * distance_m,
        end_dist_m=distance_m,
        length_m=(1 - fraction) * distance_m,
    )
    ride = _activity(user_id, [start, end], distance_m, [climb])
    db.add(ride)
    await db.commit()

    entries = await climb_log(db, user_id)

    expected_lat = start[0] + fraction * (end[0] - start[0])
    expected_lon = start[1] + fraction * (end[1] - start[1])
    assert len(entries) == 1
    assert entries[0].lat == pytest.approx(expected_lat, abs=1e-6)
    assert entries[0].lon == pytest.approx(expected_lon, abs=1e-6)


# --- isolation -------------------------------------------------------------


async def test_climb_log_never_crosses_users(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    one_id = await _user_id(db, "one@example.com")
    await register(client, "two@example.com")
    two_id = await _user_id(db, "two@example.com")

    points = [(LAT, LON), destination_point((LAT, LON), 0.0, 1000.0)]
    db.add(_activity(one_id, points, 1000.0, [_climb()]))
    db.add(_activity(two_id, points, 1000.0, [_climb()]))
    await db.commit()

    one_entries = await climb_log(db, one_id)
    two_entries = await climb_log(db, two_id)

    assert len(one_entries) == 1
    assert len(two_entries) == 1
    assert one_entries[0].ascents[0].activity_id != two_entries[0].ascents[0].activity_id


async def test_a_strangers_activity_is_excluded_even_though_it_matches_geometrically(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """Seeded directly through the session, not the API, at the identical
    location as the owner's own ride - nothing but the query's own
    `WHERE a.user_id = :user_id` keeps them apart. Sprint 1's PR3 shipped a
    user_id filter with 14 passing tests behind it that none of them
    actually exercised - deleting the filter left every one green (see
    CLAUDE.md). This pins the filter the way that gap should have been
    pinned from the start."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    await register(client, "stranger@example.com")
    stranger_id = await _user_id(db, "stranger@example.com")

    points = [(LAT, LON), destination_point((LAT, LON), 0.0, 1000.0)]
    climb = _climb()
    owner_ride = _activity(owner_id, points, 1000.0, [climb])
    stranger_ride = _activity(stranger_id, points, 1000.0, [climb])
    db.add_all([owner_ride, stranger_ride])
    await db.commit()

    entries = await climb_log(db, owner_id)

    assert len(entries) == 1
    assert entries[0].ascent_count == 1
    assert entries[0].ascents[0].activity_id == owner_ride.id


# --- migration 0018 --------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent


async def _create_scratch_db() -> str:
    admin_engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    dbname = f"bikegps_migration_{uuid.uuid4().hex[:8]}"
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    except Exception:
        await admin_engine.dispose()
        pytest.skip("test postgres not reachable on 127.0.0.1:5433 (run the dev compose stack)")
    await admin_engine.dispose()
    return dbname


async def _drop_scratch_db(dbname: str) -> None:
    admin_engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    await admin_engine.dispose()


async def _exec(dbname: str, sql: str, params: dict[str, object] | None = None) -> list:  # type: ignore[type-arg]
    engine = create_async_engine(f"postgresql+asyncpg://bikegps:bikegps@{TEST_DB_HOST}/{dbname}")
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        rows = result.fetchall() if result.returns_rows else []
    await engine.dispose()
    return list(rows)


def _migrate(dbname: str, revision: str) -> None:
    """Runs the real Alembic migration chain against `dbname`, up to and
    including `revision`. Deliberately a plain `def`, not `async def`: the
    generated env.py calls `asyncio.run(...)` itself
    (backend/alembic/env.py), which raises if invoked from inside an
    already-running event loop - a sync test function has none."""
    original = app_config.settings.database_url
    app_config.settings.database_url = (
        f"postgresql+asyncpg://bikegps:bikegps@{TEST_DB_HOST}/{dbname}"
    )
    try:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        command.upgrade(cfg, revision)
    finally:
        app_config.settings.database_url = original


def test_migration_0018_backfills_climbs_and_is_defensive_about_garbage() -> None:
    """Migration 0018's own backfill, run for real against a database built
    entirely by the Alembic chain (not create_all) - the release gate
    CLAUDE.md's "apply migrations to a copy of a populated database" rule
    describes. A pre-existing row with a real climbing profile gets its
    climbs populated; one whose `elevation` is garbage, or empty, gets `[]`
    rather than failing the whole migration.
    """
    dbname = asyncio.run(_create_scratch_db())
    try:
        _migrate(dbname, "0017")

        user_id = uuid.uuid4()
        asyncio.run(
            _exec(
                dbname,
                "INSERT INTO users (id, email, password_hash, is_admin) "
                "VALUES (:id, :email, 'x', false)",
                {"id": user_id, "email": "backfill@example.com"},
            )
        )

        # A real 5km @ 6% climb - the same fixture test_climbs.py's own
        # detection tests use, reused rather than re-derived here.
        climbing_elevation = json.dumps([point.model_dump() for point in _profile([(5000, 6.0)])])
        good_id, garbage_id, empty_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        insert_activity = (
            "INSERT INTO activities "
            "(id, user_id, name, distance_m, ascent_m, descent_m, elevation, geom) "
            "VALUES (:id, :user_id, 'ride', 1000, 0, 0, CAST(:elevation AS jsonb), "
            "ST_GeomFromText('SRID=4326;LINESTRING(-0.65 51.80, -0.64 51.81)'))"
        )
        asyncio.run(
            _exec(
                dbname,
                insert_activity,
                {"id": good_id, "user_id": user_id, "elevation": climbing_elevation},
            )
        )
        asyncio.run(
            _exec(
                dbname,
                insert_activity,
                {"id": garbage_id, "user_id": user_id, "elevation": '"not a list of points"'},
            )
        )
        asyncio.run(
            _exec(
                dbname,
                insert_activity,
                {"id": empty_id, "user_id": user_id, "elevation": "[]"},
            )
        )

        _migrate(dbname, "0018")

        rows = asyncio.run(_exec(dbname, "SELECT id, climbs FROM activities"))
        by_id = {row.id: row.climbs for row in rows}
        assert len(by_id) == 3
        assert len(by_id[good_id]) == 1
        assert by_id[good_id][0]["category"] == "2"
        assert by_id[garbage_id] == []
        assert by_id[empty_id] == []
    finally:
        asyncio.run(_drop_scratch_db(dbname))
