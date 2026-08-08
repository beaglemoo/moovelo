"""Open-Meteo-compatible wind forecast, sampled along a route line.

Off by default: nothing here is called unless WEATHER_API_URL is set (see
app/config.py) and the frontend must never call the endpoint automatically -
a rider presses "Show wind" for it to run at all.
"""

import asyncio
import hashlib
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.schemas import RideTimePoint, WeatherAlongRoute, WindSegment
from app.services.geo import Point, bearing, cumulative_distances, point_at_distance

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Roughly one wind sample every 10 km, capped so a very long route still
# sends one bounded request rather than one per point.
WEATHER_SAMPLE_SPACING_M = 10_000.0
MAX_WEATHER_SAMPLES = 20

# Forecast providers are slow-changing and the planner may ask about the
# same route+time repeatedly while a rider fiddles with the start time
# picker; a short in-process cache avoids hammering the upstream service.
_CACHE_TTL_S = 30 * 60
_cache: dict[str, tuple[float, WeatherAlongRoute]] = {}


class WeatherError(Exception):
    """Terminal weather-lookup failure whose message is shown to the user."""


def _cache_key(
    start_time: datetime,
    samples: list[tuple[Point, float]],
    total_ride_s: float,
    ride_time: list[RideTimePoint] | None,
) -> str:
    """Cache key: rounded start hour, a hash of rounded sample coordinates,
    and a pacing signature - close-enough repeats of the same ask hit the
    cache without needing exact float equality.

    The pacing signature matters: the same shape and start time at two
    different durations (or ride-time profiles) ask for different forecast
    windows and time each sample's arrival differently, so they must not
    collide on the same cache entry and return each other's wind.
    """
    start_hour = start_time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    coords = ",".join(f"{lat:.3f}:{lon:.3f}" for (lat, lon), _ in samples)
    pacing = f"{round(total_ride_s / 60.0)}"
    if ride_time:
        profile = ",".join(f"{round(p.dist_m)}:{round(p.time_s)}" for p in ride_time)
        pacing += f"|{profile}"
    raw = f"{start_hour.isoformat()}|{coords}|{pacing}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> WeatherAlongRoute | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, payload = entry
    if time.monotonic() - cached_at > _CACHE_TTL_S:
        del _cache[key]
        return None
    return payload


# Distinct route/start-hour keys would otherwise accumulate for the life of
# the process - lazy same-key eviction alone never removes them.
_CACHE_MAX_ENTRIES = 128


def _cache_set(key: str, payload: WeatherAlongRoute) -> None:
    now = time.monotonic()
    expired = [k for k, (at, _) in _cache.items() if now - at > _CACHE_TTL_S]
    for k in expired:
        del _cache[k]
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]
    _cache[key] = (now, payload)


