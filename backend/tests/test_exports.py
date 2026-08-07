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
from app.services.polyline import decode_polyline6, encode_polyline6

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


# Bearings and instructions taken verbatim from a real Valhalla response for a
# route through two Chilterns roundabouts.
ROUNDABOUT_ENTER = {
    "type": 26,
    "bearing_before": 341,
    "bearing_after": 285,
    "begin_shape_index": 1,
    "roundabout_exit_count": 3,
    "instruction": "Enter the roundabout and take the 3rd exit onto Bulbourne Road/B488.",
}
ROUNDABOUT_EXIT = {
    "type": 27,
    "bearing_before": 114,
    "bearing_after": 74,
    "begin_shape_index": 3,
    "begin_street_names": ["Bulbourne Road", "B488"],
    "street_names": ["B488"],
    "instruction": "Exit the roundabout onto Bulbourne Road/B488.",
}


def course_points_for(maneuvers: list[dict]) -> list[CoursePointMessage]:
    shape = [(53.7996 + i * 0.001, -1.5491) for i in range(6)]
    legs = [{"geometry": encode_polyline6(shape), "maneuvers": maneuvers}]
    elevation = [{"dist_m": 0.0, "elev_m": 50.0}, {"dist_m": 500.0, "elev_m": 60.0}]
    fit = build_fit("Roundabouts", legs, elevation, 600.0, BASE_TIME)
    return [
        record.message
        for record in FitFile.from_bytes(fit).records
        if isinstance(record.message, CoursePointMessage)
    ]


def test_roundabout_becomes_one_course_point_in_the_direction_travelled() -> None:
    points = course_points_for([ROUNDABOUT_ENTER, ROUNDABOUT_EXIT])
    # Entering on a bearing of 341 and leaving on 74 is a 93 degree right turn,
    # so the rider gets one right turn rather than two generic markers.
    assert len(points) == 1
    assert points[0].type == CoursePoint.RIGHT.value
    # Valhalla's own wording truncates to "Enter the roundabout and take th",
    # losing the exit number, so we build a name that fits FIT's 32 chars.
    assert points[0].course_point_name == "3rd exit onto Bulbourne Road"


def test_roundabout_straight_through_is_not_called_a_turn() -> None:
    points = course_points_for(
        [
            {**ROUNDABOUT_ENTER, "bearing_before": 90},
            {**ROUNDABOUT_EXIT, "bearing_after": 95},
        ]
    )
    assert [p.type for p in points] == [CoursePoint.STRAIGHT.value]


def test_roundabout_left_turn_uses_the_left_course_point() -> None:
    points = course_points_for(
        [
            {**ROUNDABOUT_ENTER, "bearing_before": 90},
            {**ROUNDABOUT_EXIT, "bearing_after": 0},
        ]
    )
    assert [p.type for p in points] == [CoursePoint.LEFT.value]


def test_roundabout_without_bearings_falls_back_to_generic() -> None:
    enter = {k: v for k, v in ROUNDABOUT_ENTER.items() if k != "bearing_before"}
    points = course_points_for([enter, ROUNDABOUT_EXIT])
    assert [p.type for p in points] == [CoursePoint.GENERIC.value]


def test_roundabout_exit_without_an_enter_still_produces_a_cue() -> None:
    # A leg can begin mid-roundabout, and dropping the only cue would leave
    # the rider with no prompt at all.
    points = course_points_for([ROUNDABOUT_EXIT])
    assert len(points) == 1
    assert points[0].course_point_name.startswith("Exit the roundabout")
