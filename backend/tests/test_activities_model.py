"""The activities table and the service layer that fills it."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, User
from app.services.activities import (
    activity_from_track,
    name_from_filename,
    strava_activity_id,
    track_geometry,
)
from app.services.climbs import detect_climbs
from app.services.geo import destination_point
from app.services.importer import ImportedTrack, TrackPoint, elevation_profile, parse_route_file

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

GPX_NO_TIME = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="51.7955" lon="-0.6580"/>
    <trkpt lat="51.8000" lon="-0.6580"/>
  </trkseg></trk>
</gpx>
"""


def _climbing_track() -> ImportedTrack:
    """A synthetic 5 km @ 6% climb - the same shape test_climbs.py's own
    detection fixtures use - built as an ImportedTrack rather than parsed
    from a file, since GPX_RIDE above is far too short and shallow to
    detect anything (below climbs.py's own length/gain minimums)."""
    start = (51.8000, -0.6500)
    points = []
    dist, elev = 0.0, 100.0
    while dist <= 5000.0:
        lat, lon = destination_point(start, 0.0, dist)
        points.append(TrackPoint(lat=lat, lon=lon, ele=elev))
        dist += 100.0
        elev += 100.0 * 0.06
    return ImportedTrack(name="Climb", points=points, source_format="gpx")


def test_activity_from_track_stores_detected_climbs() -> None:
    """Both import paths - POST /api/activities/import
    (api/activities.py) and the Strava archive worker
    (services/activity_import.py) - build their Activity through this one
    function, so wiring detect_climbs in here rather than at either call
    site covers both at once. Checked against detect_climbs' own output
    over the same elevation profile, not a hand-picked category, so this
    breaks if the wiring - or the profile it is fed - ever diverges."""
    track = _climbing_track()
    activity = activity_from_track(track, user_id=uuid.uuid4(), fallback_name="ride")

    expected = [climb.model_dump() for climb in detect_climbs(elevation_profile(track))]
    assert expected, "fixture must actually produce a detectable climb"
    assert activity.climbs == expected


async def _user(db: AsyncSession, email: str = "rider@example.com") -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    await db.commit()
    return user


def test_builds_an_activity_from_a_recorded_track() -> None:
    track = parse_route_file("ride.gpx", GPX_RIDE)
    activity = activity_from_track(track, user_id=uuid.uuid4(), fallback_name="ride")

    assert activity.name == "Tuesday morning"
    assert activity.started_at == datetime(2026, 5, 3, 8, 0, 0, tzinfo=UTC)
    assert activity.elapsed_time_s == 120.0
    assert activity.moving_time_s == pytest.approx(120.0)
    assert activity.distance_m == pytest.approx(1000, rel=0.05)
    assert activity.ascent_m == pytest.approx(22.0, abs=0.5)
    assert activity.descent_m == pytest.approx(10.0, abs=0.5)
    assert activity.import_status == "done"


def test_a_track_without_timestamps_stores_nulls_not_zeroes() -> None:
    """Not knowing and never moving are different answers, stored differently."""
    track = parse_route_file("planned.gpx", GPX_NO_TIME)
    activity = activity_from_track(track, user_id=uuid.uuid4(), fallback_name="ride")

    assert activity.started_at is None
    assert activity.elapsed_time_s is None
    assert activity.moving_time_s is None
    assert activity.distance_m > 0


def test_falls_back_to_the_filename_when_the_file_names_nothing() -> None:
    track = parse_route_file("planned.gpx", GPX_NO_TIME)
    activity = activity_from_track(
        track, user_id=uuid.uuid4(), fallback_name=name_from_filename("2026-05-03_morning-ride.gpx")
    )
    assert activity.name == "2026 05 03 morning ride"


def test_strips_both_extensions_from_a_gzipped_export_name() -> None:
    assert name_from_filename("4821990112.fit.gz") == "4821990112"
    assert name_from_filename("activities/4821990112.gpx") == "4821990112"


def test_recognises_a_strava_export_filename() -> None:
    assert strava_activity_id("4821990112.fit.gz") == "4821990112"
    assert strava_activity_id("activities/4821990112.gpx") == "4821990112"
    assert strava_activity_id("my morning ride.gpx") is None
    assert strava_activity_id("12345.gpx") is None, "too short to be an activity id"


async def test_round_trips_through_postgis(db: AsyncSession) -> None:
    user = await _user(db)
    track = parse_route_file("ride.gpx", GPX_RIDE)
    db.add(activity_from_track(track, user_id=user.id, fallback_name="ride"))
    await db.commit()

    stored = (await db.execute(select(Activity))).scalar_one()
    assert stored.name == "Tuesday morning"
    assert len(stored.elevation) == 3

    # The geometry is a real linestring, readable back through PostGIS rather
    # than only asserted on the Python side.
    length = await db.scalar(
        select(func.ST_Length(func.ST_Transform(Activity.geom, 3857))).where(
            Activity.id == stored.id
        )
    )
    assert length is not None and length > 0


async def test_the_same_export_imported_twice_collides(db: AsyncSession) -> None:
    user = await _user(db)
    track = parse_route_file("ride.gpx", GPX_RIDE)
    for _ in range(2):
        db.add(
            activity_from_track(
                track,
                user_id=user.id,
                fallback_name="ride",
                source="strava_export",
                source_ref="4821990112",
            )
        )

    with pytest.raises(IntegrityError):
        await db.commit()


async def test_two_users_may_hold_the_same_source_ref(db: AsyncSession) -> None:
    """The uniqueness is per rider, not global - two people can export the
    same club ride, and neither should block the other."""
    one = await _user(db, "one@example.com")
    two = await _user(db, "two@example.com")
    track = parse_route_file("ride.gpx", GPX_RIDE)
    for user in (one, two):
        db.add(
            activity_from_track(
                track,
                user_id=user.id,
                fallback_name="ride",
                source="strava_export",
                source_ref="4821990112",
            )
        )
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(Activity)) == 2


async def test_rides_without_a_source_ref_do_not_collide(db: AsyncSession) -> None:
    """The unique index is partial. Without that, the second hand-uploaded
    file would be refused for having a null in common with the first."""
    user = await _user(db)
    track = parse_route_file("ride.gpx", GPX_RIDE)
    for _ in range(3):
        db.add(activity_from_track(track, user_id=user.id, fallback_name="ride"))
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(Activity)) == 3


async def test_deleting_a_user_takes_their_rides(db: AsyncSession) -> None:
    user = await _user(db)
    track = parse_route_file("ride.gpx", GPX_RIDE)
    db.add(activity_from_track(track, user_id=user.id, fallback_name="ride"))
    await db.commit()

    await db.delete(user)
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(Activity)) == 0


def test_geometry_is_lon_lat_not_lat_lon() -> None:
    """PostGIS takes x y. Getting this backwards puts every English ride in
    the Southern Ocean and still stores and indexes perfectly happily."""
    track = parse_route_file("ride.gpx", GPX_RIDE)
    wkt = str(track_geometry(track).data)
    assert wkt.startswith("LINESTRING(-0.658 51.7955")
