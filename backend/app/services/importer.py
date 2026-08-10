"""Parsing of uploaded route files: GPX, TCX and FIT.

Files come from Komoot, Strava, RideWithGPS, other planners and head units,
and each writes slightly different XML - different namespaces, tracks vs
routes, elevation present or not. Parsing is therefore deliberately
tolerant: anything that yields an ordered list of coordinates is accepted.
Making those points routable (map matching, elevation backfill) happens
later in the import pipeline, not here.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.course_message import CourseMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import FileType

from app.schemas import ElevationPoint
from app.services.geo import Point, cumulative_distances, evenly_sampled

MAX_FILE_BYTES = 20 * 1024 * 1024
# Matches the resolution the routing engine samples its own profiles at, so a
# fallback profile charts the same as a normal one.
MAX_ELEVATION_SAMPLES = 500
MAX_POINTS = 100_000
NAME_MAX = 200

# Moving time excludes the traffic lights and the cafe. Below this speed the
# rider is stopped, not crawling - 0.5 m/s is under walking pace, so a track
# that drifts while stationary does not count as movement. A gap longer than
# MAX_MOVING_GAP_S is a paused recording or a lost fix rather than a slow
# minute, and counting it would make an overnight pause look like riding.
MOVING_MIN_SPEED_MS = 0.5
MAX_MOVING_GAP_S = 60.0

# Timestamps far outside plausible GPS history are a misread field, not a
# ride. The FIT epoch offset is the usual culprit when they appear.
MIN_PLAUSIBLE_YEAR = 1990

# FIT files carry ".FIT" at bytes 8-12 of the header.
FIT_MAGIC = b".FIT"
SUPPORTED_FORMATS = ("gpx", "tcx", "fit")

# A FIT file declares its purpose in its file_id message. Only these two are
# things anyone would upload here; a settings or weight file that reaches this
# far is left undeclared and judged on its content like a GPX.
#
# Keyed on the profile values rather than the enum members: fit_tool annotates
# FileIdMessage.type as FileType but hands back the raw int it read off the
# wire, so a lookup by member silently misses every time.
_FIT_KINDS = {FileType.COURSE.value: "course", FileType.ACTIVITY.value: "activity"}


class RouteImportError(Exception):
    """A file could not be turned into a track. The message is user-facing."""


@dataclass(frozen=True)
class TrackPoint:
    lat: float
    lon: float
    ele: float | None = None
    # When the rider was here. Absent from planned routes by definition, and
    # from plenty of recorded ones too - some exporters strip it.
    #
    # Heart rate, power, cadence and speed are deliberately not carried. They
    # are the bulk of a FIT file and nothing in the app reads them; adding a
    # field later is easy, removing a stored one is a migration.
    time: datetime | None = None


@dataclass(frozen=True)
class ImportedTrack:
    """An ordered list of coordinates parsed out of an uploaded file."""

    name: str | None
    points: list[TrackPoint]
    source_format: str
    # What the file says it is - "course" or "activity" - when it says at all.
    # Only FIT and TCX declare it; GPX has no equivalent, so it stays None.
    declared_kind: str | None = None

    @property
    def shape(self) -> list[Point]:
        return [(p.lat, p.lon) for p in self.points]

    @property
    def has_elevation(self) -> bool:
        """True only when every point has elevation, so a partial profile
        still gets backfilled from the routing engine rather than trusted."""
        return all(p.ele is not None for p in self.points)

    @property
    def started_at(self) -> datetime | None:
        return next((p.time for p in self.points if p.time is not None), None)

    @property
    def ended_at(self) -> datetime | None:
        return next((p.time for p in reversed(self.points) if p.time is not None), None)

    @property
    def elapsed_time_s(self) -> float | None:
        """Wall clock from first fix to last, stops included."""
        start, end = self.started_at, self.ended_at
        if start is None or end is None:
            return None
        return max((end - start).total_seconds(), 0.0)

    @property
    def moving_time_s(self) -> float | None:
        """Elapsed time minus the standing around.

        None when the file has no usable timestamps at all - which is not the
        same as a ride of zero moving time, and callers need to tell those
        apart.
        """
        if self.started_at is None:
            return None

        dists = cumulative_distances(self.shape)
        moving = 0.0
        for i in range(1, len(self.points)):
            before, after = self.points[i - 1], self.points[i]
            if before.time is None or after.time is None:
                continue
            dt = (after.time - before.time).total_seconds()
            if dt <= 0 or dt > MAX_MOVING_GAP_S:
                continue
            if (dists[i] - dists[i - 1]) / dt < MOVING_MIN_SPEED_MS:
                continue
            moving += dt
        return moving

    @property
    def is_activity(self) -> bool:
        """A record of a ride that happened, rather than a plan for one.

        The file's own declaration wins where there is one. Timestamps alone
        cannot decide this: Moovelo's own course export stamps every record
        with a time synthesised from the routing duration (services/fit.py),
        so a course it wrote would otherwise read as a ride someone did.
        """
        if self.declared_kind is not None:
            return self.declared_kind == "activity"
        return self.started_at is not None


def parse_route_file(filename: str, data: bytes) -> ImportedTrack:
    """Parse an uploaded file into a track, or raise RouteImportError."""
    if not data:
        raise RouteImportError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise RouteImportError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")

    fmt = detect_format(filename, data)
    track = {"gpx": _parse_gpx, "tcx": _parse_tcx, "fit": _parse_fit}[fmt](data)

    if len(track.points) < 2:
        raise RouteImportError("No usable track points found in the file.")
    if len(track.points) > MAX_POINTS:
        raise RouteImportError(
            f"File has {len(track.points)} points, more than the {MAX_POINTS} allowed."
        )
    return track


def elevation_profile(
    track: ImportedTrack, total_distance_m: float | None = None
) -> list[ElevationPoint]:
    """Profile built from the file's own elevation, as a fallback.

    Imported routes normally take elevation from the routing engine, exactly
    as planned routes do, so every route in the library reports ascent from
    the same source and stays comparable. This is for when the engine has
    none - tiles built without elevation data.

    Scaled onto `total_distance_m` when given: map matching moves the geometry
    onto the road network, so the matched route is rarely the same length as
    the recorded track and the profile has to line up with it.
    """
    dists = cumulative_distances(track.shape)
    samples = [
        (dist, point.ele)
        for dist, point in zip(dists, track.points, strict=True)
        if point.ele is not None
    ]
    if len(samples) < 2 or dists[-1] <= 0:
        return []

    scale = total_distance_m / dists[-1] if total_distance_m is not None else 1.0
    return [
        ElevationPoint(dist_m=dist * scale, elev_m=ele)
        for dist, ele in evenly_sampled(samples, MAX_ELEVATION_SAMPLES)
    ]


def detect_format(filename: str, data: bytes) -> str:
    """Format from the file extension, falling back to sniffing the content.

    Head units and browsers are inconsistent about extensions, so the bytes
    win when the name is unhelpful.
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in SUPPORTED_FORMATS:
        return suffix
    if data[8:12] == FIT_MAGIC:
        return "fit"
    head = data[:512].lstrip().lower()
    if b"<gpx" in head:
        return "gpx"
    if b"trainingcenterdatabase" in head:
        return "tcx"
    raise RouteImportError("Unsupported file type - expected a .gpx, .tcx or .fit file.")


