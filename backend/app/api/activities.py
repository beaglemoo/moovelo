"""Rides that happened: import, list, read, delete.

No update endpoint, deliberately. An activity is a record of what happened,
and letting it be rewritten would make the heatmap and coverage built from it
mean nothing in particular. Renaming is the one thing a rider plausibly wants
and is not worth the ambiguity yet.
"""

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from geoalchemy2.functions import ST_AsText
from sqlalchemy import delete, func, select

from app.api.deps import DbDep, UserDep
from app.models import Activity
from app.schemas import ActivityDetail, ActivitySummary, ArchiveImportStatus, ElevationPoint
from app.services.activities import activity_from_track, name_from_filename
from app.services.activity_import import MAX_ARCHIVE_BYTES, ImportJob
from app.services.activity_import import queue as archive_queue
from app.services.importer import MAX_FILE_BYTES, RouteImportError, parse_route_file
from app.services.polyline import encode_polyline6

router = APIRouter(prefix="/api/activities", tags=["activities"])

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

    data = await _read_capped(file, MAX_ARCHIVE_BYTES)
    return _job(archive_queue.submit(user.id, filename, data))


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
        shape=encode_polyline6(_coords(wkt)),
        elevation=[ElevationPoint(**point) for point in activity.elevation],
    )


def _coords(wkt: str | None) -> list[tuple[float, float]]:
    """lat, lon pairs from a PostGIS LINESTRING, which stores them lon-first."""
    if not wkt or "(" not in wkt:
        return []
    inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    coords = []
    for pair in inner.split(","):
        parts = pair.split()
        if len(parts) >= 2:
            coords.append((float(parts[1]), float(parts[0])))
    return coords
