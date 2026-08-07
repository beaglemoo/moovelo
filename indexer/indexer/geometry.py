"""Turning a way into a single point, and names into something searchable.

Deliberately dependency-free, in the style of the backend's services/geo.py:
a signed-area centroid is twenty lines, and pulling in shapely for it would
add GEOS to an image whose only other native dependency is osmium itself.
"""

import math
import unicodedata

Point = tuple[float, float]  # (lat, lon), matching the backend's convention


def normalise(name: str) -> str:
    """Fold accents and case so trigram matching treats Ynys Môn as Ynys Mon.

    Postgres could do this with unaccent(), but that function is STABLE
    rather than IMMUTABLE and so cannot be indexed or used in a generated
    column. Doing it here, once, at index time is both cheaper and the only
    way to get a trigram index on the result.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().strip()


def polygon_centroid(ring: list[Point]) -> Point | None:
    """Centre of a closed way - a supermarket mapped as its building.

    The signed-area formula, which is correct for a concave outline where
    averaging the vertices is not. Degenerate rings (all points collinear,
    or every vertex identical) have zero area, so those fall back to the
    mean of the vertices rather than dividing by zero.
    """
    if len(ring) < 3:
        return _mean(ring)

    twice_area = 0.0
    lat_sum = 0.0
    lon_sum = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:], strict=False):
        cross = lon1 * lat2 - lon2 * lat1
        twice_area += cross
        lat_sum += (lat1 + lat2) * cross
        lon_sum += (lon1 + lon2) * cross

    if abs(twice_area) < 1e-12:
        return _mean(ring)
    return (lat_sum / (3.0 * twice_area), lon_sum / (3.0 * twice_area))


def midpoint(line: list[Point]) -> Point | None:
    """The vertex closest to halfway along an open way.

    Rare - a POI mapped on a linear feature - so this stays cheap and
    approximate rather than interpolating between vertices.
    """
    if len(line) < 3:
        return _mean(line)

    lengths = [0.0]
    for previous, current in zip(line, line[1:], strict=False):
        lengths.append(lengths[-1] + _flat_distance(previous, current))
    if lengths[-1] <= 0:
        return _mean(line)

    half = lengths[-1] / 2.0
    nearest = min(range(len(line)), key=lambda i: abs(lengths[i] - half))
    return line[nearest]


def way_point(nodes: list[Point], closed: bool) -> Point | None:
    """One point standing in for a whole way, or None if it has no geometry."""
    if not nodes:
        return None
    return polygon_centroid(nodes) if closed else midpoint(nodes)


def _mean(points: list[Point]) -> Point | None:
    if not points:
        return None
    return (
        sum(lat for lat, _ in points) / len(points),
        sum(lon for _, lon in points) / len(points),
    )


def _flat_distance(a: Point, b: Point) -> float:
    """Good enough to find a midpoint; this is not a distance in meters."""
    return math.hypot(a[0] - b[0], a[1] - b[1])
