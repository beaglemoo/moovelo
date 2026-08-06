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
from typing import Any
from xml.etree import ElementTree as ET

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.course_message import CourseMessage
from fit_tool.profile.messages.record_message import RecordMessage

from app.services.geo import Point

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_POINTS = 100_000
NAME_MAX = 200

# FIT files carry ".FIT" at bytes 8-12 of the header.
FIT_MAGIC = b".FIT"
SUPPORTED_FORMATS = ("gpx", "tcx", "fit")


class RouteImportError(Exception):
    """A file could not be turned into a track. The message is user-facing."""


@dataclass(frozen=True)
class TrackPoint:
    lat: float
    lon: float
    ele: float | None = None


@dataclass(frozen=True)
class ImportedTrack:
    """An ordered list of coordinates parsed out of an uploaded file."""

    name: str | None
    points: list[TrackPoint]
    source_format: str

    @property
    def shape(self) -> list[Point]:
        return [(p.lat, p.lon) for p in self.points]

    @property
    def has_elevation(self) -> bool:
        """True only when every point has elevation, so a partial profile
        still gets backfilled from the routing engine rather than trusted."""
        return all(p.ele is not None for p in self.points)


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


def _make_point(lat: Any, lon: Any, ele: Any = None) -> TrackPoint | None:
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
    return TrackPoint(lat=lat_f, lon=lon_f, ele=ele_f)


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
            element.get("lat"), element.get("lon"), _first_child_text(element, "ele")
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
        )
        if point is not None:
            points.append(point)

    return ImportedTrack(name=_tcx_name(root), points=points, source_format="tcx")


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
    for record in fit_file.records:
        message = record.message
        if isinstance(message, RecordMessage):
            point = _make_point(message.position_lat, message.position_long, message.altitude)
            if point is not None:
                points.append(point)
        elif isinstance(message, CourseMessage) and name is None:
            name = _clean_name(message.course_name)

    return ImportedTrack(name=name, points=points, source_format="fit")
