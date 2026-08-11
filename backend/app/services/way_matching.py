"""Map-matching a rider's own recorded traces onto OSM way ids, for
cycle-network coverage.

One worker, following ArchiveImportQueue's shape (services/activity_import.py):
an in-process asyncio.Queue, an in-memory job with a pollable status, and a
startup/shutdown pair. A "job" here is not file bytes though - it is a rider
and, optionally, the specific activities to match - because both moments that
need this are the same operation: the instant an activity is imported, and an
explicit backfill button for everything imported before this feature existed.
`activity_ids=None` means "every activity for this user that has never been
attempted" (`Activity.ways_matched_at IS NULL`); a list means "these, whether
or not they were attempted before" - which is how a fresh import always
reaches the queue.

Matching runs off the request path entirely, for both the single-file and the
archive import endpoints: map_snap trace_attributes is a handful of Valhalla
round trips per ride, and an unreachable engine would otherwise hold a
rider's upload open for its full 30s-per-chunk timeout. Coverage simply lags
an import by however long the queue takes to reach it - the same trade the
Strava archive import already makes for parsing.
"""

import asyncio
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from geoalchemy2.functions import ST_AsText
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory
from app.models import Activity, ActivityWay
from app.services.geo import Point, coords_from_wkt
from app.services.valhalla import ValhallaClient

logger = logging.getLogger(__name__)

# A way credited off a single boundary-edge sliver (a metre or two, where a
# chunk happened to split mid-way) is noise, not evidence the rider actually
# rode it. Real ways are tens to hundreds of metres; this is well under the
# shortest of those while still well above what a chunk boundary can
# spuriously contribute.
MIN_MATCHED_WAY_LENGTH_M = 5.0

# Same bound as ArchiveImportQueue: a job is an id plus counters, and none of
# it survives a restart - so an interrupted job is reported as interrupted
# rather than silently resumed against nothing.
MAX_TRACKED_JOBS = 50


@dataclass
class MatchJob:
    id: uuid.UUID
    user_id: uuid.UUID
    status: str = "queued"  # queued | running | done | error
    total: int = 0
    # Activities credited with at least one ridden way.
    matched: int = 0
    # Activities processed but credited with nothing - Valhalla could not
    # place the track, or every candidate way fell under the minimum length.
    unmatched: int = 0
    error: str | None = None


async def match_activity(db: AsyncSession, valhalla: ValhallaClient, activity: Activity) -> int:
    """Match one activity's recorded trace, record what it touched, and mark
    the attempt. Returns how many ways were credited.

    A matching failure - the track sits outside the loaded map extract,
    Valhalla is still building tiles, or it is simply unreachable - must
    never touch the activity itself: the ride stays exactly as imported, and
    only `ways_matched_at` records that an attempt happened, so coverage for
    this ride reads as honestly unknown rather than wrongly zero.
    """
    wkt = await db.scalar(select(ST_AsText(Activity.geom)).where(Activity.id == activity.id))
    shape: list[Point] = coords_from_wkt(wkt)
    lengths = await valhalla.match_ways(shape) if len(shape) >= 2 else None
    way_ids = {
        way_id
        for way_id, length_m in (lengths or {}).items()
        if length_m >= MIN_MATCHED_WAY_LENGTH_M
    }
    if way_ids:
        await _record_ways(db, activity.user_id, way_ids)
    await db.execute(
        update(Activity).where(Activity.id == activity.id).values(ways_matched_at=datetime.now(UTC))
    )
    return len(way_ids)


async def _record_ways(db: AsyncSession, user_id: uuid.UUID, way_ids: set[int]) -> None:
    """Upsert one row per newly-ridden way, incrementing ride_count for ways
    already known. `first_ridden_at` is only ever set on insert - it is left
    out of the conflict update entirely, so an earlier ride keeps the credit
    for being first."""
    now = datetime.now(UTC)
    stmt = pg_insert(ActivityWay).values(
        [
            {"user_id": user_id, "way_id": way_id, "first_ridden_at": now, "ride_count": 1}
            for way_id in way_ids
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ActivityWay.user_id, ActivityWay.way_id],
        set_={"ride_count": ActivityWay.ride_count + 1},
    )
    await db.execute(stmt)


class WayMatchQueue:
    """One worker, one rider's batch at a time.

    A second worker buys nothing here, same reasoning as ArchiveImportQueue:
    the work is dominated by Valhalla round trips rather than CPU, but
    running two riders' batches concurrently would only interleave their
    logs for no throughput this queue is ever asked to provide at once.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[uuid.UUID, uuid.UUID, list[uuid.UUID] | None]] = (
            asyncio.Queue()
        )
        self._jobs: OrderedDict[uuid.UUID, MatchJob] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None
        self._valhalla: ValhallaClient | None = None

    async def start(self, valhalla: ValhallaClient) -> None:
        # Not owned here: main.py's lifespan owns the one ValhallaClient the
        # whole app shares, and closes it after every queue (this one
        # included) has been stopped.
        self._valhalla = valhalla
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        for job in self._jobs.values():
            if job.status in ("queued", "running"):
                job.status = "error"
                job.error = "Interrupted by a restart - try again."

    def submit(self, user_id: uuid.UUID, activity_ids: list[uuid.UUID] | None = None) -> MatchJob:
        """Queue a batch. `activity_ids=None` means every activity for this
        rider that has never been attempted - the backfill button's request;
        an explicit list is a fresh import, which is always worth attempting
        even though `ways_matched_at` is already null for it."""
        job = MatchJob(id=uuid.uuid4(), user_id=user_id)
        self._remember(job)
        self._queue.put_nowait((job.id, user_id, activity_ids))
        return job

    def get(self, job_id: uuid.UUID, user_id: uuid.UUID) -> MatchJob | None:
        job = self._jobs.get(job_id)
        return job if job is not None and job.user_id == user_id else None

    def _remember(self, job: MatchJob) -> None:
        while len(self._jobs) >= MAX_TRACKED_JOBS:
            finished = next(
                (i for i, j in self._jobs.items() if j.status in ("done", "error")), None
            )
            if finished is None:
                break
            del self._jobs[finished]
        self._jobs[job.id] = job

    async def _run(self) -> None:
        while True:
            job_id, user_id, activity_ids = await self._queue.get()
            try:
                await self._process(job_id, user_id, activity_ids)
            except Exception:  # noqa: BLE001 - the worker must never die
                logger.exception("Way match job %s crashed", job_id)
                job = self._jobs.get(job_id)
                if job is not None:
                    job.status = "error"
                    job.error = "Unexpected error while matching"
            finally:
                self._queue.task_done()

    async def _process(
        self, job_id: uuid.UUID, user_id: uuid.UUID, activity_ids: list[uuid.UUID] | None
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None or self._valhalla is None:
            return
        job.status = "running"

        async with session_factory() as db:
            query = select(Activity).where(Activity.user_id == user_id)
            query = (
                query.where(Activity.id.in_(activity_ids))
                if activity_ids is not None
                else query.where(Activity.ways_matched_at.is_(None))
            )
            activities = (await db.execute(query.order_by(Activity.created_at))).scalars().all()
            job.total = len(activities)

            # One commit per activity: an unreachable engine partway through
            # a large backfill still leaves everything matched so far in
            # place, rather than losing the whole batch to a rollback.
            for activity in activities:
                credited = await match_activity(db, self._valhalla, activity)
                if credited:
                    job.matched += 1
                else:
                    job.unmatched += 1
                await db.commit()

        job.status = "done"


queue = WayMatchQueue()
