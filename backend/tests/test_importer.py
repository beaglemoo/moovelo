"""Parsing of uploaded GPX, TCX and FIT files."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import FileType, Manufacturer

from app.services.geo import cumulative_distances
from app.services.importer import (
    MAX_ELEVATION_SAMPLES,
    MAX_FILE_BYTES,
    RouteImportError,
    detect_format,
    elevation_profile,
    parse_route_file,
)
from app.services.valhalla import ascent_descent

GOLDEN_DIR = Path(__file__).parent / "golden"

_RIDE_START_MS = int(datetime(2026, 5, 3, 8, 0, 0, tzinfo=UTC).timestamp() * 1000)

GPX_TRACK = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Komoot" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>  Chilterns   loop </name></metadata>
  <trk>
    <name>Track name that loses to metadata</name>
    <trkseg>
      <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele></trkpt>
      <trkpt lat="51.7961" lon="-0.6572"><ele>131.5</ele></trkpt>
    </trkseg>
    <trkseg>
      <trkpt lat="51.7970" lon="-0.6559"><ele>140.0</ele></trkpt>
    </trkseg>
  </trk>
</gpx>
"""

# GPX 1.0 uses a different namespace, and route points instead of a track.
GPX_ROUTE_V10 = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0">
  <rte>
    <name>Planned route</name>
    <rtept lat="51.7955" lon="-0.6580"/>
    <rtept lat="51.7961" lon="-0.6572"/>
  </rte>
</gpx>
"""

# A recorded ride: one minute of riding, one standing at a junction, then a
# thirteen-minute hole where the recording was paused over lunch.
GPX_RIDE = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Wahoo" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Tuesday morning</name>
    <trkseg>
      <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele><time>2026-05-03T08:00:00Z</time></trkpt>
      <trkpt lat="51.8000" lon="-0.6580"><ele>131.5</ele><time>2026-05-03T08:01:00Z</time></trkpt>
      <trkpt lat="51.8000" lon="-0.6580"><ele>131.5</ele><time>2026-05-03T08:02:00Z</time></trkpt>
      <trkpt lat="51.8045" lon="-0.6580"><ele>140.0</ele><time>2026-05-03T08:15:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""

TCX_ACTIVITY = b"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Biking">
      <Id>2026-05-03T08:00:00Z</Id>
      <Lap>
        <Track>
          <Trackpoint>
            <Time>2026-05-03T08:00:00Z</Time>
            <Position>
              <LatitudeDegrees>51.8412</LatitudeDegrees>
              <LongitudeDegrees>-0.6047</LongitudeDegrees>
            </Position>
            <AltitudeMeters>210.4</AltitudeMeters>
          </Trackpoint>
          <Trackpoint>
            <Time>2026-05-03T08:00:30Z</Time>
            <Position>
              <LatitudeDegrees>51.8420</LatitudeDegrees>
              <LongitudeDegrees>-0.6039</LongitudeDegrees>
            </Position>
            <AltitudeMeters>223.1</AltitudeMeters>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""

TCX_COURSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Courses>
    <Course>
      <Name>Ivinghoe Beacon</Name>
      <Track>
        <Trackpoint>
          <Position>
            <LatitudeDegrees>51.8412</LatitudeDegrees>
            <LongitudeDegrees>-0.6047</LongitudeDegrees>
          </Position>
          <AltitudeMeters>210.4</AltitudeMeters>
        </Trackpoint>
        <Trackpoint>
          <Position>
            <LatitudeDegrees>51.8420</LatitudeDegrees>
            <LongitudeDegrees>-0.6039</LongitudeDegrees>
          </Position>
          <AltitudeMeters>223.1</AltitudeMeters>
        </Trackpoint>
        <Trackpoint>
          <Time>2026-01-15T09:00:00Z</Time>
        </Trackpoint>
      </Track>
    </Course>
  </Courses>
</TrainingCenterDatabase>
"""


def test_parses_gpx_track_across_segments() -> None:
    track = parse_route_file("ride.gpx", GPX_TRACK)
    assert track.source_format == "gpx"
    assert track.name == "Chilterns loop"  # whitespace collapsed, metadata wins
    assert track.shape == [(51.7955, -0.658), (51.7961, -0.6572), (51.797, -0.6559)]
    assert track.has_elevation
    assert track.points[1].ele == pytest.approx(131.5)


def test_parses_gpx_route_points_and_old_namespace() -> None:
    track = parse_route_file("planned.gpx", GPX_ROUTE_V10)
    assert track.name == "Planned route"
    assert len(track.points) == 2
    assert not track.has_elevation


def test_parses_tcx_course_skipping_points_without_position() -> None:
    track = parse_route_file("course.tcx", TCX_COURSE)
    assert track.source_format == "tcx"
    assert track.name == "Ivinghoe Beacon"
    assert len(track.points) == 2
    assert track.points[0].ele == pytest.approx(210.4)


