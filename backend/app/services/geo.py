"""Geometry helpers over (lat, lon) coordinate lists."""

import math

EARTH_RADIUS_M = 6371000.0

Point = tuple[float, float]


def evenly_sampled[T](items: list[T], limit: int) -> list[T]:
    """Thin a list to at most `limit` items, keeping the first and last."""
    if limit <= 1:
        return items[:limit]
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)]


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


def point_at_distance(points: list[Point], dists: list[float], target: float) -> Point:
    """Coordinate at `target` meters along `points`, linearly interpolated.

    `dists` is `cumulative_distances(points)`, passed in rather than
    recomputed so a caller sampling many targets along the same line pays
    for it once.
    """
    if target <= 0:
        return points[0]
    total = dists[-1]
    if target >= total:
        return points[-1]
    lo, hi = 0, len(dists) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if dists[mid] <= target:
            lo = mid
        else:
            hi = mid
    span = dists[hi] - dists[lo]
    t = (target - dists[lo]) / span if span > 0 else 0.0
    lat = points[lo][0] + (points[hi][0] - points[lo][0]) * t
    lon = points[lo][1] + (points[hi][1] - points[lo][1]) * t
    return (lat, lon)


def bearing(a: Point, b: Point) -> float:
    """Great-circle initial bearing from `a` to `b`, degrees 0-360 (0 = due
    north, 90 = due east)."""
    lat1 = math.radians(a[0])
    lat2 = math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def resample_by_distance(points: list[Point], spacing_m: float) -> list[Point]:
    """Thin a track to roughly one point per `spacing_m`, keeping both ends.

    Recorded tracks are sampled by time, so a slow climb can carry hundreds of
    points across a few meters while a fast descent is sparse. Map matching
    wants roughly even spacing and chokes on the volume otherwise.
    """
    if len(points) < 3:
        return list(points)
    kept = [points[0]]
    for point in points[1:-1]:
        if haversine(kept[-1], point) >= spacing_m:
            kept.append(point)
    kept.append(points[-1])
    return kept


def concat_shapes(shapes: list[list[Point]]) -> list[Point]:
    """Join leg shapes, dropping the duplicated boundary point."""
    merged: list[Point] = []
    for shape in shapes:
        merged.extend(shape[1:] if merged and shape and shape[0] == merged[-1] else shape)
    return merged
