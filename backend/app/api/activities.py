"""Rides that happened: import, list, read, delete.

No general update endpoint, deliberately. An activity is a record of what
happened, and letting it be rewritten would make the heatmap and coverage
built from it mean nothing in particular. Renaming is the one thing a rider
plausibly wants and is not worth the ambiguity yet.

Linking a ride to the route it followed is different: it does not change
what the ride *was*, only what it is associated with, and a rider correcting
a wrong or missing auto-match is exactly the case services/route_match.py's
`match_locked` exists for - see set_activity_route below.
"""

import asyncio
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import Response
from geoalchemy2.functions import ST_AsText
from sqlalchemy import delete, exists, func, select

from app.api.deps import DbDep, UserDep, reload_or_404
from app.models import Activity, Route
from app.schemas import (
    ActivityDetail,
    ActivityMonthStats,
    ActivityRouteLinkRequest,
    ActivityStatsResponse,
    ActivitySummary,
    ActivityYearStats,
    ArchiveImportStatus,
    ClimbLogResponse,
    ElevationPoint,
    HeatmapAvailability,
)
from app.services.activities import activity_from_track, name_from_filename
from app.services.activity_import import (
    MAX_ARCHIVE_BYTES,
    MAX_QUEUED_ARCHIVES,
    ImportJob,
    QueueFullError,
)
from app.services.activity_import import queue as archive_queue
from app.services.climb_log import climb_log
from app.services.geo import coords_from_wkt
from app.services.heatmap import MAX_HEATMAP_ZOOM, MIN_HEATMAP_ZOOM, heatmap_etag, heatmap_tile
from app.services.importer import MAX_FILE_BYTES, RouteImportError, parse_route_file
from app.services.polyline import encode_polyline6
from app.services.route_match import match_activity_to_route
from app.services.way_matching import queue as match_queue

router = APIRouter(prefix="/api/activities", tags=["activities"])
logger = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 64 * 1024


