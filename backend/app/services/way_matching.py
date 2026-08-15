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
from sqlalchemy import delete, select, update
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


async def match_activity(
    db: AsyncSession, valhalla: ValhallaClient, activity: Activity
) -> int | None:
    """Match one activity's recorded trace, record what it touched, and mark
    the attempt. Commits its own work. Returns how many ways were credited,
    or None if another job had already claimed this activity and we skipped
    it.

    Claims the activity atomically before crediting anything. Two jobs can
    target the same activity - a fresh import's own explicit match job and a
    concurrent backfill that enumerated it while `ways_matched_at` was still
    null - and each independently upserts into activity_ways, which
    double-increments ride_count on a durable coverage table (measured: 42
    activities, ride_count 43). The conditional UPDATE makes matching
    exactly-once: whoever's `WHERE ways_matched_at IS NULL` lands first sets
    the timestamp and proceeds; the loser's WHERE no longer matches, returns
    no row, and skips.

    The claim is COMMITTED before any Valhalla work. The claim UPDATE takes
    the activity's row lock, and the earlier shape - claim, then match, then
    one commit at the end - held that lock across the whole Valhalla round
    trip (up to 30s per chunk), blocking a concurrent DELETE, manual route
    link or rematch on that ride for the duration. Committing first releases
    the lock in milliseconds; the price is that the claim is now durable
    before the outcome is known, so an UNEXPECTED failure (a bug, a dropped
    connection - not Valhalla merely failing to place the track, which
    match_ways degrades to None) must explicitly un-claim, or the ride is
    stranded as attempted-forever and invisible to every future backfill.
    The un-claim is keyed on the exact claim timestamp this call wrote, so
    if anything else has since touched `ways_matched_at` (a re-derive on
    this same worker) it is inert rather than clobbering that write - and
    it runs on a FRESH session from session_factory, because the session
    that just failed may be exactly what is broken (review round 2 proved a
    genuinely dead connection made the handler's own rollback raise, and a
    recovery riding on the failed session never ran). Residual ways a ride
    can still strand as attempted-forever: a hard process crash between
    claim and un-claim, or the database being unreachable for the recovery
    session too (logged loudly). Both are the same residue an interrupted
    job has always left, recoverable by a delete-triggered re-derive; every
    ordinary exception path un-claims.

    A matching failure - the track sits outside the loaded map extract,
    Valhalla is still building tiles, or it is simply unreachable - must
    never touch the ride itself: it stays exactly as imported, and the claim
    that already marked `ways_matched_at` records that an attempt happened,
    so coverage for this ride reads as honestly unknown rather than wrongly
    zero.
    """
    # Captured into locals BEFORE any commit or rollback: the un-claim path
    # rolls the session back, which expires every loaded instance, and an
    # attribute access on an expired instance inside the very handler that
    # exists to recover is the round-7 import_activity bug over again.
    act_id = activity.id
    owner_id = activity.user_id

    claimed_at = datetime.now(UTC)
    claimed = await db.scalar(
        update(Activity)
        .where(Activity.id == act_id, Activity.ways_matched_at.is_(None))
        .values(ways_matched_at=claimed_at)
        .returning(Activity.id)
    )
    if claimed is None:
        # Nothing was written (the WHERE matched no row), but the statement
        # still opened a transaction - close it rather than leave the caller
        # holding a stale snapshot into its next iteration. Commit, not
        # rollback: rolling back expires every instance the caller's session
        # holds, and this function has no business trashing them over a no-op.
        await db.commit()
        return None

    try:
        # The try block starts HERE, directly after the claim UPDATE - review
        # of this fix proved that starting it after the commit below leaves a
        # commit-ack-lost failure (the server applied the COMMIT, the
        # connection died before the ack) stranding the ride claimed-forever
        # with no un-claim ever attempted. Inside the handler the un-claim's
        # timestamp predicate sorts the cases out: a claim that never landed
        # matches nothing (inert), a claim that landed gets reset.
        wkt = await db.scalar(select(ST_AsText(Activity.geom)).where(Activity.id == act_id))
        await db.commit()  # claim durable, row lock released - BEFORE any network work

        shape: list[Point] = coords_from_wkt(wkt)
        lengths = await valhalla.match_ways(shape) if len(shape) >= 2 else None
        way_ids = {
            way_id
            for way_id, length_m in (lengths or {}).items()
            if length_m >= MIN_MATCHED_WAY_LENGTH_M
        }
        if way_ids:
            await _record_ways(db, owner_id, way_ids)
        await db.commit()
        return len(way_ids)
    except BaseException:  # noqa: BLE001 - CancelledError must un-claim too
        # Roll the caller's session back first: on the one path where the
        # claim is still uncommitted (the wkt SELECT failed), this releases
        # the row lock the fresh-session un-claim below would otherwise
        # block on. It gets its own try because the session's connection may
        # be the very thing that failed - review round 2 killed the backend
        # for real and this rollback raised InternalClientError, and when it
        # shared a try with the un-claim, that swallowed exception was the
        # un-claim never running.
        try:
            await db.rollback()
        except Exception:
            logger.exception(
                "Rollback failed while recovering activity %s - continuing to the "
                "un-claim on a fresh session",
                act_id,
            )
        # The un-claim runs on its OWN session, never the one that just
        # failed - the route_match lesson: a genuinely dead connection
        # poisons every later statement on the same session, and a recovery
        # that depends on the thing being recovered from is not a recovery.
        try:
            async with session_factory() as recovery:
                await recovery.execute(
                    update(Activity)
                    .where(Activity.id == act_id, Activity.ways_matched_at == claimed_at)
                    .values(ways_matched_at=None)
                )
                await recovery.commit()
        except Exception:
            logger.exception(
                "Could not un-claim activity %s after a failed match - it will read "
                "as attempted with no coverage until a re-derive resets it",
                act_id,
            )
        raise


