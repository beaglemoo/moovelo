"""FIT course file export with Valhalla maneuvers as course points."""

from datetime import datetime
from typing import Any

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.course_message import CourseMessage
from fit_tool.profile.messages.course_point_message import CoursePointMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import (
    CoursePoint,
    Event,
    EventType,
    FileType,
    Manufacturer,
    Sport,
)

from app.schemas import ElevationPoint, RouteLeg
from app.services.geo import Point, cumulative_distances
from app.services.gpx import interpolate_elevations
from app.services.polyline import decode_polyline6

COURSE_POINT_NAME_MAX = 32

# Valhalla maneuver type -> FIT course point type. Unlisted types fall back to
# GENERIC; start maneuvers (0-3) produce no course point at all.
MANEUVER_TYPE_MAP: dict[int, CoursePoint] = {
    4: CoursePoint.GENERIC,  # destination - see DESTINATION_MANEUVER_TYPES
    5: CoursePoint.GENERIC,
    6: CoursePoint.GENERIC,
    7: CoursePoint.STRAIGHT,  # becomes
    8: CoursePoint.STRAIGHT,  # continue
    9: CoursePoint.SLIGHT_RIGHT,
    10: CoursePoint.RIGHT,
    11: CoursePoint.SHARP_RIGHT,
    12: CoursePoint.U_TURN,
    13: CoursePoint.U_TURN,
    14: CoursePoint.SHARP_LEFT,
    15: CoursePoint.LEFT,
    16: CoursePoint.SLIGHT_LEFT,
    17: CoursePoint.STRAIGHT,  # ramp straight
    18: CoursePoint.RIGHT,  # ramp right
    19: CoursePoint.LEFT,  # ramp left
    20: CoursePoint.RIGHT_FORK,  # exit right
    21: CoursePoint.LEFT_FORK,  # exit left
    22: CoursePoint.STRAIGHT,  # stay straight
    23: CoursePoint.RIGHT_FORK,  # stay right
    24: CoursePoint.LEFT_FORK,  # stay left
    25: CoursePoint.STRAIGHT,  # merge
    37: CoursePoint.RIGHT,  # merge right
    38: CoursePoint.LEFT,  # merge left
}

SKIPPED_MANEUVER_TYPES = {0, 1, 2, 3}

ROUNDABOUT_ENTER = 26
ROUNDABOUT_EXIT = 27


def _bearing_delta(before: Any, after: Any) -> float | None:
    """Heading change in degrees, positive for a right turn."""
    if before is None or after is None:
        return None
    return ((float(after) - float(before) + 180.0) % 360.0) - 180.0


def _turn_point(delta: float) -> CoursePoint:
    magnitude = abs(delta)
    if magnitude < 20:
        return CoursePoint.STRAIGHT
    if magnitude < 45:
        return CoursePoint.SLIGHT_RIGHT if delta > 0 else CoursePoint.SLIGHT_LEFT
    if magnitude < 135:
        return CoursePoint.RIGHT if delta > 0 else CoursePoint.LEFT
    return CoursePoint.SHARP_RIGHT if delta > 0 else CoursePoint.SHARP_LEFT


def _roundabout_point(enter: dict[str, Any], exit_maneuver: dict[str, Any] | None) -> CoursePoint:
    """The direction actually travelled through a roundabout.

    FIT has no roundabout course point. The FIT profile does model
    roundabouts - in its `turn_type` enum - but no course-file message
    carries that field, so a course can only say "turn right here". Valhalla
    splits a roundabout into an enter and an exit maneuver; comparing the
    heading going in with the heading coming out gives the turn a rider
    actually makes, which beats emitting two featureless generic points.
    """
    source = exit_maneuver if exit_maneuver is not None else enter
    delta = _bearing_delta(enter.get("bearing_before"), source.get("bearing_after"))
    return CoursePoint.GENERIC if delta is None else _turn_point(delta)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _roundabout_name(enter: dict[str, Any], exit_maneuver: dict[str, Any] | None) -> str:
    """A name for a roundabout that survives the 32-character FIT limit.

    Valhalla's own instruction ("Enter the roundabout and take the 3rd exit
    onto Bulbourne Road/B488.") truncates to "Enter the roundabout and take th"
    - losing the exit number, which is the one thing the rider needs.
    """
    count = enter.get("roundabout_exit_count")
    if not count:
        return str(enter.get("instruction", ""))
    streets = (exit_maneuver or {}).get("begin_street_names") or (exit_maneuver or {}).get(
        "street_names"
    )
    exit_label = f"{_ordinal(int(count))} exit"
    return f"{exit_label} onto {streets[0]}" if streets else exit_label