def _localname(tag: str) -> str:
    """Tag without its XML namespace - GPX 1.0, GPX 1.1 and TCX all differ."""
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> Iterator[ET.Element]:
    for element in root.iter():
        if _localname(element.tag) == name:
            yield element


def _first_child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _localname(child.tag) == name and child.text:
            return child.text
    return None


def _clean_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    name = " ".join(raw.split())[:NAME_MAX]
    return name or None


def _make_point(
    lat: Any, lon: Any, ele: Any = None, time: datetime | None = None
) -> TrackPoint | None:
    """Build a point, dropping anything not plausibly a GPS fix."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90) or not (-180 <= lon_f <= 180):
        return None
    # Null Island is what a device without a fix writes, never a real ride.
    if lat_f == 0.0 and lon_f == 0.0:
        return None
    try:
        ele_f = None if ele is None else float(ele)
    except (TypeError, ValueError):
        ele_f = None
    return TrackPoint(lat=lat_f, lon=lon_f, ele=ele_f, time=time)


def _parse_time(raw: str | None) -> datetime | None:
    """An ISO 8601 instant from GPX <time> or TCX <Time>.

    Tolerant on purpose, like the rest of this module: a file with an
    unreadable timestamp is still a perfectly good track, so a bad value
    yields None rather than rejecting the whole upload.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    # GPX requires UTC and TCX conventionally uses it, so a naive stamp is
    # UTC rather than local - guessing the rider's timezone would be worse.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed if parsed.year >= MIN_PLAUSIBLE_YEAR else None


