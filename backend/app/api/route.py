import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.api.routes import _settings_for, with_ride_time
from app.api.settings import get_or_default_settings
from app.config import settings
from app.models import SearchIndexMeta
from app.schemas import (
    MAX_ROUTE_POINTS,
    AlternatesQuery,
    AlternatesResponse,
    AppConfig,
    ElevationPoint,
    IsochroneQuery,
    IsochroneResponse,
    Latitude,
    Longitude,
    LoopCandidatesResponse,
    LoopQuery,
    RideTimePoint,
    RouteRequest,
    RouteResponse,
    RouteSurfaceResponse,
    WeatherAlongRoute,
    WeatherQuery,
)
from app.services.llm_config import resolve_llm_config
from app.services.loop import generate_loop_candidates
from app.services.presets import resolve_costing
from app.services.ride_time import compute_ride_time
from app.services.valhalla import MAX_ELEVATION_SAMPLES, ValhallaClient
from app.services.weather import WeatherError, weather_along_route

# Bound on the whole search, not just each Valhalla call: dozens of LAN
# round trips (services/loop.py) run per request, and the per-bearing early
# stop keeps a normal search well under this - it exists for the pathological
# case, not the common one.
LOOP_TIMEOUT_S = 25.0

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def config(db: DbDep) -> AppConfig:
    # One row at most, so this is a cheap probe on every page load rather
    # than a count of a table the indexer may have filled with hundreds of
    # thousands of places. Taking built_at rather than a bare EXISTS costs
    # nothing and doubles as the tile cache key.
    built_at = await db.scalar(select(SearchIndexMeta.built_at))
    return AppConfig(
        tile_url_cyclosm=settings.tile_url_cyclosm or None,
        search_enabled=built_at is not None,
        search_index_version=str(int(built_at.timestamp())) if built_at else None,
        weather_enabled=settings.weather_enabled,
        # Resolved, not the bare env flag: the assistant can be configured
        # entirely from the admin page, and reading settings here would leave
        # the panel hidden on an install that never set the env vars.
        assistant_enabled=(await resolve_llm_config(db)).enabled,
    )


@router.post("/route")
async def plan_route(
    request: Request, body: RouteRequest, db: DbDep, user: UserDep
) -> RouteResponse:
    client: ValhallaClient = request.app.state.valhalla
    snapshot = await client.route(body)
    return await with_ride_time(snapshot, db, user.id)


class RouteSurfaceQuery(BaseModel):
    """A route's own legs (and, optionally, its elevation) to break down by
    surface, sent rather than a route id so the planner can ask before
    anything is saved."""

    # One [lon, lat] array per route leg (GeoJSON order, matching
    # PoiQuery.line), each needing at least 2 points to be traced. Legs stay
    # separate rather than concatenated: a via waypoint's legs meet at a
    # shared coordinate but arrive/depart on different edges, and
    # ValhallaClient.trace_attributes fails outright on a chunk straddling
    # that discontinuity.
    legs: list[list[tuple[Longitude, Latitude]]] = Field(min_length=1)
    # No preset or costing here deliberately: edge_walk follows the shape's
    # own edges, so costing cannot change the breakdown - it could only break
    # it (see ValhallaClient._attributes_chunk). Unknown extra fields from
    # older clients are ignored by pydantic's default config.
    # When given, the response also carries a surface-aware ride_time -
    # otherwise plan_route's own paved-equivalent estimate is the only one
    # the planner has until the route is saved.
    elevation: list[ElevationPoint] | None = Field(
        default=None, max_length=MAX_ELEVATION_SAMPLES * 2
    )

    @model_validator(mode="after")
    def _check_leg_lengths_and_total(self) -> "RouteSurfaceQuery":
        for leg in self.legs:
            if len(leg) < 2:
                raise ValueError("Each leg needs at least 2 points.")
        total = sum(len(leg) for leg in self.legs)
        if total > MAX_ROUTE_POINTS:
            raise ValueError(f"Route has too many points (max {MAX_ROUTE_POINTS}).")
        return self


@router.post("/route/surface")
async def route_surface(
    request: Request, body: RouteSurfaceQuery, user: UserDep, db: DbDep
) -> RouteSurfaceResponse:
    """Surface breakdown for the given legs and, when elevation is supplied,
    a ride-time estimate computed with that breakdown.

    This is how the planner's live estimate becomes surface-aware before a
    route is ever saved: plan_route runs before any surface breakdown
    exists, so without this the displayed time always assumes factor 1.0
    (paved). Folding both into one request avoids a second round trip -
    and a second latency hit - added to /api/route itself.
    """
    client: ValhallaClient = request.app.state.valhalla
    # [lon, lat] per leg (GeoJSON order) in; ValhallaClient legs are
    # (lat, lon), matching PoiQuery's handling of the same ordering.
    legs = [[(lat, lon) for lon, lat in leg] for leg in body.legs]
    surface = await client.trace_attributes(legs)
    ride_time: list[RideTimePoint] = []
    if body.elevation is not None:
        # Computed even when surface is None (factor 1.0), matching
        # plan_route's own behaviour for a route with no breakdown yet.
        settings_ = await get_or_default_settings(db, user.id)
        ride_time = compute_ride_time(body.elevation, surface, settings_)
    return RouteSurfaceResponse(surface=surface, ride_time=ride_time)


