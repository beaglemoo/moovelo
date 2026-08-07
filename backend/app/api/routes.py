import re
import secrets
import uuid
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from geoalchemy2 import WKTElement
from sqlalchemy import func, or_, select

from app.api.deps import DbDep, UserDep
from app.models import Route
from app.schemas import (
    ElevationPoint,
    Preset,
    RouteLeg,
    RoutePatchRequest,
    RouteRequest,
    RouteResponse,
    RouteSaveRequest,
    RouteSummary,
    SavedRoute,
    SharedRoute,
    SurfaceBreakdown,
    WahooState,
    Waypoint,
)
from app.services.fit import build_fit
from app.services.geo import concat_shapes
from app.services.gpx import build_gpx
from app.services.import_routes import import_route, match_or_keep
from app.services.importer import MAX_FILE_BYTES, RouteImportError
from app.services.polyline import decode_polyline6
from app.services.valhalla import ValhallaClient

router = APIRouter(prefix="/api/routes")

UPLOAD_CHUNK_BYTES = 64 * 1024


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "route"


def _snapshot_fields(route: Route) -> dict[str, Any]:
    return {
        "legs": route.legs,
        "elevation": route.elevation,
        "distance_m": route.distance_m,
        "duration_s": route.duration_s,
        "ascent_m": route.ascent_m,
        "descent_m": route.descent_m,
        "surface": route.surface,
    }


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
    route.surface = snapshot.surface.model_dump() if snapshot.surface else None
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
        source=route.source,
        tags=route.tags,
        notes=route.notes,
        is_favourite=route.is_favourite,
        waypoints=route.waypoints,
        legs=route.legs,
        elevation=route.elevation,
        distance_m=route.distance_m,
        duration_s=route.duration_s,
        ascent_m=route.ascent_m,
        descent_m=route.descent_m,
        surface=cast("SurfaceBreakdown | None", route.surface),
        updated_at=route.updated_at,
        wahoo=WahooState(
            status=route.wahoo_status,
            error=route.wahoo_error,
            route_id=route.wahoo_route_id,
            pushed_at=route.wahoo_pushed_at,
        ),
        share_token=route.share_token,
    )


SORT_COLUMNS = {
    "updated": Route.updated_at,
    "name": Route.name,
    "distance": Route.distance_m,
    "ascent": Route.ascent_m,
}


@router.get("")
async def list_routes(
    db: DbDep,
    user: UserDep,
    q: str | None = None,
    tag: str | None = None,
    favourite: bool | None = None,
    source: str | None = None,
    sort: Literal["updated", "name", "distance", "ascent"] = "updated",
    order: Literal["asc", "desc"] = "desc",
) -> list[RouteSummary]:
    """List the user's routes, optionally searched, filtered and sorted."""
    query = select(Route).where(Route.user_id == user.id)
    if q:
        # Notes are searched as well as names - recording "cafe at 12km" is
        # only useful if it can be found again.
        # Escape LIKE wildcards so a route named "50% gravel" is searchable
        # and a bare "%" does not match everything.
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.where(
            or_(Route.name.ilike(pattern, escape="\\"), Route.notes.ilike(pattern, escape="\\"))
        )
    if tag:
        query = query.where(Route.tags.contains([tag]))
    if favourite is not None:
        query = query.where(Route.is_favourite.is_(favourite))
    if source:
        query = query.where(Route.source == source)

    column = SORT_COLUMNS[sort]
    query = query.order_by(column.asc() if order == "asc" else column.desc())
    rows = (await db.execute(query)).scalars().all()
    return [RouteSummary.from_route(r) for r in rows]


@router.get("/tags")
async def list_tags(db: DbDep, user: UserDep) -> list[str]:
    """Every tag the user has used, so the library can offer them."""
    rows = (
        await db.execute(
            select(func.unnest(Route.tags).label("tag"))
            .where(Route.user_id == user.id)
            .distinct()
            .order_by("tag")
        )
    ).scalars()
    return list(rows)


@router.post("/import", status_code=201)
async def import_route_file(
    request: Request,
    db: DbDep,
    user: UserDep,
    file: Annotated[UploadFile, File()],
    preset: Annotated[Preset, Form()] = "road",
) -> SavedRoute:
    """Import a GPX, TCX or FIT file as a saved route."""
    data = await _read_capped(file)
    try:
        imported = await import_route(file.filename or "", data, preset, request.app.state.valhalla)
    except RouteImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    route = Route(
        user_id=user.id,
        name=imported.track.name or _name_from_filename(file.filename),
        preset=preset,
        source="imported",
        waypoints=[wp.model_dump() for wp in imported.waypoints],
    )
    _apply_snapshot(route, imported.snapshot)
    db.add(route)
    await db.commit()
    await db.refresh(route)
    return _saved(route)


