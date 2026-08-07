from fastapi import APIRouter, Request
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
)
from app.services.valhalla import ValhallaClient

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