@router.post("/import", status_code=201)
async def import_activity(
    db: DbDep,
    user: UserDep,
    file: Annotated[UploadFile, File()],
) -> ActivityDetail:
    """Import one recorded ride from a GPX, TCX or FIT file."""
    filename = file.filename or ""
    if _is_archive(filename):
        raise HTTPException(
            status_code=400,
            detail="Use /api/activities/import/archive for a bulk export zip.",
        )
    data = await _read_capped(file, MAX_FILE_BYTES)

    # Parsing a four-hour ride measures at three to four seconds, and it is
    # pure CPU. Left on the event loop it would stall every other request in
    # the process for that whole time, not merely this one.
    try:
        track = await asyncio.to_thread(parse_route_file, filename, data)
    except RouteImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    activity = activity_from_track(
        track, user_id=user.id, fallback_name=name_from_filename(filename)
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    # Off the request path: map matching is a few Valhalla round trips, and a
    # rider uploading one ride should not wait on them (or on Valhalla's own
    # 30s timeout, if it is unreachable). Coverage simply lags the import by
    # however long the queue takes to reach it.
    try:
        match_queue.submit(user.id, [activity.id])
    except QueueFullError:
        # The ride is imported; only its coverage matching is deferred. Its
        # ways_matched_at is still null, so a later backfill picks it up -
        # refusing under a submission burst must not fail the import itself.
        logger.warning("match queue full; activity %s left for backfill", activity.id)
    # Route matching is one SQL query, not a Valhalla round trip, so it runs
    # inline rather than through a queue - see services/route_match.py.
    # Never allowed to fail the import itself: the ride is the valuable
    # thing, its route link is an enrichment.
    # The id in a local, and every log below uses it rather than the
    # instance. An earlier version logged `activity.id` from inside the
    # except block, and a failed refresh leaves the instance expired - so
    # the attribute read raised MissingGreenlet from within the handler
    # that existed to contain the failure, and a durably-committed import
    # 500'd anyway. A guard that touches the thing it is guarding is not a
    # guard.
    activity_id = activity.id
    try:
        await match_activity_to_route(activity_id)
    except Exception:  # noqa: BLE001 - see comment above
        logger.warning("route match failed for activity %s", activity_id, exc_info=True)
    # Unconditionally, and outside the except. The match commits on its own
    # private session, so this instance still holds the pre-match row and has
    # to be re-read before it is rendered.
    #
    # It used to be conditional on the match having returned a route id, to
    # save a SELECT when nothing had changed - and that optimisation was a
    # bug, because the return value does not mean what the condition assumed.
    # match_activity_to_route returns None for "no candidate qualified", for
    # "a rider has this locked", AND for "the row is gone", which are not
    # distinguishable from out here. So the one case that most needed the
    # re-read - the ride deleted while the match was running - was precisely
    # the case that skipped it, and the import answered 201 with a full body
    # for a row that no longer existed, populated stats beside an empty
    # shape. The clear_if_unmatched path has the same shape: it writes and
    # still returns None.
    #
    # Liveness is not something to infer from an unrelated return value. One
    # SELECT per import buys a response that describes a row that is really
    # there, or an honest 404.
    activity = await reload_or_404(db, activity, "Activity not found")
    return await _detail(activity, db)


@router.post("/import/archive", status_code=202)
async def import_archive(
    user: UserDep,
    file: Annotated[UploadFile, File()],
) -> ArchiveImportStatus:
    """Queue a Strava bulk-export zip.

    202, not 201: hundreds of rides at seconds of CPU each is minutes of
    work, and holding a request open for that would time out long before it
    finished. The response is a job to poll.
    """
    filename = file.filename or ""
    if not _is_archive(filename):
        raise HTTPException(status_code=400, detail="Expected a .zip archive.")

    # Checked before _read_capped, not only after: a queue already saturated
    # by earlier jobs is refused without first buffering up to
    # MAX_ARCHIVE_BYTES into memory for a body that was only ever going to be
    # rejected.
    #
    # It does NOT avoid receiving the body. FastAPI has already parsed and
    # spooled the whole multipart payload by the time this line runs - that is
    # what producing the `file` parameter above means, and it is the same
    # mechanism reject_oversized_uploads in main.py was written as middleware
    # to get ahead of. Refusing before the read would have to live there too.
    # What this saves is the second, in-memory copy; test_activity_import_
    # queue_gate.py pins both halves.
    #
    # Nor is it a reservation: two requests can both pass this check against
    # the same not-yet-full queue, so submit()'s own check below is still the
    # real enforcement - see ArchiveImportQueue.full().
    if archive_queue.full():
        raise HTTPException(
            status_code=429,
            detail=(
                f"{MAX_QUEUED_ARCHIVES} archive imports are already queued. "
                "Try again in a few minutes."
            ),
        )

    data = await _read_capped(file, MAX_ARCHIVE_BYTES)
    try:
        job = archive_queue.submit(user.id, filename, data)
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return _job(job)


@router.get("/import/archive/{job_id}")
async def archive_status(job_id: uuid.UUID, user: UserDep) -> ArchiveImportStatus:
    job = archive_queue.get(job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import job not found")
    return _job(job)


def _is_archive(filename: str) -> bool:
    return filename.lower().endswith(".zip")


def _job(job: ImportJob) -> ArchiveImportStatus:
    return ArchiveImportStatus(
        id=job.id,
        filename=job.filename,
        status=job.status,
        total=job.total,
        imported=job.imported,
        failed=job.failed,
        skipped=job.skipped,
        duplicates=job.duplicates,
        error=job.error,
        problems=job.problems,
    )


@router.get("")
async def list_activities(
    db: DbDep,
    user: UserDep,
    year: Annotated[int | None, Query(ge=1990, le=2100)] = None,
    source: Annotated[str | None, Query(max_length=16)] = None,
) -> list[ActivitySummary]:
    """One rider's activities, newest first."""
    # Outer-joined so a ride with no match still comes back (route_name
    # None) rather than being dropped from the list.
    query = (
        select(Activity, Route.name)
        # The join carries the OWNER's id as well as the ride's. Every writer
        # that can set route_id checks both sides today, so a cross-linked row
        # cannot currently exist - but the FK permits it, and a read path does
        # not get to depend on every present and future writer staying
        # correct. Without this a cross-linked row hands a stranger's private
        # route name to whoever owns the ride. GET /api/routes/{id}/activities
        # and services/ride_calibration.py already filter both tables for the
        # same reason; these two reads were the pair that did not.
        .outerjoin(Route, (Route.id == Activity.route_id) & (Route.user_id == user.id))
        .where(Activity.user_id == user.id)
    )
    if year is not None:
        query = query.where(func.extract("year", Activity.started_at) == year)
    if source is not None:
        query = query.where(Activity.source == source)

    # Rides with no timestamp sort by when they were imported rather than
    # falling to the bottom of the list forever.
    ordering = func.coalesce(Activity.started_at, Activity.created_at)
    rows = (await db.execute(query.order_by(ordering.desc()))).all()
    return [_summary(activity, route_name) for activity, route_name in rows]


@router.get("/climbs")
async def list_climb_log(db: DbDep, user: UserDep) -> ClimbLogResponse:
    """This rider's own deduplicated climb log - see services/climb_log.py.

    Ahead of `/{activity_id}` in this file for the same reason `/import`
    and `/heatmap-available` are: FastAPI matches routes in declaration
    order, and a static path declared after the dynamic `/{activity_id}`
    would never be reached - it would parse "climbs" as an activity id and
    404 instead.
    """
    return ClimbLogResponse(climbs=await climb_log(db, user.id))


@router.get("/stats")
async def activity_stats(db: DbDep, user: UserDep) -> ActivityStatsResponse:
    """Yearly (and monthly-within-year) riding totals for /activities.

    Ahead of `/{activity_id}` for the same route-ordering reason `/climbs`
    and `/heatmap-available` are - see the comment on `list_climb_log`.

    Two aggregate SQL queries, not a Python loop summing loaded rows: one
    GROUP BY year for the per-year totals (every activity, including one
    with no `started_at` at all, which lands in a NULL year group - the
    "undated" bucket, not silently dropped, since started_at is nullable
    by design - see Activity.started_at's own comment), and one GROUP BY
    year, month restricted to rows that actually have a month to report,
    for the drill-down. Because the year query has no such restriction,
    summing every bucket it returns always equals this rider's unfiltered
    total - test_api_ride_stats.py pins exactly that reconciliation.

    func.coalesce guards distance and ascent - both NOT NULL on the model,
    so a zero there is always a real sum - from SUM's NULL-for-empty-input.
    `moving_time_s` is deliberately left uncoalesced; see its own comment
    below.

    KNOWN GAP - buckets are UTC, not the rider's local calendar. `started_at`
    is `timestamptz`, which keeps only the absolute instant: the offset the
    source file carried is discarded at import, so there is nothing left to
    reconstruct a local calendar from. `extract` therefore reports the
    session's timezone, which is UTC. Measured consequences: a rider west of
    UTC can see a New Year's Eve ride counted in the following year (22:00 on
    31 December US Pacific reports as year 2026, month 1). In the UK, year
    totals are unaffected - 1 January falls in GMT, which is UTC - but a
    summer ride starting just after local midnight lands in the previous
    month, because 00:30 BST is 23:30 UTC the day before.

    Closing it properly means storing the rider's own offset alongside the
    instant, which is a schema change rather than a query change, so it is
    stated here rather than half-fixed with a guessed timezone.
    """
    year_expr = func.extract("year", Activity.started_at)
    month_expr = func.extract("month", Activity.started_at)

    def _totals() -> tuple[Any, Any, Any, Any]:
        return (
            func.count().label("ride_count"),
            func.coalesce(func.sum(Activity.distance_m), 0.0).label("distance_m"),
            # Deliberately NOT coalesced to 0, unlike distance and ascent.
            # Activity.moving_time_s is the one nullable numeric here - a file
            # can carry no per-point timestamps at all - and format.ts's
            # duration() renders null as "-" precisely so a bucket with no
            # timing data cannot read as a real, suspiciously fast ride.
            # Coalescing here would erase that distinction before the UI ever
            # saw it. SUM already skips individual NULLs, so this is None only
            # when every contributing ride lacked a time.
            func.sum(Activity.moving_time_s).label("moving_time_s"),
            func.coalesce(func.sum(Activity.ascent_m), 0.0).label("ascent_m"),
        )

    year_rows = (
        await db.execute(
            select(year_expr.label("year"), *_totals())
            .where(Activity.user_id == user.id)
            .group_by(year_expr)
        )
    ).all()

    month_rows = (
        await db.execute(
            select(year_expr.label("year"), month_expr.label("month"), *_totals())
            .where(Activity.user_id == user.id, Activity.started_at.is_not(None))
            .group_by(year_expr, month_expr)
        )
    ).all()

    months_by_year: dict[int, list[ActivityMonthStats]] = {}
    for row in month_rows:
        months_by_year.setdefault(int(row.year), []).append(
            ActivityMonthStats(
                month=int(row.month),
                count=row.ride_count,
                distance_m=float(row.distance_m),
                moving_time_s=float(row.moving_time_s) if row.moving_time_s is not None else None,
                ascent_m=float(row.ascent_m),
            )
        )
    for months in months_by_year.values():
        months.sort(key=lambda m: m.month)

    years = [
        ActivityYearStats(
            year=int(row.year) if row.year is not None else None,
            count=row.ride_count,
            distance_m=float(row.distance_m),
            moving_time_s=float(row.moving_time_s) if row.moving_time_s is not None else None,
            ascent_m=float(row.ascent_m),
            months=months_by_year.get(int(row.year), []) if row.year is not None else [],
        )
        for row in year_rows
    ]
    # Newest year first; the undated bucket (year None) sorts last.
    years.sort(key=lambda y: (y.year is None, -(y.year or 0)))
    return ActivityStatsResponse(years=years)


@router.get("/heatmap-available")
async def heatmap_available(db: DbDep, user: UserDep) -> HeatmapAvailability:
    """Whether this rider has any activities.

    Asked once by the planner so the heatmap toggle can be left out
    entirely for a rider with nothing imported, rather than offered as a
    control that would only ever fetch empty tiles. EXISTS rather than a
    COUNT: only the boolean matters, so there is no reason to make
    Postgres count past the first row.
    """
    found = await db.scalar(select(exists().where(Activity.user_id == user.id)))
    return HeatmapAvailability(available=bool(found))


@router.get("/heatmap/{z}/{x}/{y}.mvt")
async def heatmap(
    request: Request,
    db: DbDep,
    user: UserDep,
    z: Annotated[int, Path(ge=MIN_HEATMAP_ZOOM, le=MAX_HEATMAP_ZOOM)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
) -> Response:
    """A vector tile of this rider's own activity traces.

    Cache-Control is deliberately not the cycle-network overlay's
    `max-age=86400`: that overlay is install-wide and only changes when the
    indexer reruns, monthly at most, while this one is personal and changes
    the moment a ride is imported or deleted. `private, no-cache` forces
    revalidation on every request rather than serving a stale tile for a
    day, and the ETag (a count + latest created_at fingerprint, cheap to
    compute) makes that revalidation a 304 instead of a re-fetch on every
    pan when nothing has actually changed.
    """
    # 2^z tiles per axis. Out of range is a client bug, and returning an
    # empty tile would hide it.
    if x >= 2**z or y >= 2**z:
        return Response(status_code=404)

    etag = await heatmap_etag(db, user.id, z, x, y)
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    tile = await heatmap_tile(db, user.id, z, x, y)
    return Response(
        content=tile,
        media_type="application/vnd.mapbox-vector-tile",
        headers=headers,
    )


@router.get("/{activity_id}")
async def get_activity(activity_id: uuid.UUID, db: DbDep, user: UserDep) -> ActivityDetail:
    return await _detail(await _owned(activity_id, db, user.id), db)


@router.put("/{activity_id}/route")
async def set_activity_route(
    activity_id: uuid.UUID,
    body: ActivityRouteLinkRequest,
    db: DbDep,
    user: UserDep,
) -> ActivityDetail:
    """Set or clear the route this ride is matched to, by hand.

    Ownership is checked on both sides: the activity has to be the caller's
    own, and so does the route being linked to it - otherwise a rider could
    link their ride to someone else's saved route (or learn that a given id
    exists at all). Either way, this is a human decision, so it locks out
    the auto-matcher the same way a set and a clear both do.
    """
    activity = await _owned(activity_id, db, user.id)
    if body.route_id is not None:
        route = (
            await db.execute(
                select(Route).where(Route.id == body.route_id, Route.user_id == user.id)
            )
        ).scalar_one_or_none()
        if route is None:
            raise HTTPException(status_code=404, detail="Route not found")
    activity.route_id = body.route_id
    activity.match_locked = True
    activity.match_confidence = None
    await db.commit()
    activity = await reload_or_404(db, activity, "Activity not found")
    return await _detail(activity, db)


@router.post("/{activity_id}/rematch")
async def rematch_activity(activity_id: uuid.UUID, db: DbDep, user: UserDep) -> ActivityDetail:
    """Re-run auto-matching for one ride.

    A no-op if the rider has already picked or cleared the match by hand -
    see match_activity_to_route's own match_locked check, which this relies
    on rather than duplicating.
    """
    activity = await _owned(activity_id, db, user.id)
    await match_activity_to_route(activity.id, clear_if_unmatched=True)
    activity = await reload_or_404(db, activity, "Activity not found")
    return await _detail(activity, db)


@router.delete("/{activity_id}", status_code=204)
async def delete_activity(activity_id: uuid.UUID, db: DbDep, user: UserDep) -> None:
    await _owned(activity_id, db, user.id)
    await db.execute(
        delete(Activity).where(Activity.id == activity_id, Activity.user_id == user.id)
    )
    await db.commit()
    # activity_ways has no per-activity link, so the deleted ride's coverage
    # credit cannot be decremented - without this, coverage stays inflated
    # forever, still reporting ridden ways after every contributing activity
    # is gone. Re-derive it from what remains. The clear+rematch runs on the
    # match-queue worker (clear_first), NOT on this request session: doing it
    # here on a second session deadlocked against the worker's own matching
    # and left phantom over-credits. A re-derive is never put in the bounded
    # tracker (submit() skips _remember for clear_first), so it cannot be
    # evicted or refused - a delete under a full queue always schedules it and
    # never raises QueueFullError.
    match_queue.submit(user.id, clear_first=True)


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload, giving up once it exceeds `limit`.

    Reading the whole body first would let a multi-gigabyte upload exhaust
    memory before the limit was ever consulted. The route importer has its
    own copy of this against a fixed limit; here the limit differs by whether
    the upload is one ride or a whole archive.
    """
    buffer = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Upload is larger than {limit // (1024 * 1024)} MB.",
            )
    return bytes(buffer)


async def _owned(activity_id: uuid.UUID, db: DbDep, user_id: uuid.UUID) -> Activity:
    """The rider's own activity, or a 404.

    404 rather than 403 for someone else's ride: whether a given id exists is
    not something a stranger gets to learn.
    """
    activity = (
        await db.execute(
            select(Activity).where(Activity.id == activity_id, Activity.user_id == user_id)
        )
    ).scalar_one_or_none()
    if activity is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    return activity


def _summary(activity: Activity, route_name: str | None = None) -> ActivitySummary:
    # route_id is derived from route_name, not read straight off the row, so
    # the two cannot disagree. `route_name` is already resolved by an
    # owner-filtered join, and Route.name is NOT NULL, so "no name" means
    # "no route this caller may see" - either unmatched, or matched to a
    # route somebody else owns.
    #
    # The owner filter was added to route_name alone, which left this
    # returning the raw UUID of a stranger's private route beside a
    # correctly-nulled name. No writer can currently produce such a row (both
    # PUT .../route and the matcher scope to one user on both sides), but the
    # filter exists precisely so a read path need not trust that forever, and
    # a guard with a hole in it reads as cover it does not give.
    linked = route_name is not None
    return ActivitySummary(
        id=activity.id,
        name=activity.name,
        started_at=activity.started_at,
        elapsed_time_s=activity.elapsed_time_s,
        moving_time_s=activity.moving_time_s,
        distance_m=activity.distance_m,
        ascent_m=activity.ascent_m,
        descent_m=activity.descent_m,
        source=activity.source,
        created_at=activity.created_at,
        route_id=activity.route_id if linked else None,
        route_name=route_name,
        match_confidence=activity.match_confidence if linked else None,
        match_locked=activity.match_locked,
    )


async def _detail(activity: Activity, db: DbDep) -> ActivityDetail:
    """The summary plus the geometry, read back out of PostGIS.

    The trace is encoded as polyline6 on the way out, which is what the
    planner already decodes for route legs - so an activity draws through the
    same path as everything else on the map.
    """
    wkt = await db.scalar(select(ST_AsText(Activity.geom)).where(Activity.id == activity.id))
    route_name = None
    if activity.route_id is not None:
        # Owner-filtered too - see list_activities' own comment.
        route_name = await db.scalar(
            select(Route.name).where(
                Route.id == activity.route_id, Route.user_id == activity.user_id
            )
        )
    return ActivityDetail(
        **_summary(activity, route_name).model_dump(),
        shape=encode_polyline6(coords_from_wkt(wkt)),
        elevation=[ElevationPoint(**point) for point in activity.elevation],
    )
