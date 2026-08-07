"""Parsing of uploaded GPX, TCX and FIT files."""

from pathlib import Path

import pytest

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