def test_parses_fit_course() -> None:
    track = parse_route_file("route.fit", (GOLDEN_DIR / "route.fit").read_bytes())
    assert track.source_format == "fit"
    assert track.name == "Canal loop"
    assert len(track.points) == 5
    assert track.has_elevation
    # Round-trips the coordinates the golden course was built from.
    assert track.points[0].lat == pytest.approx(53.7996, abs=1e-4)
    assert track.points[0].ele == pytest.approx(55.0, abs=0.5)


def test_detects_format_from_content_when_the_name_is_useless() -> None:
    assert detect_format("upload", GPX_TRACK) == "gpx"
    assert detect_format("upload.txt", TCX_COURSE) == "tcx"
    assert detect_format("upload", (GOLDEN_DIR / "route.fit").read_bytes()) == "fit"
    # A wrong-but-supported extension still loses to nothing; the name wins.
    assert detect_format("actually-a-gpx.gpx", GPX_TRACK) == "gpx"


def test_drops_implausible_points() -> None:
    gpx = b"""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
      <trkpt lat="0.0" lon="0.0"/>
      <trkpt lat="51.7955" lon="-0.6580"/>
      <trkpt lat="91.0" lon="-0.6572"/>
      <trkpt lat="51.7961" lon="-0.6572"/>
      <trkpt lat="not-a-number" lon="-0.6559"/>
    </trkseg></trk></gpx>"""
    track = parse_route_file("messy.gpx", gpx)
    assert track.shape == [(51.7955, -0.658), (51.7961, -0.6572)]


def test_partial_elevation_is_not_treated_as_elevation() -> None:
    gpx = b"""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
      <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele></trkpt>
      <trkpt lat="51.7961" lon="-0.6572"/>
    </trkseg></trk></gpx>"""
    assert not parse_route_file("partial.gpx", gpx).has_elevation


def test_nonfinite_elevation_is_dropped_not_stored() -> None:
    """A real device/export glitch: NaN and +/-inf both pass float() and
    Pydantic without complaint, but the JSON spec has no token for either,
    and the elevation profile is stored as JSONB - an unfiltered NaN takes
    down the whole INSERT (and, for a bulk import, every other ride batched
    into the same statement) rather than just this one point. The point
    itself is still a real GPS fix, so it is kept - only the elevation is
    dropped."""
    gpx = b"""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
      <trkpt lat="51.7955" lon="-0.6580"><ele>NaN</ele></trkpt>
      <trkpt lat="51.7961" lon="-0.6572"><ele>INF</ele></trkpt>
      <trkpt lat="51.7970" lon="-0.6559"><ele>-INF</ele></trkpt>
    </trkseg></trk></gpx>"""
    track = parse_route_file("nan.gpx", gpx)
    assert len(track.points) == 3, "the points themselves are still real GPS fixes"
    assert [p.ele for p in track.points] == [None, None, None]
    assert not track.has_elevation


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("empty.gpx", b"", "empty"),
        ("huge.gpx", b"x" * (MAX_FILE_BYTES + 1), "larger than"),
        ("notes.pdf", b"%PDF-1.7 not a route", "Unsupported file type"),
        ("broken.gpx", b"<gpx><trk><trkseg>", "not valid XML"),
        ("empty-track.gpx", b'<gpx version="1.1"><trk><trkseg/></trk></gpx>', "No usable track"),
        ("bad.fit", b"\x00" * 8 + b".FIT" + b"\x00" * 32, "not a readable FIT"),
    ],
)
def test_rejects_bad_files_with_a_usable_message(filename: str, data: bytes, message: str) -> None:
    with pytest.raises(RouteImportError, match=message):
        parse_route_file(filename, data)


def test_elevation_profile_from_the_files_own_points() -> None:
    track = parse_route_file("ride.gpx", GPX_TRACK)
    profile = elevation_profile(track)
    assert [p.elev_m for p in profile] == [128.0, 131.5, 140.0]
    assert profile[0].dist_m == 0.0
    assert profile[-1].dist_m > 0
    assert ascent_descent(profile) == (pytest.approx(12.0), pytest.approx(0.0))


def test_elevation_profile_is_scaled_onto_the_matched_distance() -> None:
    track = parse_route_file("ride.gpx", GPX_TRACK)
    raw = elevation_profile(track)
    # Map matching moves the line onto roads, so the matched route is a
    # different length and the profile has to stretch to match it.
    scaled = elevation_profile(track, total_distance_m=raw[-1].dist_m * 2)
    assert scaled[-1].dist_m == pytest.approx(raw[-1].dist_m * 2)
    assert [p.elev_m for p in scaled] == [p.elev_m for p in raw]


def test_elevation_profile_uses_only_the_points_that_have_elevation() -> None:
    gpx = b"""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
      <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele></trkpt>
      <trkpt lat="51.7961" lon="-0.6572"/>
      <trkpt lat="51.7970" lon="-0.6559"><ele>140.0</ele></trkpt>
    </trkseg></trk></gpx>"""
    profile = elevation_profile(parse_route_file("partial.gpx", gpx))
    assert [p.elev_m for p in profile] == [128.0, 140.0]


def test_elevation_profile_is_empty_without_elevation() -> None:
    assert elevation_profile(parse_route_file("planned.gpx", GPX_ROUTE_V10)) == []


