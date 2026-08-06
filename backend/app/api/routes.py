import re
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Response
from geoalchemy2 import WKTElement
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.models import Route
from app.schemas import (
    ElevationPoint,
    RouteLeg,
    RoutePatchRequest,
    RouteResponse,
    RouteSaveRequest,
    RouteSummary,
    SavedRoute,
    SharedRoute,
    WahooState,
)
from app.services.fit import build_fit
from app.services.geo import concat_shapes
from app.services.gpx import build_gpx
from app.services.polyline import decode_polyline6

router = APIRouter(prefix="/api/routes")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "route"


def _geom_wkt(snapshot: RouteResponse) -> WKTElement:
    shape = concat_shapes([decode_polyline6(leg.geometry) for leg in snapshot.legs])
    coords = ",".join(f"{lon} {lat}" for lat, lon in shape)
    return WKTElement(f"LINESTRING({coords})", srid=4326)


def _apply_snapshot(route: Route, snapshot: RouteResponse) -> None:
    route.legs = [leg.model_dump() for leg in snapshot.legs]
    route.elevation = [point.model_dump() for point in snapshot.elevation]
    route.distance_m = snapshot.distance_m
    route.duration_s = snapshot.duration_s
    route.ascent_m = snapshot.ascent_m
    route.descent_m = snapshot.descent_m
    route.geom = _geom_wkt(snapshot)


async def get_owned_route(db: DbDep, user: UserDep, route_id: uuid.UUID) -> Route:
    route = (
        await db.execute(select(Route).where(Route.id == route_id, Route.user_id == user.id))
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return route


def _saved(route: Route) -> SavedRoute:
    return SavedRoute(
        id=route.id,
        name=route.name,
        preset=route.preset,
        waypoints=route.waypoints,
        legs=route.legs,
        elevation=route.elevation,
        distance_m=route.distance_m,
        duration_s=route.duration_s,
        ascent_m=route.ascent_m,
        descent_m=route.descent_m,
        updated_at=route.updated_at,
        wahoo=WahooState(
            status=route.wahoo_status,
            error=route.wahoo_error,
            route_id=route.wahoo_route_id,
            pushed_at=route.wahoo_pushed_at,
        ),
        share_token=route.share_token,
    )


@router.get("")
async def list_routes(db: DbDep, user: UserDep) -> list[RouteSummary]:
    rows = (
        (
            await db.execute(
                select(Route).where(Route.user_id == user.id).order_by(Route.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [RouteSummary.from_route(r) for r in rows]


@router.post("", status_code=201)
async def save_route(body: RouteSaveRequest, db: DbDep, user: UserDep) -> SavedRoute:
    route = Route(
        user_id=user.id,
        name=body.name,
        preset=body.preset,
        waypoints=[wp.model_dump() for wp in body.waypoints],
    )
    _apply_snapshot(route, body.snapshot)
    db.add(route)
    await db.commit()
    await db.refresh(route)
    return _saved(route)


@router.get("/{route_id}")
async def get_route(route_id: uuid.UUID, db: DbDep, user: UserDep) -> SavedRoute:
    return _saved(await get_owned_route(db, user, route_id))


@router.patch("/{route_id}")
async def update_route(
    route_id: uuid.UUID, body: RoutePatchRequest, db: DbDep, user: UserDep
) -> SavedRoute:
    route = await get_owned_route(db, user, route_id)
    if body.name is not None:
        route.name = body.name
    if body.waypoints is not None:
        route.waypoints = [wp.model_dump() for wp in body.waypoints]
    if body.preset is not None:
        route.preset = body.preset
    if body.snapshot is not None:
        _apply_snapshot(route, body.snapshot)
    await db.commit()
    await db.refresh(route)
    return _saved(route)


@router.delete("/{route_id}", status_code=204)
async def delete_route(route_id: uuid.UUID, db: DbDep, user: UserDep) -> None:
    await db.delete(await get_owned_route(db, user, route_id))
    await db.commit()


def _gpx_response(route: Route) -> Response:
    legs = [RouteLeg(**leg) for leg in route.legs]
    shape = concat_shapes([decode_polyline6(leg.geometry) for leg in legs])
    profile = [ElevationPoint(**p) for p in route.elevation]
    return Response(
        content=build_gpx(route.name, shape, profile),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{_slug(route.name)}.gpx"'},
    )


@router.get("/{route_id}/export.gpx")
async def export_gpx(route_id: uuid.UUID, db: DbDep, user: UserDep) -> Response:
    return _gpx_response(await get_owned_route(db, user, route_id))


@router.get("/{route_id}/export.fit")
async def export_fit(route_id: uuid.UUID, db: DbDep, user: UserDep) -> Response:
    route = await get_owned_route(db, user, route_id)
    return Response(
        content=build_fit(
            route.name, route.legs, route.elevation, route.duration_s, route.updated_at
        ),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_slug(route.name)}.fit"'},
    )


@router.post("/{route_id}/share")
async def share_route(route_id: uuid.UUID, db: DbDep, user: UserDep) -> SavedRoute:
    """Create (or rotate) the public share token for a route."""
    route = await get_owned_route(db, user, route_id)
    route.share_token = secrets.token_urlsafe(16)
    await db.commit()
    await db.refresh(route)
    return _saved(route)


@router.delete("/{route_id}/share")
async def revoke_share(route_id: uuid.UUID, db: DbDep, user: UserDep) -> SavedRoute:
    route = await get_owned_route(db, user, route_id)
    route.share_token = None
    await db.commit()
    await db.refresh(route)
    return _saved(route)


# --- Public share endpoints (token is the only credential) -----------------

shared_router = APIRouter(prefix="/api/shared")


async def _shared_route(db: DbDep, token: str) -> Route:
    route = (await db.execute(select(Route).where(Route.share_token == token))).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Shared route not found")
    return route


@shared_router.get("/{token}")
async def get_shared(token: str, db: DbDep) -> SharedRoute:
    route = await _shared_route(db, token)
    return SharedRoute(
        name=route.name,
        preset=route.preset,
        legs=route.legs,
        elevation=route.elevation,
        distance_m=route.distance_m,
        duration_s=route.duration_s,
        ascent_m=route.ascent_m,
        descent_m=route.descent_m,
        updated_at=route.updated_at,
    )


@shared_router.get("/{token}/export.gpx")
async def shared_gpx(token: str, db: DbDep) -> Response:
    return _gpx_response(await _shared_route(db, token))
