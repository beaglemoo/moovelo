"""Golden-file and decode-back tests for GPX and FIT export."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.course_point_message import CoursePointMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import CoursePoint

from app.schemas import RouteResponse
from app.services.fit import build_fit
from app.services.geo import concat_shapes
from app.services.gpx import build_gpx, interpolate_elevations
from app.services.polyline import decode_polyline6

GOLDEN_DIR = Path(__file__).parent / "golden"
BASE_TIME = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


def build_outputs(snapshot: RouteResponse) -> tuple[bytes, bytes]:
    shape = concat_shapes([decode_polyline6(leg.geometry) for leg in snapshot.legs])
    gpx = build_gpx("Canal loop", shape, snapshot.elevation)
    fit = build_fit(
        "Canal loop",
        [leg.model_dump() for leg in snapshot.legs],
        [p.model_dump() for p in snapshot.elevation],
        snapshot.duration_s,
        BASE_TIME,
    )
    return gpx, fit


def test_gpx_matches_golden(snapshot: RouteResponse) -> None:
    gpx, _ = build_outputs(snapshot)
    assert gpx == (GOLDEN_DIR / "route.gpx").read_bytes()


def test_fit_matches_golden(snapshot: RouteResponse) -> None:
    _, fit = build_outputs(snapshot)
    assert fit == (GOLDEN_DIR / "route.fit").read_bytes()


def test_fit_decodes_with_expected_course_points(snapshot: RouteResponse) -> None:
    _, fit = build_outputs(snapshot)
    decoded = FitFile.from_bytes(fit)
    course_points = [
        record.message
        for record in decoded.records
        if isinstance(record.message, CoursePointMessage)
    ]
    # Start maneuvers are skipped; destination of leg 1, left turn, continue,
    # right turn, and final destination remain.
    assert [cp.type for cp in course_points] == [
        CoursePoint.LEFT.value,
        CoursePoint.GENERIC.value,
        CoursePoint.STRAIGHT.value,
        CoursePoint.RIGHT.value,
        CoursePoint.GENERIC.value,
    ]
    assert course_points[0].course_point_name == "Turn left onto Canal Towpath."
    distances = [cp.distance for cp in course_points]
    assert distances == sorted(distances)
    assert distances[-1] == pytest.approx(2575, abs=15)

    records = [
        record.message for record in decoded.records if isinstance(record.message, RecordMessage)
    ]
    assert len(records) == 5  # merged shape vertices (leg boundary deduplicated)
    assert records[0].altitude == pytest.approx(55.0, abs=0.5)


def test_elevation_interpolation(snapshot: RouteResponse) -> None:
    shape = concat_shapes([decode_polyline6(leg.geometry) for leg in snapshot.legs])
    elevations = interpolate_elevations(shape, snapshot.elevation)
    assert len(elevations) == len(shape)
    assert elevations[0] == pytest.approx(55.0)
    valid = [e for e in elevations if e is not None]
    assert all(55.0 <= e <= 70.0 for e in valid)


def test_gpx_without_elevation(snapshot: RouteResponse) -> None:
    shape = concat_shapes([decode_polyline6(leg.geometry) for leg in snapshot.legs])
    gpx = build_gpx("No elevation", shape, [])
    assert b"<ele>" not in gpx
    assert gpx.count(b"<trkpt") == len(shape)
