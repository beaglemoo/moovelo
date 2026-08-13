"""The activities endpoints: import, list, read, delete."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity
from app.services.polyline import decode_polyline6
from tests.conftest import register

GPX_RIDE = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Tuesday morning</name>
    <trkseg>
      <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele><time>2026-05-03T08:00:00Z</time></trkpt>
      <trkpt lat="51.8000" lon="-0.6580"><ele>150.0</ele><time>2026-05-03T08:01:00Z</time></trkpt>
      <trkpt lat="51.8045" lon="-0.6580"><ele>140.0</ele><time>2026-05-03T08:02:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""

GPX_OLDER = GPX_RIDE.replace(b"2026-05-03", b"2024-09-14").replace(
    b"Tuesday morning", b"An older ride"
)

GPX_UNDATED = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>No clock</name><trkseg>
    <trkpt lat="51.7955" lon="-0.6580"/>
    <trkpt lat="51.8000" lon="-0.6580"/>
  </trkseg></trk>
</gpx>
"""

GPX_UNDATED_2 = GPX_UNDATED.replace(b"No clock", b"No clock too")


async def _import(client: AsyncClient, data: bytes, filename: str = "ride.gpx") -> dict:
    response = await client.post(
        "/api/activities/import", files={"file": (filename, data, "application/gpx+xml")}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_imports_a_ride(client: AsyncClient) -> None:
    await register(client)
    body = await _import(client, GPX_RIDE)

    assert body["name"] == "Tuesday morning"
    assert body["started_at"].startswith("2026-05-03T08:00:00")
    assert body["elapsed_time_s"] == 120.0
    assert body["distance_m"] == pytest.approx(1000, rel=0.05)
    assert len(body["elevation"]) == 3


async def test_the_returned_shape_is_the_recorded_trace(client: AsyncClient) -> None:
    """Encoded as polyline6, so an activity draws through the same path the
    planner already uses for route legs - and in lat/lon order, not the
    lon/lat PostGIS stores."""
    await register(client)
    body = await _import(client, GPX_RIDE)

    coords = decode_polyline6(body["shape"])
    assert len(coords) == 3
    assert coords[0][0] == pytest.approx(51.7955, abs=1e-5)
    assert coords[0][1] == pytest.approx(-0.6580, abs=1e-5)


async def test_a_file_that_will_not_parse_is_a_422(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/activities/import", files={"file": ("ride.gpx", b"<gpx></gpx>", "text/xml")}
    )
    assert response.status_code == 422
    assert "track points" in response.json()["detail"]


async def test_import_requires_a_session(client: AsyncClient) -> None:
    response = await client.post(
        "/api/activities/import", files={"file": ("ride.gpx", GPX_RIDE, "text/xml")}
    )
    assert response.status_code == 401


async def test_lists_newest_first_and_dates_undated_rides_by_import(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Two undated rides, and their created_at values deliberately set to
    contradict the order they were imported in.

    A single undated row cannot prove this endpoint uses
    coalesce(started_at, created_at) rather than having quietly lost it:
    Postgres's own default NULLS FIRST on a bare `started_at DESC` already
    puts one undated row first, no coalesce required. A second undated row
    does not fix that on its own either - for freshly inserted, never
    updated rows, created_at *is* insertion order, so relying on whatever
    incidental tie-break Postgres gives untied NULLs (measured: it happens
    to already match insertion order here) would pass a two-undated-row
    test just as easily as a real coalesce would, for the wrong reason.
    Only forcing created_at to disagree with insertion order - and with the
    interleaved dated rows either side of it - actually distinguishes the
    two: the coalesced order below is impossible to produce by accident.
    """
    await register(client)
    await _import(client, GPX_OLDER)  # started_at 2024-09-14
    first = await _import(client, GPX_UNDATED)  # inserted first
    await _import(client, GPX_RIDE)  # started_at 2026-05-03
    second = await _import(client, GPX_UNDATED_2)  # inserted second

    # Inserted first, but given a created_at *after* everything else -
    # coalesce must place it first regardless of insertion order.
    await db.execute(
        update(Activity)
        .where(Activity.id == uuid.UUID(first["id"]))
        .values(created_at=datetime(2026, 7, 1, tzinfo=UTC))
    )
    # Inserted second, but given a created_at *before* everything else -
    # coalesce must sink it to the bottom, the opposite of insertion order.
    await db.execute(
        update(Activity)
        .where(Activity.id == uuid.UUID(second["id"]))
        .values(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    )
    await db.commit()

    names = [row["name"] for row in (await client.get("/api/activities")).json()]
    assert names == ["No clock", "Tuesday morning", "An older ride", "No clock too"]


async def test_filters_by_year(client: AsyncClient) -> None:
    await register(client)
    await _import(client, GPX_OLDER)
    await _import(client, GPX_RIDE)

    rows = (await client.get("/api/activities", params={"year": 2024})).json()
    assert [row["name"] for row in rows] == ["An older ride"]


async def test_filters_by_source(client: AsyncClient) -> None:
    await register(client)
    await _import(client, GPX_RIDE)

    assert len((await client.get("/api/activities", params={"source": "file"})).json()) == 1
    assert (
        len((await client.get("/api/activities", params={"source": "strava_export"})).json()) == 0
    )


async def test_deletes_a_ride(client: AsyncClient) -> None:
    await register(client)
    body = await _import(client, GPX_RIDE)

    assert (await client.delete(f"/api/activities/{body['id']}")).status_code == 204
    assert (await client.get("/api/activities")).json() == []
    assert (await client.get(f"/api/activities/{body['id']}")).status_code == 404


async def test_deleting_a_ride_schedules_a_coverage_rederive(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """activity_ways has no per-activity link, so a deleted ride's coverage
    credit cannot be decremented in place. Deleting a ride schedules a full
    re-derive (clear + rebuild) as a clear_first job on the match-queue worker,
    NOT on the request session: doing the clear on a second session deadlocked
    against the worker and left phantom over-credits. The rebuild correctness
    is covered in test_way_matching; here we pin that the delete returns 204
    and schedules exactly that job."""
    import uuid as uuidlib

    from app.services import way_matching

    calls: list[tuple[uuidlib.UUID, bool]] = []
    real_submit = way_matching.queue.submit

    def spy(user_id, activity_ids=None, clear_first=False):  # type: ignore[no-untyped-def]
        calls.append((user_id, clear_first))
        return real_submit(user_id, activity_ids, clear_first)

    monkeypatch.setattr(way_matching.queue, "submit", spy)

    await register(client)
    body = await _import(client, GPX_RIDE)
    activity_id = uuid.UUID(body["id"])
    user_id = await db.scalar(select(Activity.user_id).where(Activity.id == activity_id))

    assert (await client.delete(f"/api/activities/{activity_id}")).status_code == 204

    # A clear_first re-derive was scheduled for this rider (the import's own
    # match submit, clear_first=False, is also in `calls` and is not it).
    assert (user_id, True) in calls


async def test_one_riders_activities_are_invisible_to_another(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst bug this feature could ship, so it is asserted rather than
    reviewed - on the list, on the read, and on the delete."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    mine = await _import(client, GPX_RIDE)
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    assert (await client.get("/api/activities")).json() == []
    assert (await client.get(f"/api/activities/{mine['id']}")).status_code == 404
    assert (await client.delete(f"/api/activities/{mine['id']}")).status_code == 404

    # And the first rider still has theirs, which the 404 above could have
    # been hiding a successful delete behind.
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"email": "one@example.com", "password": "correct-horse-9"}
    )
    assert [row["id"] for row in (await client.get("/api/activities")).json()] == [mine["id"]]
