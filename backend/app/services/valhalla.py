"""Client for the Valhalla routing engine: /route and /height."""

import contextlib
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import settings
from app.schemas import ElevationPoint, RouteLeg, RouteRequest, RouteResponse
from app.services.polyline import decode_polyline6
from app.services.presets import PRESETS

MAX_ELEVATION_SAMPLES = 500


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
        shape = _concat_shapes([decode_polyline6(leg.geometry) for leg in legs])
        elevation = await self._elevation_profile(shape)
        ascent, descent = _ascent_descent(elevation)
        return RouteResponse(
            legs=legs,
            distance_m=trip["summary"]["length"] * 1000.0,
            duration_s=trip["summary"]["time"],
            ascent_m=ascent,
            descent_m=descent,
            elevation=elevation,
        )

    async def _elevation_profile(self, shape: list[tuple[float, float]]) -> list[ElevationPoint]:
        sampled = _downsample(shape, MAX_ELEVATION_SAMPLES)
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


def _concat_shapes(shapes: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for shape in shapes:
        # Legs share their boundary point; drop the duplicate on join.
        merged.extend(shape[1:] if merged and shape and shape[0] == merged[-1] else shape)
    return merged


def _downsample(points: list[tuple[float, float]], limit: int) -> list[tuple[float, float]]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def _ascent_descent(elevation: list[ElevationPoint]) -> tuple[float, float]:
    ascent = descent = 0.0
    for prev, curr in zip(elevation, elevation[1:], strict=False):
        delta = curr.elev_m - prev.elev_m
        if delta > 0:
            ascent += delta
        else:
            descent -= delta
    return ascent, descent
