"""Geometry helpers over (lat, lon) coordinate lists."""

import math

EARTH_RADIUS_M = 6371000.0

Point = tuple[float, float]


def haversine(a: Point, b: Point) -> float:
    """Distance in meters between two (lat, lon) points."""
    dlat = math.radians(b[0] - a[0])
    dlon = math.radians(b[1] - a[1])
    lat1 = math.radians(a[0])
    lat2 = math.radians(b[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def cumulative_distances(points: list[Point]) -> list[float]:
    """Cumulative distance in meters at each vertex."""
    dists = [0.0]
    for prev, curr in zip(points, points[1:], strict=False):
        dists.append(dists[-1] + haversine(prev, curr))
    return dists


def concat_shapes(shapes: list[list[Point]]) -> list[Point]:
    """Join leg shapes, dropping the duplicated boundary point."""
    merged: list[Point] = []
    for shape in shapes:
        merged.extend(shape[1:] if merged and shape and shape[0] == merged[-1] else shape)
    return merged
