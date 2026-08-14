"""GET /api/activities/stats: yearly (and monthly-within-year) riding
totals - see the endpoint's own docstring in app/api/activities.py.

Activities are seeded directly through the `db` session with straight-line
geometry, the same technique test_route_match.py and test_climb_log.py use
for building exactly the shapes a query needs without parsing a real file.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity
from tests.conftest import register
from tests.test_route_match import _user_id

LAT, LON = 51.8000, -0.6500


def _line_wkt() -> str:
    return f"SRID=4326;LINESTRING({LON} {LAT}, {LON} {LAT + 0.01})"


def _activity(
    user_id: uuid.UUID,
    distance_m: float = 1000.0,
    moving_time_s: float | None = 200.0,
    ascent_m: float = 50.0,
    started_at: datetime | None = None,
) -> Activity:
    return Activity(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Test ride",
        started_at=started_at,
        moving_time_s=moving_time_s,
        elapsed_time_s=moving_time_s,
        distance_m=distance_m,
        ascent_m=ascent_m,
        descent_m=ascent_m,
        elevation=[],
        geom=_line_wkt(),
    )


async def _stats(client: AsyncClient) -> dict:
    response = await client.get("/api/activities/stats")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _year(body: dict, year: int | None) -> dict:
    matches = [row for row in body["years"] if row["year"] == year]
    assert len(matches) == 1, f"expected exactly one bucket for year={year}, got {matches}"
    return matches[0]


async def test_buckets_by_year_with_correct_sums(client: AsyncClient, db: AsyncSession) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    db.add_all(
        [
            _activity(
                user_id,
                distance_m=1000.0,
                moving_time_s=200.0,
                ascent_m=50.0,
                started_at=datetime(2024, 3, 1, tzinfo=UTC),
            ),
            _activity(
                user_id,
                distance_m=2000.0,
                moving_time_s=400.0,
                ascent_m=60.0,
                started_at=datetime(2024, 6, 1, tzinfo=UTC),
            ),
            _activity(
                user_id,
                distance_m=3000.0,
                moving_time_s=600.0,
                ascent_m=70.0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    body = await _stats(client)

    y2024 = _year(body, 2024)
    assert y2024["count"] == 2
    assert y2024["distance_m"] == pytest.approx(3000.0)
    assert y2024["moving_time_s"] == pytest.approx(600.0)
    assert y2024["ascent_m"] == pytest.approx(110.0)

    y2026 = _year(body, 2026)
    assert y2026["count"] == 1
    assert y2026["distance_m"] == pytest.approx(3000.0)
    assert y2026["moving_time_s"] == pytest.approx(600.0)
    assert y2026["ascent_m"] == pytest.approx(70.0)

    # Newest year first.
    years_present = [row["year"] for row in body["years"]]
    assert years_present.index(2026) < years_present.index(2024)


async def test_months_within_a_year_bucket_correctly(client: AsyncClient, db: AsyncSession) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    db.add_all(
        [
            _activity(
                user_id,
                distance_m=1000.0,
                moving_time_s=100.0,
                ascent_m=10.0,
                started_at=datetime(2026, 3, 5, tzinfo=UTC),
            ),
            _activity(
                user_id,
                distance_m=1500.0,
                moving_time_s=150.0,
                ascent_m=15.0,
                started_at=datetime(2026, 3, 20, tzinfo=UTC),
            ),
            _activity(
                user_id,
                distance_m=2000.0,
                moving_time_s=200.0,
                ascent_m=20.0,
                started_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    body = await _stats(client)
    y2026 = _year(body, 2026)
    months = {row["month"]: row for row in y2026["months"]}

    assert set(months) == {3, 7}
    assert months[3]["count"] == 2
    assert months[3]["distance_m"] == pytest.approx(2500.0)
    assert months[3]["moving_time_s"] == pytest.approx(250.0)
    assert months[3]["ascent_m"] == pytest.approx(25.0)
    assert months[7]["count"] == 1
    assert months[7]["distance_m"] == pytest.approx(2000.0)

    # Sorted ascending within the year.
    assert [row["month"] for row in y2026["months"]] == [3, 7]


async def test_undated_bucket_reconciles_with_the_unfiltered_total(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    db.add_all(
        [
            _activity(
                user_id,
                distance_m=1000.0,
                moving_time_s=100.0,
                ascent_m=10.0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            _activity(
                user_id,
                distance_m=500.0,
                moving_time_s=50.0,
                ascent_m=5.0,
                started_at=None,
            ),
            _activity(
                user_id,
                distance_m=250.0,
                moving_time_s=25.0,
                ascent_m=2.5,
                started_at=None,
            ),
        ]
    )
    await db.commit()

    body = await _stats(client)

    undated = _year(body, None)
    assert undated["count"] == 2
    assert undated["distance_m"] == pytest.approx(750.0)
    assert undated["moving_time_s"] == pytest.approx(75.0)
    assert undated["ascent_m"] == pytest.approx(7.5)
    assert undated["months"] == []

    # Undated bucket sorts last.
    assert body["years"][-1]["year"] is None

    # The reconciliation the brief requires: summing every bucket equals the
    # unfiltered total across all three seeded rides.
    total_count = sum(row["count"] for row in body["years"])
    total_distance = sum(row["distance_m"] for row in body["years"])
    total_moving_time = sum(row["moving_time_s"] for row in body["years"])
    total_ascent = sum(row["ascent_m"] for row in body["years"])
    assert total_count == 3
    assert total_distance == pytest.approx(1750.0)
    assert total_moving_time == pytest.approx(175.0)
    assert total_ascent == pytest.approx(17.5)


async def test_a_null_moving_time_does_not_corrupt_the_sum(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client, "rider@example.com")
    user_id = await _user_id(db, "rider@example.com")

    db.add_all(
        [
            _activity(
                user_id,
                distance_m=1000.0,
                moving_time_s=100.0,
                ascent_m=10.0,
                started_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            # No usable moving time at all - e.g. a file with no timestamps
            # (started_at is set here so this lands in the same year bucket
            # as the ride above; started_at and moving_time_s are nulled
            # independently on Activity).
            _activity(
                user_id,
                distance_m=500.0,
                moving_time_s=None,
                ascent_m=5.0,
                started_at=datetime(2026, 4, 2, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    body = await _stats(client)
    y2026 = _year(body, 2026)

    # SQL SUM skips NULLs: the bucket's moving_time_s is the one real value,
    # not NULL/None and not silently zero for the whole bucket.
    assert y2026["count"] == 2
    assert y2026["moving_time_s"] == pytest.approx(100.0)
    assert y2026["distance_m"] == pytest.approx(1500.0)


async def test_a_rider_with_no_activities_gets_an_empty_result(client: AsyncClient) -> None:
    await register(client, "rider@example.com")
    body = await _stats(client)
    assert body["years"] == []


async def test_another_users_totals_are_never_visible(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    db.add(
        _activity(
            owner_id,
            distance_m=5000.0,
            moving_time_s=500.0,
            ascent_m=100.0,
            started_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    body = await _stats(client)

    assert body["years"] == []


async def test_another_users_ride_in_the_same_month_never_inflates_your_own(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-user isolation test above only exercises a stranger with no
    rides of their own, where the year-level filter alone is enough to hide
    everything - it does not prove the month-level query is scoped too.
    Two users riding in the *same* year and month is the case that would
    catch a month query missing its own user_id filter: GROUP BY year, month
    with no user_id in the WHERE clause merges both riders' distances into
    one bucket, and the leak would still show up here even though every
    activity involved belongs to a real, matched year row for its owner."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    owner_id = await _user_id(db, "owner@example.com")
    db.add(
        _activity(
            owner_id,
            distance_m=1000.0,
            moving_time_s=100.0,
            ascent_m=10.0,
            started_at=datetime(2026, 5, 10, tzinfo=UTC),
        )
    )
    await db.commit()
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    stranger_id = await _user_id(db, "stranger@example.com")
    db.add(
        _activity(
            stranger_id,
            distance_m=9000.0,
            moving_time_s=900.0,
            ascent_m=90.0,
            started_at=datetime(2026, 5, 20, tzinfo=UTC),
        )
    )
    await db.commit()

    body = await _stats(client)
    y2026 = _year(body, 2026)
    assert y2026["count"] == 1
    assert y2026["distance_m"] == pytest.approx(9000.0)
    months = {row["month"]: row for row in y2026["months"]}
    assert months[5]["count"] == 1
    assert months[5]["distance_m"] == pytest.approx(9000.0)
    assert months[5]["moving_time_s"] == pytest.approx(900.0)
    assert months[5]["ascent_m"] == pytest.approx(90.0)