async def _read_capped(file: UploadFile) -> bytes:
    """Read an upload, giving up once it exceeds the import size limit.

    Reading the whole body first would let a multi-gigabyte upload exhaust
    memory before the limit was ever consulted.
    """
    buffer = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.",
            )
    return bytes(buffer)


def _clean_tags(tags: list[str]) -> list[str]:
    """Trimmed, de-duplicated, order preserved, empties dropped."""
    seen: dict[str, None] = {}
    for tag in tags:
        cleaned = " ".join(tag.split())[:40]
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _name_from_filename(filename: str | None) -> str:
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return " ".join(stem.replace("_", " ").replace("-", " ").split())[:200] or "Imported route"


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
    if body.tags is not None:
        route.tags = _clean_tags(body.tags)
    if body.notes is not None:
        route.notes = body.notes or None
    if body.is_favourite is not None:
        route.is_favourite = body.is_favourite
    if body.waypoints is not None:
        route.waypoints = [wp.model_dump() for wp in body.waypoints]
        # Once re-routed between its endpoints, the imported track is gone and
        # the route is an ordinary planned one.
        route.source = "planned"
    if body.preset is not None:
        route.preset = body.preset
    if body.snapshot is not None:
        _apply_snapshot(route, body.snapshot)
    await db.commit()
    await db.refresh(route)
    return _saved(route)


def _suffixed(name: str, suffix: str) -> str:
    """Append a suffix without overflowing the name column."""
    room = 200 - len(suffix)
    return f"{name[:room].rstrip()}{suffix}"


def _copy_of(route: Route, name: str) -> Route:
    """A new route carrying the same organisation but none of the sync state:
    a copy has never been pushed to Wahoo and is not shared."""
    return Route(
        user_id=route.user_id,
        name=name,
        preset=route.preset,
        source=route.source,
        tags=list(route.tags),
        notes=route.notes,
        is_favourite=route.is_favourite,
    )


@router.post("/{route_id}/duplicate", status_code=201)
async def duplicate_route(route_id: uuid.UUID, db: DbDep, user: UserDep) -> SavedRoute:
    """Copy a route as-is. The stored snapshot is reused rather than
    re-routed, so a duplicate is identical even if the map data has moved
    on since the original was planned."""
    route = await get_owned_route(db, user, route_id)
    copy = _copy_of(route, _suffixed(route.name, " (copy)"))
    _apply_snapshot(copy, RouteResponse(**_snapshot_fields(route)))
    copy.waypoints = route.waypoints
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return _saved(copy)


@router.post("/{route_id}/reverse", status_code=201)
async def reverse_route(
    request: Request, route_id: uuid.UUID, db: DbDep, user: UserDep
) -> SavedRoute:
    """Ride it the other way, as a new route.

    Reversing re-routes rather than flipping the stored line, because one-way
    streets and turn instructions are direction-dependent - a flipped
    geometry would hand the rider cues for the journey they are not making.
    Non-destructive: you usually want both directions in the library.
    """
    route = await get_owned_route(db, user, route_id)
    valhalla: ValhallaClient = request.app.state.valhalla

    if route.source == "imported":
        # An imported route has no meaningful waypoints, so reverse the track
        # itself and match it again in the new direction.
        shape = concat_shapes([decode_polyline6(RouteLeg(**leg).geometry) for leg in route.legs])
        shape.reverse()
        # Same fallback as import: a track that could not be matched forwards
        # will not match backwards either, and losing the ride is worse than
        # losing its cues.
        snapshot, _matched = await match_or_keep(shape, cast("Preset", route.preset), valhalla)
        waypoints = [
            {"lat": shape[0][0], "lon": shape[0][1]},
            {"lat": shape[-1][0], "lon": shape[-1][1]},
        ]
    else:
        waypoints = list(reversed(route.waypoints))
        snapshot = await valhalla.route(
            RouteRequest(
                waypoints=[Waypoint(**wp) for wp in waypoints], preset=cast("Preset", route.preset)
            )
        )

    # Surface is decorative: it degrades to None on any failure rather than
    # blocking the reverse, so it is fetched after the snapshot is settled.
    surface_shape = concat_shapes([decode_polyline6(leg.geometry) for leg in snapshot.legs])
    surface = await valhalla.trace_attributes(surface_shape, cast("Preset", route.preset))
    if surface is not None:
        snapshot = snapshot.model_copy(update={"surface": surface})

    reversed_route = _copy_of(route, _suffixed(route.name, " (reversed)"))
    reversed_route.waypoints = waypoints
    _apply_snapshot(reversed_route, snapshot)
    db.add(reversed_route)
    await db.commit()
    await db.refresh(reversed_route)
    return _saved(reversed_route)


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
        surface=cast("SurfaceBreakdown | None", route.surface),
        updated_at=route.updated_at,
    )


@shared_router.get("/{token}/export.gpx")
async def shared_gpx(token: str, db: DbDep) -> Response:
    return _gpx_response(await _shared_route(db, token))