async def rederive_user_coverage(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Reset a rider's coverage so it can be rebuilt from their remaining
    activities. Commits.

    activity_ways is a per-(user, way) aggregate: ride_count is incremented
    per matched ride and there is no per-activity record of which rides
    contributed a given way. So a deleted ride's credit cannot be decremented
    directly - the honest way to reflect a deletion is to clear the rider's
    rows and mark every remaining ride unmatched, then re-credit from scratch.

    Called by _process for a clear_first job, on the single match-queue
    worker's own session, immediately before its match loop re-credits the
    survivors. It runs there rather than on the deleting request's session for
    a reason that cost a round of review: on a second session it deadlocked
    against the worker's concurrent per-activity claim/upsert, and its READ
    COMMITTED DELETE missed a mid-flight match's not-yet-committed credit,
    leaving a phantom over-count. On the worker there is no concurrent coverage
    writer, so neither can happen.

    This re-matches every one of the rider's remaining rides on each delete,
    the accepted cost of not storing per-activity way contributions.
    """
    await db.execute(delete(ActivityWay).where(ActivityWay.user_id == user_id))
    await db.execute(
        update(Activity).where(Activity.user_id == user_id).values(ways_matched_at=None)
    )
    await db.commit()


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
        # (job_id, user_id, activity_ids, clear_first). clear_first rebuilds a
        # rider's whole coverage: it runs on this single worker rather than on
        # a request session precisely so it cannot race a concurrent match -
        # see submit(clear_first=True) and rederive_user_coverage.
        self._queue: asyncio.Queue[tuple[uuid.UUID, uuid.UUID, list[uuid.UUID] | None, bool]] = (
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

    def submit(
        self,
        user_id: uuid.UUID,
        activity_ids: list[uuid.UUID] | None = None,
        clear_first: bool = False,
    ) -> MatchJob:
        """Queue a batch. `activity_ids=None` means every activity for this
        rider that has never been attempted - the backfill button's request;
        an explicit list is a fresh import, which is always worth attempting
        even though `ways_matched_at` is already null for it.

        `clear_first=True` (a delete's re-derive) additionally clears the
        rider's whole activity_ways aggregate and marks every ride unmatched
        before matching - a full rebuild. It runs here, on the one worker,
        rather than on the caller's request session, so it can never race the
        worker's own per-activity claim/upsert: doing the clear on a second
        session deadlocked against the worker and left phantom over-credits.
        It forces activity_ids to None (rebuild everything).

        A re-derive is NOT tracked in the bounded _jobs map. A match job is
        tracked so its status can be polled, and the bound keeps that map from
        growing without limit. A delete's re-derive is neither polled (the
        DELETE already returned 204) nor optional: tracking it once let a later
        burst of deletes force-evict it before the worker ran it, silently
        stranding a deleted ride's credit. Left off the map it can never be
        evicted - it is fire-and-forget on the worker, and the unbounded work
        queue (cheap tuples) always accepts it."""
        job = MatchJob(id=uuid.uuid4(), user_id=user_id)
        if not clear_first:
            self._remember(job)
        self._queue.put_nowait(
            (job.id, user_id, None if clear_first else activity_ids, clear_first)
        )
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
                # Mirror ArchiveImportQueue._remember: rather than fall through
                # and insert anyway - which let _jobs grow past
                # MAX_TRACKED_JOBS under a submission burst, since this queue's
                # own asyncio.Queue is unbounded and nothing upstream refuses
                # work - refuse so the cap actually holds. Imported here rather
                # than at module scope because activity_import imports this
                # module, so a top-level import would be a cycle.
                from app.services.activity_import import QueueFullError

                raise QueueFullError(
                    "Too many coverage jobs are already tracked. Try again in a few minutes."
                )
            del self._jobs[finished]
        self._jobs[job.id] = job

    async def _run(self) -> None:
        while True:
            job_id, user_id, activity_ids, clear_first = await self._queue.get()
            try:
                await self._process(job_id, user_id, activity_ids, clear_first)
            except Exception:  # noqa: BLE001 - the worker must never die
                logger.exception("Way match job %s crashed", job_id)
                job = self._jobs.get(job_id)
                if job is not None:
                    job.status = "error"
                    job.error = "Unexpected error while matching"
            finally:
                self._queue.task_done()

    async def _process(
        self,
        job_id: uuid.UUID,
        user_id: uuid.UUID,
        activity_ids: list[uuid.UUID] | None,
        clear_first: bool = False,
    ) -> None:
        if self._valhalla is None:
            return
        job = self._jobs.get(job_id)
        if job is None:
            # A tracked match job that was evicted (its status was polled while
            # newer jobs pushed it out) - its ride simply stays unmatched and a
            # backfill recovers it, so skip. A re-derive is deliberately never
            # tracked (see submit); it runs on a throwaway job, since nobody
            # polls it and it must never be skippable.
            if not clear_first:
                return
            job = MatchJob(id=job_id, user_id=user_id)
        job.status = "running"

        async with session_factory() as db:
            # A re-derive (after a delete) wipes the rider's aggregate and marks
            # every ride unmatched here, on the worker, so it serialises with
            # matching instead of racing it on a second session.
            if clear_first:
                await rederive_user_coverage(db, user_id)

            query = select(Activity).where(Activity.user_id == user_id)
            query = (
                query.where(Activity.id.in_(activity_ids))
                if activity_ids is not None
                else query.where(Activity.ways_matched_at.is_(None))
            )
            activities = (await db.execute(query.order_by(Activity.created_at))).scalars().all()
            job.total = len(activities)

            # match_activity commits per activity (claim first, credit after),
            # so an unreachable engine partway through a large backfill still
            # leaves everything matched so far in place, rather than losing
            # the whole batch to a rollback.
            for activity in activities:
                credited = await match_activity(db, self._valhalla, activity)
                # None means another job claimed it in the gap between this
                # job's SELECT and now - not ours to count either way.
                if credited is None:
                    job.total -= 1
                elif credited:
                    job.matched += 1
                else:
                    job.unmatched += 1

        job.status = "done"


queue = WayMatchQueue()
