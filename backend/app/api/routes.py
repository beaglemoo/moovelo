import logging
import re
import secrets
import uuid
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from geoalchemy2 import WKTElement
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, UserDep, reload_or_404
from app.api.settings import get_or_default_settings
from app.models import Activity, Route
from app.schemas import (
    DEFAULT_FLAT_SPEED_KMH,
    DEFAULT_WEIGHT_KG,
    BicycleCostingOptions,
    ElevationPoint,
    Preset,
    RideTimePoint,
    RouteActivitiesResponse,
    RouteActivity,
    RouteLeg,
    RoutePatchRequest,
    RouteRequest,
    RouteResponse,
    RouteSaveRequest,
    RouteSummary,
    SavedRoute,
    SharedRoute,
    SurfaceBreakdown,
    UserSettingsResponse,
    WahooState,
    Waypoint,
)
from app.services.climbs import detect_climbs
from app.services.fit import build_fit
from app.services.geo import concat_shapes
from app.services.gpx import build_gpx
from app.services.import_routes import import_route, match_or_keep
from app.services.importer import MAX_FILE_BYTES, RouteImportError
from app.services.llm_config import resolve_llm_config
from app.services.polyline import decode_polyline6
from app.services.ride_time import compute_ride_time
from app.services.route_match import match_activity_to_route
from app.services.route_summary import generate_summary, route_geometry_signature
from app.services.valhalla import ValhallaClient, ascent_descent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routes")

UPLOAD_CHUNK_BYTES = 64 * 1024

# Bounds _rematch_linked_activities. At 9-28ms per re-match on real
# multi-thousand-point traces, 200 is a few seconds in the worst realistic
# shape and effectively never reached in an ordinary library.
MAX_REDERIVE = 200


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
        "climbs": route.climbs,
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
    route.climbs = [climb.model_dump() for climb in snapshot.climbs]
    route.geom = _geom_wkt(snapshot)


# Anonymous viewers (shared links) get the ride-time model's plain
# defaults - there is no rider settings row to look up for someone who
# never logged in, and it would not be the owner's business anyway.
_DEFAULT_SETTINGS = UserSettingsResponse(
    weight_kg=DEFAULT_WEIGHT_KG, flat_speed_kmh=DEFAULT_FLAT_SPEED_KMH, ftp_watts=None
)


async def _settings_for(db: AsyncSession, user_id: uuid.UUID | None) -> UserSettingsResponse:
    if user_id is None:
        return _DEFAULT_SETTINGS
    return await get_or_default_settings(db, user_id)


async def with_ride_time(
    snapshot: RouteResponse, db: AsyncSession, user_id: uuid.UUID | None
) -> RouteResponse:
    """A snapshot fresh off Valhalla, with `ride_time` computed for the
    viewer's rider settings. Never touches `duration_s`."""
    settings = await _settings_for(db, user_id)
    ride_time = compute_ride_time(snapshot.elevation, snapshot.surface, settings)
    return snapshot.model_copy(update={"ride_time": ride_time})


async def _ride_time_for(
    route: Route, db: AsyncSession, user_id: uuid.UUID | None
) -> list[RideTimePoint]:
    """Same computation as `with_ride_time`, over a stored `Route` row
    rather than a fresh `RouteResponse` - used everywhere a saved or
    shared route is read."""
    settings = await _settings_for(db, user_id)
    elevation = [ElevationPoint(**p) for p in route.elevation]
    surface = SurfaceBreakdown(**route.surface) if route.surface else None
    return compute_ride_time(elevation, surface, settings)