def _leg_offsets(shapes: list[list[Point]]) -> list[int]:
    """Index in the merged shape where each leg starts.

    Mirrors how build_fit concatenates the legs: a boundary vertex is only
    dropped when the two endpoints are identical, which independently matched
    trace chunks are not guaranteed to be.
    """
    offsets: list[int] = []
    length = 0
    previous_end: Point | None = None
    for shape in shapes:
        shares_vertex = previous_end is not None and bool(shape) and shape[0] == previous_end
        offsets.append(length - 1 if shares_vertex else length)
        length += len(shape) - 1 if shares_vertex else len(shape)
        if shape:
            previous_end = shape[-1]
    return [max(offset, 0) for offset in offsets]


def _paired_exit(
    flat: list[tuple[dict[str, Any], int]], enter_position: int
) -> dict[str, Any] | None:
    """The exit belonging to the roundabout entered at `enter_position`.

    Bounded by the next roundabout: if this one's exit is missing - a trace
    chunk can end mid-roundabout - folding against a later roundabout's exit
    would name the wrong road and give the wrong direction.
    """
    for maneuver, _ in flat[enter_position + 1 :]:
        m_type = int(maneuver.get("type", 0))
        if m_type == ROUNDABOUT_EXIT:
            return maneuver
        if m_type == ROUNDABOUT_ENTER:
            return None
    return None


def _folded_roundabout_exits(flat: list[tuple[dict[str, Any], int]]) -> set[int]:
    """Positions of exits already accounted for by their enter maneuver."""
    folded: set[int] = set()
    for position, (maneuver, _) in enumerate(flat):
        if int(maneuver.get("type", 0)) != ROUNDABOUT_ENTER:
            continue
        for offset in range(position + 1, len(flat)):
            m_type = int(flat[offset][0].get("type", 0))
            if m_type == ROUNDABOUT_EXIT:
                folded.add(offset)
                break
            if m_type == ROUNDABOUT_ENTER:
                break
    return folded


# Valhalla ends EVERY leg with one of these, not just the last: a route with
# two via points carries three "you have arrived". On a course file that means
# the head unit announces the destination partway through the ride. Only the
# final leg's arrival is real.
DESTINATION_MANEUVER_TYPES = {4, 5, 6}


