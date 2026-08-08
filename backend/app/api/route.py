from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.api.routes import with_ride_time
from app.api.settings import get_or_default_settings
from app.config import settings
from app.models import SearchIndexMeta
from app.schemas import (
    MAX_ROUTE_POINTS,
    AppConfig,
    BicycleCostingOptions,
    ElevationPoint,
    Latitude,
    Longitude,
    Preset,
    RideTimePoint,
    RouteRequest,
    RouteResponse,
    RouteSurfaceResponse,
    WeatherAlongRoute,
    WeatherQuery,
)
from app.services.ride_time import compute_ride_time
from app.services.valhalla import MAX_ELEVATION_SAMPLES, ValhallaClient
from app.services.weather import WeatherError, weather_along_route

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
    preset: Preset = "road"
    # Overrides `preset` entirely when set - see resolve_costing.
    costing_options: BicycleCostingOptions | None = None
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
    surface = await client.trace_attributes(legs, body.preset, body.costing_options)
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
