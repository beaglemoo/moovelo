from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.api.routes import with_ride_time
from app.config import settings
from app.models import SearchIndexMeta
from app.schemas import (
    MAX_ROUTE_POINTS,
    AppConfig,
    Latitude,
    Longitude,
    Preset,
    RouteRequest,
    RouteResponse,
    SurfaceBreakdown,
    WeatherAlongRoute,
    WeatherQuery,
)
from app.services.valhalla import ValhallaClient
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
    """A route line to break down by surface, sent rather than a route id so
    the planner can ask before anything is saved."""

    # [lon, lat], matching GeoJSON and PoiQuery.line.
    line: list[tuple[Longitude, Latitude]] = Field(min_length=2, max_length=MAX_ROUTE_POINTS)
    preset: Preset = "road"


@router.post("/route/surface")
async def route_surface(
    request: Request, body: RouteSurfaceQuery, _user: UserDep
) -> SurfaceBreakdown | None:
    client: ValhallaClient = request.app.state.valhalla
    # The line arrives as [lon, lat] (GeoJSON order); ValhallaClient shapes
    # are (lat, lon), matching PoiQuery's handling of the same ordering.
    shape = [(lat, lon) for lon, lat in body.line]
    return await client.trace_attributes(shape, body.preset)


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