def test_elevation_profile_is_capped() -> None:
    points = "".join(
        f'<trkpt lat="{51.79 + i * 0.0001:.6f}" lon="-0.658"><ele>{100 + i % 7}</ele></trkpt>'
        for i in range(1500)
    )
    gpx = f'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{points}</trkseg></trk></gpx>'
    track = parse_route_file("long.gpx", gpx.encode())
    profile = elevation_profile(track)
    assert len(profile) == MAX_ELEVATION_SAMPLES
    assert profile[0].dist_m == 0.0
    assert profile[-1].dist_m == pytest.approx(cumulative_distances(track.shape)[-1])


# --- Timestamps and the course/activity distinction -------------------------


def test_reads_gpx_timestamps() -> None:
    track = parse_route_file("ride.gpx", GPX_RIDE)
    assert track.started_at == datetime(2026, 5, 3, 8, 0, 0, tzinfo=UTC)
    assert track.ended_at == datetime(2026, 5, 3, 8, 15, 0, tzinfo=UTC)
    assert track.elapsed_time_s == 900.0
    assert track.is_activity


def test_a_track_without_timestamps_is_not_an_activity() -> None:
    track = parse_route_file("planned.gpx", GPX_ROUTE_V10)
    assert track.started_at is None
    assert track.ended_at is None
    assert track.elapsed_time_s is None
    assert track.moving_time_s is None
    assert not track.is_activity


def test_moving_time_excludes_a_stop_and_a_paused_recording() -> None:
    """Fifteen minutes elapsed, one of them actually spent riding.

    08:00-08:01 covers 500 m, so it counts. 08:01-08:02 does not move at all -
    standing at a junction. 08:02-08:15 is a thirteen-minute jump, which is a
    paused recording rather than a very slow quarter hour, and counting it
    would turn every lunch stop into ride time.
    """
    track = parse_route_file("ride.gpx", GPX_RIDE)
    assert track.elapsed_time_s == 900.0
    assert track.moving_time_s == pytest.approx(60.0)


def test_unreadable_and_implausible_timestamps_are_dropped_not_fatal() -> None:
    gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
  <trkpt lat="51.7955" lon="-0.6580"><time>not a date</time></trkpt>
  <trkpt lat="51.7961" lon="-0.6572"><time>1904-01-01T00:00:00Z</time></trkpt>
  <trkpt lat="51.7970" lon="-0.6559"><time>2026-05-03T08:00:00Z</time></trkpt>
</trkseg></trk></gpx>"""
    track = parse_route_file("ride.gpx", gpx)
    assert len(track.points) == 3
    assert [p.time for p in track.points[:2]] == [None, None]
    assert track.started_at == datetime(2026, 5, 3, 8, 0, 0, tzinfo=UTC)


def test_naive_timestamps_are_read_as_utc() -> None:
    gpx = GPX_RIDE.replace(b"2026-05-03T08:00:00Z", b"2026-05-03T08:00:00")
    track = parse_route_file("ride.gpx", gpx)
    assert track.started_at == datetime(2026, 5, 3, 8, 0, 0, tzinfo=UTC)


def test_tcx_declares_which_it_is() -> None:
    assert parse_route_file("course.tcx", TCX_COURSE).declared_kind == "course"
    assert parse_route_file("ride.tcx", TCX_ACTIVITY).declared_kind == "activity"
    assert parse_route_file("ride.tcx", TCX_ACTIVITY).is_activity


def test_our_own_course_export_is_not_mistaken_for_a_ride() -> None:
    """The regression this whole discriminator exists for.

    services/fit.py stamps every RecordMessage with a time derived from the
    routing duration, so a course Moovelo exported carries a full set of
    timestamps. Judging by timestamps alone would file it as a ride someone
    went out and did.
    """
    track = parse_route_file("route.fit", (GOLDEN_DIR / "route.fit").read_bytes())
    assert track.started_at is not None, "the golden course does carry timestamps"
    assert track.declared_kind == "course"
    assert not track.is_activity


def test_fit_activity_is_recognised_from_its_file_id() -> None:
    track = parse_route_file("ride.fit", _fit_activity())
    assert track.declared_kind == "activity"
    assert track.is_activity
    assert track.started_at == datetime(2026, 5, 3, 8, 0, 0, tzinfo=UTC)
    assert track.ended_at == datetime(2026, 5, 3, 8, 0, 20, tzinfo=UTC)


def _fit_activity() -> bytes:
    """A minimal FIT activity.

    Synthesised rather than captured, unusually for this suite. What is being
    tested is our reading of one standardised enum in file_id, not tolerance
    of a vendor's quirks, and no real ride file is in the repo to capture
    from. A head unit's own export is still worth exercising on staging.
    """
    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 0x1234
    file_id.time_created = _RIDE_START_MS
    builder.add(file_id)

    for i in range(3):
        record = RecordMessage()
        record.timestamp = _RIDE_START_MS + i * 10_000
        record.position_lat = 51.7955 + i * 0.001
        record.position_long = -0.6580 + i * 0.001
        record.altitude = 120.0 + i
        builder.add(record)

    data: bytes = builder.build().to_bytes()
    return data