def _course_points(
    legs: list[RouteLeg],
    shapes: list[list[Point]],
    merged: list[Point],
    dists: list[float],
    duration_s: float,
    total_dist: float,
    base_ms: int,
) -> list[CoursePointMessage]:
    messages: list[CoursePointMessage] = []
    # Flattened first, because a roundabout can straddle a leg boundary when a
    # waypoint sits on it: the enter maneuver ends one leg and the exit starts
    # the next. Folding per-leg would miss those and emit two vague cues.
    flat: list[tuple[dict[str, Any], int]] = []
    # Which leg each entry came from, parallel to `flat` rather than part of
    # it: the roundabout folding below navigates by position and must not
    # have its tuple shape changed underneath it.
    leg_of: list[int] = []
    for leg_index, (offset, leg) in enumerate(zip(_leg_offsets(shapes), legs, strict=True)):
        for maneuver in leg.maneuvers:
            begin_index: int = maneuver.get("begin_shape_index", 0)
            flat.append((maneuver, min(offset + begin_index, len(merged) - 1)))
            leg_of.append(leg_index)
    last_leg = len(legs) - 1

    # Identified by position rather than a running flag: Valhalla puts a
    # destination maneuver between the two halves of a roundabout that a
    # waypoint sits on, and any flag would be cleared by it.
    folded_exits = _folded_roundabout_exits(flat)
    for position, (maneuver, index) in enumerate(flat):
        m_type = int(maneuver.get("type", 0))
        if m_type in SKIPPED_MANEUVER_TYPES or position in folded_exits:
            continue
        if m_type in DESTINATION_MANEUVER_TYPES and leg_of[position] != last_leg:
            # An arrival at a via point is not an arrival. Dropped rather than
            # retyped: there is nothing useful to tell the rider here, and the
            # next real turn cue follows within metres.
            continue
        if m_type == ROUNDABOUT_ENTER:
            exit_maneuver = _paired_exit(flat, position)
            point_type = _roundabout_point(maneuver, exit_maneuver)
            label = _roundabout_name(maneuver, exit_maneuver)
        else:
            point_type = MANEUVER_TYPE_MAP.get(m_type, CoursePoint.GENERIC)
            label = str(maneuver.get("instruction", ""))
        point = CoursePointMessage()
        point.timestamp = base_ms + _time_offset_ms(dists[index], total_dist, duration_s)
        point.position_lat = merged[index][0]
        point.position_long = merged[index][1]
        point.distance = dists[index]
        point.type = point_type
        point.course_point_name = label[:COURSE_POINT_NAME_MAX]
        messages.append(point)
    return messages


def _time_offset_ms(dist: float, total_dist: float, duration_s: float) -> int:
    fraction = dist / total_dist if total_dist > 0 else 0.0
    return int(duration_s * fraction * 1000)


def build_fit(
    name: str,
    legs_data: list[dict[str, Any]],
    elevation_data: list[dict[str, Any]],
    duration_s: float,
    base_time: datetime,
) -> bytes:
    legs = [RouteLeg(**leg) for leg in legs_data]
    profile = [ElevationPoint(**p) for p in elevation_data]
    shapes = [decode_polyline6(leg.geometry) for leg in legs]

    merged: list[Point] = []
    for shape in shapes:
        merged.extend(shape[1:] if merged and shape and shape[0] == merged[-1] else shape)
    dists = cumulative_distances(merged)
    total_dist = dists[-1] if dists else 0.0
    elevations = interpolate_elevations(merged, profile)
    base_ms = int(base_time.timestamp() * 1000)

    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    file_id = FileIdMessage()
    file_id.type = FileType.COURSE
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 0x1234
    file_id.time_created = base_ms
    builder.add(file_id)

    course = CourseMessage()
    course.course_name = name[:COURSE_POINT_NAME_MAX]
    course.sport = Sport.CYCLING
    builder.add(course)

    lap = LapMessage()
    lap.timestamp = base_ms
    lap.start_time = base_ms
    lap.start_position_lat = merged[0][0]
    lap.start_position_long = merged[0][1]
    lap.end_position_lat = merged[-1][0]
    lap.end_position_long = merged[-1][1]
    lap.total_elapsed_time = duration_s
    lap.total_timer_time = duration_s
    lap.total_distance = total_dist
    builder.add(lap)

    start = EventMessage()
    start.event = Event.TIMER
    start.event_type = EventType.START
    start.timestamp = base_ms
    builder.add(start)

    for (lat, lon), dist, ele in zip(merged, dists, elevations, strict=True):
        record = RecordMessage()
        record.timestamp = base_ms + _time_offset_ms(dist, total_dist, duration_s)
        record.position_lat = lat
        record.position_long = lon
        record.distance = dist
        if ele is not None:
            record.altitude = ele
        builder.add(record)

    builder.add_all(
        list(_course_points(legs, shapes, merged, dists, duration_s, total_dist, base_ms))
    )

    stop = EventMessage()
    stop.event = Event.TIMER
    stop.event_type = EventType.STOP_DISABLE_ALL
    stop.timestamp = base_ms + int(duration_s * 1000)
    builder.add(stop)

    fit_file = builder.build()
    data: bytes = fit_file.to_bytes()
    return data