@router.post("/route/weather")
async def route_weather(body: WeatherQuery, _user: UserDep) -> WeatherAlongRoute:
    """Wind sampled along a route line for a chosen start time.

    Gated first, before anything else runs: an unconfigured instance must
    never make an outbound request, and 404 (rather than a 200 with an
    empty result) is what lets the frontend hide the feature entirely.
    """
    if not settings.weather_enabled:
        raise HTTPException(status_code=404, detail="Weather is not configured.")
    # [lon, lat] in, (lat, lon) out - matching RouteSurfaceQuery and PoiQuery.
    shape = [(lat, lon) for lon, lat in body.line]
    try:
        return await weather_along_route(shape, body.start_time, body.ride_time, body.duration_s)
    except WeatherError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _isochrone_reaches_anywhere(data: dict[str, Any]) -> bool:
    """Whether any contour spans more than ~500 m in either axis.

    Coordinates are walked structurally (rings of [lon, lat] pairs) so both
    polygon and linestring output shapes are covered without caring which.
    """
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")

    def walk(node: Any) -> None:
        nonlocal min_lon, min_lat, max_lon, max_lat
        if (
            isinstance(node, list)
            and len(node) == 2
            and all(isinstance(v, (int, float)) for v in node)
        ):
            lon, lat = node
            min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
            min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for feature in data.get("features", []):
        walk(feature.get("geometry", {}).get("coordinates", []))
    if min_lon == float("inf"):
        return False
    # ~500 m in degrees: generous at any UK latitude, and a real contour of
    # even a few minutes is kilometres across.
    return (max_lat - min_lat) > 0.0045 or (max_lon - min_lon) > 0.0045


@router.post("/route/isochrone")
async def route_isochrone(
    request: Request, body: IsochroneQuery, _user: UserDep
) -> IsochroneResponse:
    """Reachability polygons ("how far can I get in N minutes") from a
    point, sent rather than tied to a saved route - it is anchored to
    wherever the rider right-clicked, not to any route on screen.
    """
    client: ValhallaClient = request.app.state.valhalla
    costing = resolve_costing(body.preset, body.costing_options)
    data = await client.isochrone(
        body.origin,
        body.contours,
        costing,
        polygons=body.polygons,
        denoise=body.denoise,
        generalize=body.generalize,
    )
    # An origin in open water or outside the extract does not error - Valhalla
    # snaps to nothing and returns a degenerate ~50 m placeholder ring as a
    # cheerful 200. Even one minute of cycling covers hundreds of metres, so
    # a bounding box under that across every feature means the origin found
    # no roads, and saying so beats drawing an invisible speck.
    if not _isochrone_reaches_anywhere(data):
        raise HTTPException(
            status_code=422,
            detail=(
                "No reachable roads at that point - is it on land, and covered "
                "by the loaded map extract?"
            ),
        )
    return IsochroneResponse.model_validate(data)


@router.post("/route/alternates")
async def route_alternates(
    request: Request, body: AlternatesQuery, db: DbDep, user: UserDep
) -> AlternatesResponse:
    """Up to `body.count` alternative routes alongside the primary, for the
    A-to-B pair AlternatesQuery restricts this to (see its docstring for
    why). Both the primary and every alternate carry a ride_time, computed
    for the caller's own rider settings, so adopting any of them keeps a
    populated stats row rather than losing it until the route is re-planned.
    """
    client: ValhallaClient = request.app.state.valhalla
    route_request = RouteRequest(
        waypoints=body.waypoints,
        preset=body.preset,
        costing_options=body.costing_options,
        exclude_locations=body.exclude_locations,
    )
    primary, alternates = await client.route_alternates(route_request, body.count)
    # One settings read for all candidates - the same row cannot change
    # between them, and with_ride_time would re-select it per snapshot.
    rider = await _settings_for(db, user.id)
    primary = primary.model_copy(
        update={"ride_time": compute_ride_time(primary.elevation, primary.surface, rider)}
    )
    alternates = [
        alt.model_copy(update={"ride_time": compute_ride_time(alt.elevation, alt.surface, rider)})
        for alt in alternates
    ]
    return AlternatesResponse(primary=primary, alternates=alternates)


@router.post("/route/loop")
async def route_loop(
    request: Request, body: LoopQuery, db: DbDep, user: UserDep
) -> LoopCandidatesResponse:
    """Up to three candidate loops from `body.origin`, each an ordinary
    out-and-back route through one via point (services/loop.py). Bounded so
    a rider is never left staring at a spinner indefinitely; partial results
    on timeout are not returned - see LOOP_TIMEOUT_S.
    """
    client: ValhallaClient = request.app.state.valhalla
    try:
        candidates = await asyncio.wait_for(
            generate_loop_candidates(
                body.origin,
                body.target_km,
                body.preset,
                body.costing_options,
                client,
                body.exclude_locations,
            ),
            timeout=LOOP_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="Loop search timed out - try a shorter target distance."
        ) from exc
    # Every sibling route endpoint attaches a rider-settings ride_time;
    # without this, picking a candidate silently reverted the displayed
    # duration to Valhalla's flat estimate. One settings read for all three.
    rider = await _settings_for(db, user.id)
    for candidate in candidates:
        snap = candidate.snapshot
        candidate.snapshot = snap.model_copy(
            update={"ride_time": compute_ride_time(snap.elevation, snap.surface, rider)}
        )
    return LoopCandidatesResponse(candidates=candidates)