async def get_owned_route(db: DbDep, user: UserDep, route_id: uuid.UUID) -> Route:
    route = (
        await db.execute(select(Route).where(Route.id == route_id, Route.user_id == user.id))
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return route


async def _saved(route: Route, db: AsyncSession, user_id: uuid.UUID) -> SavedRoute:
    return SavedRoute(
        id=route.id,
        name=route.name,
        preset=route.preset,
        costing_options=route.costing_options,
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
        climbs=route.climbs,
        ride_time=await _ride_time_for(route, db, user_id),
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
    return await _saved(route, db, user.id)


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
        notes=body.notes,
        preset=body.preset,
        costing_options=body.costing_options.model_dump() if body.costing_options else None,
        waypoints=[wp.model_dump() for wp in body.waypoints],
    )
    _apply_snapshot(route, body.snapshot)
    db.add(route)
    await db.commit()
    await db.refresh(route)
    return await _saved(route, db, user.id)


@router.get("/{route_id}")
async def get_route(route_id: uuid.UUID, db: DbDep, user: UserDep) -> SavedRoute:
    return await _saved(await get_owned_route(db, user, route_id), db, user.id)


@router.get("/{route_id}/activities")
async def route_activities(
    route_id: uuid.UUID, db: DbDep, user: UserDep
) -> RouteActivitiesResponse:
    """The rides matched to this route - services/route_match.py, or a
    rider's own manual link - newest first, for planned-vs-actual.

    404 for another user's route (get_owned_route), and the activity query
    is filtered on both route_id and user_id: a route this rider owns can
    only ever be linked to that same rider's own activities (set_activity_
    route checks both sides), but the filter costs nothing and means this
    endpoint's own isolation does not depend on that other endpoint never
    changing.
    """
    route = await get_owned_route(db, user, route_id)
    ride_time = await _ride_time_for(route, db, user.id)
    predicted_time_s = ride_time[-1].time_s if ride_time else None

    # Re-checked here, not only by get_owned_route above. _ride_time_for
    # awaits a settings read in between, and a DELETE landing in that window
    # leaves this answering 200 with an empty `activities` list - which reads
    # as "no rides have matched this route yet" - beside a predicted_time_s
    # computed from the deleted route's own elevation. The rides did not
    # vanish; activities.route_id is ON DELETE SET NULL, so they were
    # unlinked, and a rider's manually locked match is exactly what gets
    # silently dropped from the answer.
    #
    # Narrows the window; does NOT close it, and the first version of this
    # comment wrongly claimed otherwise. The widest gap is the settings read
    # inside _ride_time_for above, and this catches a delete landing there.
    # A delete landing between this line and the Activity SELECT below still
    # yields a 200 with an empty list - measured, not assumed.
    #
    # Closing it properly is not "check one await later": there is always a
    # later read, so that is a chase with no end. It needs a single atomic
    # read, or a decision that this benign case does not need closing. That
    # is part of the app-wide concurrent-delete class recorded in CLAUDE.md,
    # which reaches 29 commit sites across nine modules and predates this
    # phase - not something to keep patching one endpoint at a time.
    #
    # Via reload_or_404, not a bare db.get. A plain get is served from the
    # session's identity map and never reaches the database, so it happily
    # returns the route this request loaded moments ago and reports a
    # deleted row as present - written that way first, and the test below
    # failed with a 200 until it was corrected. populate_existing=True,
    # which the helper already passes, is what forces the re-read.
    route = await reload_or_404(db, route, "Route not found")

    ordering = func.coalesce(Activity.started_at, Activity.created_at)
    rows = (
        (
            await db.execute(
                select(Activity)
                .where(Activity.route_id == route.id, Activity.user_id == user.id)
                .order_by(ordering.desc())
            )
        )
        .scalars()
        .all()
    )
    return RouteActivitiesResponse(
        predicted_time_s=predicted_time_s,
        activities=[
            RouteActivity(
                id=a.id,
                name=a.name,
                started_at=a.started_at,
                elapsed_time_s=a.elapsed_time_s,
                moving_time_s=a.moving_time_s,
                distance_m=a.distance_m,
                ascent_m=a.ascent_m,
                # Carried, not filtered out. The ride really did happen and
                # really is linked to this route by the rider's own choice -
                # hiding it would lose a row they put there. What must not
                # happen is presenting its comparison as though it still
                # described this route, so the flag travels with it and the
                # page says so.
                match_stale=a.match_stale,
                match_confidence=a.match_confidence,
            )
            for a in rows
        ],
    )


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
        # Tied to `preset` rather than checked on its own: the planner
        # always resends both together on every save, so this is what lets
        # switching back to a named preset clear a previously stored
        # custom bundle rather than leaving it stranded.
        route.costing_options = body.costing_options.model_dump() if body.costing_options else None
    if body.snapshot is not None:
        _apply_snapshot(route, body.snapshot)
    await db.commit()
    if body.snapshot is not None:
        await _rematch_linked_activities(route.id, db)
    route = await reload_or_404(db, route, "Route not found")
    return await _saved(route, db, user.id)


async def _rematch_linked_activities(route_id: uuid.UUID, db: AsyncSession) -> None:
    """Re-derive the matches of every ride linked to a route whose shape just
    changed.

    A saved route is re-routed in place - same row, same id - whenever the
    rider tweaks a waypoint and saves, so any ride matched to it was matched
    against geometry that no longer exists. Nothing else re-derives it: the
    import-time pass only runs when a ride arrives, and its deliberate "no
    candidate qualified means leave the existing link alone" policy is exactly
    wrong here, because here the previous match IS known to be stale. Hence
    clear_if_unmatched.

    Left stale the damage is quiet and compounding: planned-vs-actual shows the
    old confidence beside a predicted time computed from the NEW geometry - a
    confident-looking comparison against a road the ride never touched - and
    ride-time calibration joins on the same link and solves the new elevation
    against the old ride's moving time, corrupting the rider's suggested flat
    speed indefinitely with no visible error.

    Rides the rider matched by hand are untouched: match_activity_to_route
    honours match_locked, which is the whole point of that flag.

    Inline, after the commit that changed the route, and capped. Measured
    against 14,000-point traces (route_match.py's "ordinary four-hour ride at
    1Hz"), each re-match costs 9-28ms, so a regular commute carrying a
    multi-year imported history can turn one save into seconds of work held
    open on the request. MAX_REDERIVE keeps the common case exact and bounds
    the tail: anything past it keeps its existing link and is logged, and a
    rider can correct one by hand from the ride page. A stale link on the far
    tail of an exceptionally well-ridden route is worse-but-visible, and both
    are better than the silent corruption this function exists to stop.

    Oldest-first so the cap always falls on the same rides rather than an
    arbitrary set - an unordered LIMIT is exactly how route_match.py's own
    candidate query managed to drop the true match.

    A failure here cannot fail the save: match_activity_to_route never raises,
    by contract.
    """
    # The locked rides are the ones this pass cannot re-derive - the rider
    # chose that route and the choice stands. Their link is now against
    # geometry that no longer exists, though, so anything DERIVED from
    # comparing ride to route is not to be trusted: calibration solving the
    # new elevation against the old moving time suggested 60 km/h for a real
    # 26 km/h rider. Flagged rather than broken, so the rider keeps their
    # link and the derived readings decline to use it.
    # Every ride linked to this route, not only the locked ones. A locked
    # ride is skipped by the re-derive below by design; an UNLOCKED ride past
    # the MAX_REDERIVE cap is skipped by accident, and both end up carrying a
    # match made against geometry that is gone. Flagging the whole set first
    # and letting each successful re-derive clear its own row (via
    # _store_match) means the cap can move, or a re-derive can fail, without
    # anything being left silently wrong - the flag is cleared by success
    # rather than set by a guess about which rows will be reached.
    await db.execute(update(Activity).where(Activity.route_id == route_id).values(match_stale=True))
    await db.commit()
    linked = (
        (
            await db.execute(
                select(Activity.id)
                .where(Activity.route_id == route_id)
                .order_by(Activity.created_at)
                .limit(MAX_REDERIVE + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(linked) > MAX_REDERIVE:
        logger.warning(
            "route %s has more than %d linked rides; re-deriving the oldest %d and "
            "leaving the rest to an explicit rematch",
            route_id,
            MAX_REDERIVE,
            MAX_REDERIVE,
        )
        linked = linked[:MAX_REDERIVE]
    for activity_id in linked:
        # No commit of our own afterwards. match_activity_to_route commits each
        # change durably itself, so a trailing one here did no useful work -
        # but it was a bare, unguarded commit sitting directly under a
        # docstring promising that a failure here cannot fail the save. That
        # promise covers match_activity_to_route, which never raises; it never
        # covered this line. A transient fault on it would have 500'd a PATCH
        # whose route snapshot and every re-derived match were already durable.
        await match_activity_to_route(activity_id, clear_if_unmatched=True)


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
        # Carried over alongside preset - otherwise a copy of a
        # preset="custom" route would keep that marker but lose the
        # options it refers to.
        costing_options=route.costing_options,
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
    return await _saved(copy, db, user.id)


def _route_costing(route: Route) -> tuple[Preset, BicycleCostingOptions | None]:
    """Base preset and any custom override to re-route a stored route with.

    `route.preset` can now be "custom", which is not a Valhalla preset key
    - casting it straight to Preset (as every call site below used to do)
    is a runtime KeyError inside PRESETS[...] once a custom-costed route
    exists, that mypy has no way to see. When costing_options is set,
    "road" stands in as the placeholder base preset: resolve_costing always
    prefers costing_options over it, so the placeholder is never actually
    looked up.
    """
    if route.costing_options:
        return "road", BicycleCostingOptions(**route.costing_options)
    return cast("Preset", route.preset), None


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
    base_preset, costing = _route_costing(route)

    if route.source == "imported":
        # An imported route has no meaningful waypoints, so reverse the track
        # itself and match it again in the new direction.
        shape = concat_shapes([decode_polyline6(RouteLeg(**leg).geometry) for leg in route.legs])
        shape.reverse()
        # Same fallback as import: a track that could not be matched forwards
        # will not match backwards either, and losing the ride is worse than
        # losing its cues. trace_route stays preset-only by design (see
        # ValhallaClient.trace_route), so `costing` is not threaded through
        # here even when set.
        snapshot, _matched = await match_or_keep(shape, base_preset, valhalla)
        waypoints = [
            {"lat": shape[0][0], "lon": shape[0][1]},
            {"lat": shape[-1][0], "lon": shape[-1][1]},
        ]
        if not snapshot.elevation and route.elevation:
            # match_or_keep's unmatched fallback carries no elevation at
            # all, but the route being reversed usually already has a real
            # profile - either from Valhalla's own /height on the forward
            # match, or from the imported file itself. Losing it on reverse
            # would be a worse regression than losing cues. Mirrors
            # import_route's own fallback (services/import_routes.py) by
            # reversing the stored profile rather than discarding it:
            # distances flip around the route's own length, and ascent and
            # descent naturally swap as a result.
            stored = [ElevationPoint(**p) for p in route.elevation]
            total = stored[-1].dist_m
            reversed_profile = [
                ElevationPoint(dist_m=total - p.dist_m, elev_m=p.elev_m) for p in reversed(stored)
            ]
            ascent, descent = ascent_descent(reversed_profile)
            snapshot = snapshot.model_copy(
                update={
                    "elevation": reversed_profile,
                    "ascent_m": ascent,
                    "descent_m": descent,
                    "climbs": detect_climbs(reversed_profile),
                }
            )
    else:
        waypoints = list(reversed(route.waypoints))
        snapshot = await valhalla.route(
            RouteRequest(
                waypoints=[Waypoint(**wp) for wp in waypoints],
                preset=base_preset,
                costing_options=costing,
            )
        )

    # Surface is decorative: it degrades to None on any failure rather than
    # blocking the reverse, so it is fetched after the snapshot is settled.
    # Legs, not one concatenated shape - see ValhallaClient.trace_attributes.
    surface = await valhalla.trace_attributes(
        [decode_polyline6(leg.geometry) for leg in snapshot.legs]
    )
    if surface is not None:
        snapshot = snapshot.model_copy(update={"surface": surface})

    reversed_route = _copy_of(route, _suffixed(route.name, " (reversed)"))
    reversed_route.waypoints = waypoints
    _apply_snapshot(reversed_route, snapshot)
    db.add(reversed_route)
    await db.commit()
    await db.refresh(reversed_route)
    return await _saved(reversed_route, db, user.id)


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
    """Create (or rotate) the public share token for a route.

    This is the only place a summary is ever generated: an authenticated,
    owner-triggered write. The public read path (get_shared, below) only
    ever serves what was stored here - it must never itself call the LLM.
    """
    route = await get_owned_route(db, user, route_id)
    route.share_token = secrets.token_urlsafe(16)
    current_signature = route_geometry_signature(route)
    # An unchanged route being re-shared (e.g. rotating the link without
    # editing anything) already has a summary that matches its geometry -
    # skip the call rather than spending on a summary that would read
    # identically to the one already stored.
    if route.summary_signature != current_signature:
        config = await resolve_llm_config(db)
        route.summary = await generate_summary(route, config)
        route.summary_signature = current_signature if route.summary else None
    await db.commit()
    route = await reload_or_404(db, route, "Route not found")
    return await _saved(route, db, user.id)


@router.delete("/{route_id}/share")
async def revoke_share(route_id: uuid.UUID, db: DbDep, user: UserDep) -> SavedRoute:
    route = await get_owned_route(db, user, route_id)
    route.share_token = None
    await db.commit()
    route = await reload_or_404(db, route, "Route not found")
    return await _saved(route, db, user.id)


# --- Public share endpoints (token is the only credential) -----------------

shared_router = APIRouter(prefix="/api/shared")


async def _shared_route(db: DbDep, token: str) -> Route:
    route = (await db.execute(select(Route).where(Route.share_token == token))).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Shared route not found")
    return route


@shared_router.get("/{token}")
async def get_shared(token: str, db: DbDep) -> SharedRoute:
    """No LLM call happens anywhere on this path - this is a plain read of
    whatever share_route already stored. `summary` is only ever included
    when its stored signature still matches the route's current geometry;
    a route re-routed since it was last shared has a summary that no
    longer describes it, and that must be suppressed rather than shown."""
    route = await _shared_route(db, token)
    # An anonymous viewer has no rider settings of their own, and it would
    # not be the owner's business anyway: always the ride-time defaults.
    ride_time = await _ride_time_for(route, db, None)
    summary = None
    if route.summary and route.summary_signature == route_geometry_signature(route):
        summary = route.summary
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
        climbs=route.climbs,
        ride_time=ride_time,
        updated_at=route.updated_at,
        summary=summary,
    )


@shared_router.get("/{token}/export.gpx")
async def shared_gpx(token: str, db: DbDep) -> Response:
    return _gpx_response(await _shared_route(db, token))
