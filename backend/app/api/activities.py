"""Rides that happened: import, list, read, delete.

No update endpoint, deliberately. An activity is a record of what happened,
and letting it be rewritten would make the heatmap and coverage built from it
mean nothing in particular. Renaming is the one thing a rider plausibly wants
and is not worth the ambiguity yet.
"""

import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import Response
from geoalchemy2.functions import ST_AsText
from sqlalchemy import delete, exists, func, select

from app.api.deps import DbDep, UserDep
from app.models import Activity
from app.schemas import (
    ActivityDetail,
    ActivitySummary,
    ArchiveImportStatus,
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
from app.services.geo import coords_from_wkt
from app.services.heatmap import MAX_HEATMAP_ZOOM, MIN_HEATMAP_ZOOM, heatmap_etag, heatmap_tile
from app.services.importer import MAX_FILE_BYTES, RouteImportError, parse_route_file
from app.services.polyline import encode_polyline6
from app.services.way_matching import queue as match_queue
from app.services.way_matching import rederive_user_coverage

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
    query = select(Activity).where(Activity.user_id == user.id)
    if year is not None:
        query = query.where(func.extract("year", Activity.started_at) == year)
    if source is not None:
        query = query.where(Activity.source == source)

    # Rides with no timestamp sort by when they were imported rather than
    # falling to the bottom of the list forever.
    ordering = func.coalesce(Activity.started_at, Activity.created_at)
    rows = (await db.execute(query.order_by(ordering.desc()))).scalars().all()
    return [_summary(activity) for activity in rows]


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
    # is gone. Clear the rider's aggregate and re-derive it from what remains.
    await rederive_user_coverage(db, user.id)
    try:
        match_queue.submit(user.id)
    except QueueFullError:
        # The reset has committed, so coverage now reads empty; a rider can
        # rebuild it with the backfill button. Better than leaving the stale,
        # over-credited aggregate in place.
        logger.warning("coverage re-derive queue full after delete; user %s can backfill", user.id)


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


def _summary(activity: Activity) -> ActivitySummary:
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
    )


async def _detail(activity: Activity, db: DbDep) -> ActivityDetail:
    """The summary plus the geometry, read back out of PostGIS.

    The trace is encoded as polyline6 on the way out, which is what the
    planner already decodes for route legs - so an activity draws through the
    same path as everything else on the map.
    """
    wkt = await db.scalar(select(ST_AsText(Activity.geom)).where(Activity.id == activity.id))
    return ActivityDetail(
        **_summary(activity).model_dump(),
        shape=encode_polyline6(coords_from_wkt(wkt)),
        elevation=[ElevationPoint(**point) for point in activity.elevation],
    )