def _fit_kind(raw: Any) -> str | None:
    """A kind from a file_id type field, tolerating either form it arrives in."""
    if isinstance(raw, FileType):
        raw = raw.value
    try:
        return _FIT_KINDS.get(int(raw))
    except (TypeError, ValueError):
        return None


def _fit_time(raw: Any) -> datetime | None:
    """fit_tool reports timestamps as milliseconds since the Unix epoch."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromtimestamp(float(raw) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.year >= MIN_PLAUSIBLE_YEAR else None


def _parse_xml(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise RouteImportError(f"File is not valid XML: {exc}") from exc


def _parse_gpx(data: bytes) -> ImportedTrack:
    root = _parse_xml(data)

    # Track points first; a file with only <rtept> is a planned route, which
    # is equally importable. Segments are concatenated in document order.
    points = _gpx_points(root, "trkpt") or _gpx_points(root, "rtept")
    return ImportedTrack(name=_gpx_name(root), points=points, source_format="gpx")


def _gpx_points(root: ET.Element, tag: str) -> list[TrackPoint]:
    points = []
    for element in _find_all(root, tag):
        point = _make_point(
            element.get("lat"),
            element.get("lon"),
            _first_child_text(element, "ele"),
            _parse_time(_first_child_text(element, "time")),
        )
        if point is not None:
            points.append(point)
    return points


def _gpx_name(root: ET.Element) -> str | None:
    for container in ("metadata", "trk", "rte"):
        for element in _find_all(root, container):
            if name := _clean_name(_first_child_text(element, "name")):
                return name
    return None


def _parse_tcx(data: bytes) -> ImportedTrack:
    root = _parse_xml(data)

    points = []
    for element in _find_all(root, "Trackpoint"):
        position = next(_find_all(element, "Position"), None)
        if position is None:
            continue
        point = _make_point(
            _first_child_text(position, "LatitudeDegrees"),
            _first_child_text(position, "LongitudeDegrees"),
            _first_child_text(element, "AltitudeMeters"),
            _parse_time(_first_child_text(element, "Time")),
        )
        if point is not None:
            points.append(point)

    return ImportedTrack(
        name=_tcx_name(root),
        points=points,
        source_format="tcx",
        declared_kind=_tcx_kind(root),
    )


def _tcx_kind(root: ET.Element) -> str | None:
    """TCX says which it is structurally: <Courses> or <Activities>."""
    if next(_find_all(root, "Course"), None) is not None:
        return "course"
    if next(_find_all(root, "Activity"), None) is not None:
        return "activity"
    return None


def _tcx_name(root: ET.Element) -> str | None:
    """Courses carry a <Name>; activities only have an <Id> (a timestamp)."""
    for container in ("Course", "Activity"):
        for element in _find_all(root, container):
            raw = _first_child_text(element, "Name") or _first_child_text(element, "Id")
            if name := _clean_name(raw):
                return name
    return None


def _parse_fit(data: bytes) -> ImportedTrack:
    try:
        fit_file = FitFile.from_bytes(data)
    except Exception as exc:  # fit_tool raises a range of parse errors
        raise RouteImportError(f"File is not a readable FIT file: {exc}") from exc

    points = []
    name = None
    kind = None
    for record in fit_file.records:
        message = record.message
        if isinstance(message, RecordMessage):
            point = _make_point(
                message.position_lat,
                message.position_long,
                message.altitude,
                _fit_time(message.timestamp),
            )
            if point is not None:
                points.append(point)
        elif isinstance(message, CourseMessage) and name is None:
            name = _clean_name(message.course_name)
        elif isinstance(message, FileIdMessage) and kind is None:
            kind = _fit_kind(message.type)

    return ImportedTrack(name=name, points=points, source_format="fit", declared_kind=kind)
