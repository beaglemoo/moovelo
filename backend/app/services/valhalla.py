"""Client for the Valhalla routing engine: /route, /trace_route and /height."""

import asyncio
import contextlib
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import settings
from app.schemas import ElevationPoint, Preset, RouteLeg, RouteRequest, RouteResponse
from app.services.geo import (
    Point,
    concat_shapes,
    cumulative_distances,
    evenly_sampled,
    resample_by_distance,
)
from app.services.polyline import decode_polyline6, encode_polyline6
from app.services.presets import PRESETS

MAX_ELEVATION_SAMPLES = 500

# Map matching: thin recorded tracks to roughly one point per TRACE_SPACING_M,
# then send at most TRACE_MAX_POINTS per request. Valhalla accepts far more,
# but long traces are slow and a failure loses the whole track rather than one
# chunk of it.
TRACE_SPACING_M = 15.0
TRACE_MAX_POINTS = 1000

# Valhalla ends every trip with a destination maneuver. A chunk is a piece of
# one ride, so every chunk but the last would contribute a spurious "you have
# arrived" cue partway along.
DESTINATION_MANEUVERS = {4, 5, 6}

# Only used to give an unmatched chunk a plausible duration, in metres/second.
FALLBACK_SPEED_MS = 5.0


class ValhallaClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url or settings.valhalla_url, timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.TransportError as exc:
            raise HTTPException(
                status_code=503,
                detail="Routing engine unavailable - it may still be building tiles.",
            ) from exc
        if response.status_code >= 400:
            detail = "Routing request failed."
            with contextlib.suppress(ValueError):
                detail = response.json().get("error", detail)
            if "no suitable edges" in detail.lower() or "no path could be found" in detail.lower():
                detail += " - is this area covered by the loaded map extract?"
            raise HTTPException(status_code=422, detail=detail)
        data: dict[str, Any] = response.json()
        return data

    async def route(self, request: RouteRequest) -> RouteResponse:
        payload: dict[str, Any] = {
            "locations": [{"lat": w.lat, "lon": w.lon, "type": "break"} for w in request.waypoints],
            "costing": "bicycle",
            "costing_options": {"bicycle": PRESETS[request.preset]},
            "units": "kilometers",
        }
        data = await self._post("/route", payload)
        trip = data["trip"]
        legs = [RouteLeg(geometry=leg["shape"], maneuvers=leg["maneuvers"]) for leg in trip["legs"]]
        shape = concat_shapes([decode_polyline6(leg.geometry) for leg in legs])
        elevation = await self._elevation_profile(shape)
        ascent, descent = ascent_descent(elevation)
        return RouteResponse(
            legs=legs,
            distance_m=trip["summary"]["length"] * 1000.0,
            duration_s=trip["summary"]["time"],
            ascent_m=ascent,
            descent_m=descent,
            elevation=elevation,
        )

    async def trace_route(self, shape: list[Point], preset: Preset) -> RouteResponse:
        """Snap an imported track to the road network, gaining maneuvers.

        An imported GPX is just coordinates - no turn instructions - so a head
        unit can follow the line but cannot prompt. Matching the track back
        onto the routing graph recovers the maneuvers that make FIT course
        points, and therefore turn-by-turn cues on the ELEMNT.
        """
        thinned = resample_by_distance(shape, TRACE_SPACING_M)
        if len(thinned) < 2:
            raise HTTPException(status_code=422, detail="Track is too short to match to roads.")

        # Chunks are independent, so request them together and join the
        # results in order afterwards: a long track otherwise waits for the
        # sum of every round trip rather than the slowest one.
        chunks = _chunks(thinned, TRACE_MAX_POINTS)
        results = await asyncio.gather(
            *(self._trace_chunk(chunk, preset) for chunk in chunks),
            return_exceptions=True,
        )
        outcomes: list[dict[str, Any] | HTTPException] = []
        for result in results:
            # An unreachable engine is retryable, so it must not be quietly
            # downgraded into a partly-cueless route.
            if isinstance(result, BaseException) and (
                not isinstance(result, HTTPException) or result.status_code >= 500
            ):
                raise result
            outcomes.append(result)

        # A track that matched nowhere is unmatchable, and the caller decides
        # what to do with it. Only a partial failure is worth salvaging here.
        failures = [o for o in outcomes if isinstance(o, HTTPException)]
        if len(failures) == len(outcomes):
            raise failures[0]

        legs: list[RouteLeg] = []
        distance_m = duration_s = 0.0
        # Keyed off the last chunk that actually matched, not the last chunk:
        # if the final stretch failed to match it contributes no maneuvers, so
        # stripping the previous chunk's destination would leave the whole
        # route with no arrival cue at all.
        last = max(i for i, o in enumerate(outcomes) if not isinstance(o, HTTPException))
        for position, (chunk, result) in enumerate(zip(chunks, outcomes, strict=True)):
            if isinstance(result, HTTPException):
                # Keep the stretch this chunk covers instead of discarding
                # every chunk that did match - the point of chunking.
                length = cumulative_distances(chunk)[-1]
                legs.append(RouteLeg(geometry=encode_polyline6(chunk), maneuvers=[]))
                distance_m += length
                duration_s += length / FALLBACK_SPEED_MS
                continue
            for leg in result["legs"]:
                maneuvers = leg["maneuvers"]
                if position != last:
                    maneuvers = [
                        m for m in maneuvers if int(m.get("type", 0)) not in DESTINATION_MANEUVERS
                    ]
                legs.append(RouteLeg(geometry=leg["shape"], maneuvers=maneuvers))
            distance_m += result["summary"]["length"] * 1000.0
            duration_s += result["summary"]["time"]

        matched = concat_shapes([decode_polyline6(leg.geometry) for leg in legs])
        elevation = await self._elevation_profile(matched)
        ascent, descent = ascent_descent(elevation)
        return RouteResponse(
            legs=legs,
            distance_m=distance_m,
            duration_s=duration_s,
            ascent_m=ascent,
            descent_m=descent,
            elevation=elevation,
        )

    async def _trace_chunk(self, chunk: list[Point], preset: Preset) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "shape": [{"lat": lat, "lon": lon} for lat, lon in chunk],
            "shape_match": "map_snap",
            "costing": "bicycle",
            "costing_options": {"bicycle": PRESETS[preset]},
            "units": "kilometers",
        }
        trip: dict[str, Any] = (await self._post("/trace_route", payload))["trip"]
        return trip

    async def _elevation_profile(self, shape: list[tuple[float, float]]) -> list[ElevationPoint]:
        sampled = evenly_sampled(shape, MAX_ELEVATION_SAMPLES)
        payload = {
            "shape": [{"lat": lat, "lon": lon} for lat, lon in sampled],
            "range": True,
        }
        try:
            data = await self._post("/height", payload)
        except HTTPException:
            # Elevation is an enhancement; a route without a profile is still
            # useful (e.g. tiles built with build_elevation=False).
            return []
        return [
            ElevationPoint(dist_m=dist, elev_m=elev)
            for dist, elev in data.get("range_height", [])
            if elev is not None
        ]


def _chunks(points: list[Point], size: int) -> list[list[Point]]:
    """Split a trace into request-sized chunks that share a boundary point, so
    the matched legs join up instead of leaving a gap."""
    if len(points) <= size:
        return [points]
    return [points[start : start + size] for start in range(0, len(points) - 1, size - 1)]


def ascent_descent(elevation: list[ElevationPoint]) -> tuple[float, float]:
    ascent = descent = 0.0
    for prev, curr in zip(elevation, elevation[1:], strict=False):
        delta = curr.elev_m - prev.elev_m
        if delta > 0:
            ascent += delta
        else:
            descent -= delta
    return ascent, descent