def _sample_route(shape: list[Point]) -> tuple[list[tuple[Point, float]], float]:
    """Points along `shape` roughly WEATHER_SAMPLE_SPACING_M apart, each
    paired with its distance along the route, plus the route's total length.
    Always includes the start; a route shorter than the spacing yields
    exactly one sample. The total is the whole route's length, not the last
    sample's position - a capped sample count must not shrink the
    denominator used to place arrival times proportionally.

    Fixed 10 km spacing only reaches (MAX_WEATHER_SAMPLES - 1) * spacing -
    about 190 km - before the count cap kicks in and every sample beyond
    that point is silently dropped, leaving the back half of a longer route
    with no wind coverage at all. Past that length, MAX_WEATHER_SAMPLES
    points are instead spread evenly across the whole route so the last
    sample always lands on the route's actual end.
    """
    dists = cumulative_distances(shape)
    total = dists[-1]
    samples: list[tuple[Point, float]] = []
    if total > (MAX_WEATHER_SAMPLES - 1) * WEATHER_SAMPLE_SPACING_M:
        for i in range(MAX_WEATHER_SAMPLES):
            target = i * total / (MAX_WEATHER_SAMPLES - 1)
            samples.append((point_at_distance(shape, dists, target), target))
        return samples, total
    count = 1
    if total > 0:
        count = min(MAX_WEATHER_SAMPLES, int(total // WEATHER_SAMPLE_SPACING_M) + 1)
    for i in range(count):
        target = min(i * WEATHER_SAMPLE_SPACING_M, total)
        samples.append((point_at_distance(shape, dists, target), target))
    return samples, total


def _travel_bearings(shape: list[Point], sample_points: list[Point]) -> list[float]:
    """Travel bearing at each sample: bearing toward the next sample, with
    the last sample using the previous leg's bearing. A single-sample route
    has no leg between samples, so it falls back to the bearing of the
    route's own start-to-end line."""
    if len(sample_points) <= 1:
        if shape[0] == shape[-1]:
            return [0.0]
        return [bearing(shape[0], shape[-1])]
    legs = [bearing(sample_points[i], sample_points[i + 1]) for i in range(len(sample_points) - 1)]
    return [*legs, legs[-1]]


def _smallest_signed_angle(a_deg: float, b_deg: float) -> float:
    """Smallest signed angle (degrees, -180..180] to rotate from a to b."""
    return (b_deg - a_deg + 180.0) % 360.0 - 180.0


def _interpolate_ride_time(dist_m: float, ride_time: list[RideTimePoint]) -> float:
    # Callers pass an already distance-ordered profile (weather_along_route
    # sorts client input once); no re-sort per sample.
    points = ride_time
    if dist_m <= points[0].dist_m:
        return points[0].time_s
    if dist_m >= points[-1].dist_m:
        return points[-1].time_s
    for prev, curr in zip(points, points[1:], strict=False):
        if prev.dist_m <= dist_m <= curr.dist_m:
            span = curr.dist_m - prev.dist_m
            t = (dist_m - prev.dist_m) / span if span > 0 else 0.0
            return prev.time_s + (curr.time_s - prev.time_s) * t
    return points[-1].time_s


def _arrival_seconds(
    dist_m: float,
    total_m: float,
    ride_time: list[RideTimePoint] | None,
    duration_s: float | None,
) -> float:
    if ride_time:
        return _interpolate_ride_time(dist_m, ride_time)
    if duration_s is not None and total_m > 0:
        return duration_s * (dist_m / total_m)
    return 0.0


def _nearest_hour_index(times: list[str], target: datetime) -> int | None:
    """Index of the hourly slot closest to `target` (naive, since the
    request asked for timezone=UTC and `target` has already been converted
    to UTC and stripped of tzinfo)."""
    if not times:
        return None
    best_idx = 0
    best_diff: float | None = None
    for i, raw in enumerate(times):
        parsed = datetime.fromisoformat(raw)
        diff = abs((parsed - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_idx = i
    return best_idx


async def _fetch_forecast(params: dict[str, str]) -> list[dict[str, Any]] | dict[str, Any] | None:
    """GET the forecast, retrying only 429/5xx. Returns None for a
    deterministic rejection (out-of-window dates, or any `error: true`
    body) so the caller can degrade rather than fail the whole request."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.get(settings.weather_api_url, params=params)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise WeatherError("Weather service is unreachable - try again later") from exc
                await asyncio.sleep(min(2**attempt, 60.0))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    break
                # Retry-After may legally be an HTTP-date rather than
                # delay-seconds; anything non-numeric falls back to the
                # exponential default instead of crashing the request.
                try:
                    retry_after = float(response.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    retry_after = float(2**attempt)
                logger.warning(
                    "Weather request retry %d after HTTP %d", attempt, response.status_code
                )
                await asyncio.sleep(min(retry_after, 60.0))
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            if response.status_code >= 400:
                return None
            if isinstance(body, dict) and body.get("error") is True:
                return None
            result: list[dict[str, Any]] | dict[str, Any] | None = body
            return result
    raise WeatherError("Weather service is rate-limiting or unavailable - try again later")


async def weather_along_route(
    shape: list[Point],
    start_time: datetime,
    ride_time: list[RideTimePoint] | None,
    duration_s: float | None,
) -> WeatherAlongRoute:
    """Wind at points sampled along `shape`, timed to when the rider expects
    to reach each one."""
    if len(shape) < 2:
        return WeatherAlongRoute(segments=[], truncated=True)

    # Client-supplied profiles are not guaranteed ordered; sort once here so
    # the per-sample interpolation never has to.
    if ride_time:
        ride_time = sorted(ride_time, key=lambda p: p.dist_m)

    samples, total_m = _sample_route(shape)
    bearings = _travel_bearings(shape, [point for point, _ in samples])
    start_utc = start_time.astimezone(UTC)

    # Prefer the elevation-aware ride_time profile's own total over the flat
    # duration_s: arrivals below are computed against ride_time whenever it
    # is present (see _arrival_seconds), so sizing the forecast window from
    # a different total would leave it too short to cover a slow uphill
    # finish, or too long for a fast one.
    total_ride_s = ride_time[-1].time_s if ride_time else (duration_s or 0.0)
    end_utc = start_utc + timedelta(seconds=max(total_ride_s, 0.0))

    cache_key = _cache_key(start_utc, samples, total_ride_s, ride_time)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "latitude": ",".join(f"{point[0]:.5f}" for point, _ in samples),
        "longitude": ",".join(f"{point[1]:.5f}" for point, _ in samples),
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "start_date": start_utc.date().isoformat(),
        "end_date": end_utc.date().isoformat(),
    }
    body = await _fetch_forecast(params)
    if body is None:
        result = WeatherAlongRoute(segments=[], truncated=True)
        _cache_set(cache_key, result)
        return result

    locations = body if isinstance(body, list) else [body]

    segments: list[WindSegment] = []
    for (point, dist_along), travel_bearing, location in zip(
        samples, bearings, locations, strict=False
    ):
        arrival_s = _arrival_seconds(dist_along, total_m, ride_time, duration_s)
        arrival = start_utc + timedelta(seconds=arrival_s)
        hourly = location.get("hourly", {})
        times = hourly.get("time", [])
        idx = _nearest_hour_index(times, arrival.replace(tzinfo=None))
        if idx is None:
            continue
        speeds = hourly.get("wind_speed_10m", [])
        directions = hourly.get("wind_direction_10m", [])
        if idx >= len(speeds) or idx >= len(directions):
            continue
        # Individual hourly slots can be null at the edge of the model's
        # range; skip the sample (surfacing as truncated) rather than 500.
        if speeds[idx] is None or directions[idx] is None:
            continue
        speed = float(speeds[idx])
        direction = float(directions[idx])
        downwind = (direction + 180.0) % 360.0
        relative = _smallest_signed_angle(downwind, travel_bearing)
        headwind = -speed * math.cos(math.radians(relative))
        crosswind = speed * math.sin(math.radians(relative))
        segments.append(
            WindSegment(
                lat=point[0],
                lon=point[1],
                dist_along_m=dist_along,
                arrival_iso=arrival.isoformat(),
                wind_speed_ms=speed,
                wind_direction_deg=direction,
                headwind_ms=headwind,
                crosswind_ms=crosswind,
            )
        )

    result = WeatherAlongRoute(segments=segments, truncated=len(segments) < len(samples))
    _cache_set(cache_key, result)
    return result
