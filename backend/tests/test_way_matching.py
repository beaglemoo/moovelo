"""services/way_matching.py against real Postgres: matching one activity,
the ride_count/first_ridden_at upsert semantics, and the queue's own job
bookkeeping for the backfill button.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Activity, ActivityWay, User
from app.services.activities import activity_from_track, name_from_filename
from app.services.importer import parse_route_file
from app.services.valhalla import ValhallaClient
from app.services.way_matching import WayMatchQueue, match_activity
from tests.test_activities_api import GPX_RIDE

BASE = "http://valhalla.test"


async def _user(db: AsyncSession, email: str = "rider@example.com") -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _activity(db: AsyncSession, user: User) -> Activity:
    track = parse_route_file("ride.gpx", GPX_RIDE)
    activity = activity_from_track(
        track, user_id=user.id, fallback_name=name_from_filename("ride.gpx")
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return activity


@respx.mock
async def test_match_activity_records_ways_above_the_minimum_length(db: AsyncSession) -> None:
    respx.post(f"{BASE}/trace_attributes").respond(
        json={
            "units": "kilometers",
            "edges": [
                {"way_id": 111, "length": 0.1},  # 100m - counted
                {"way_id": 222, "length": 0.05},  # 50m - counted
                {"way_id": 333, "length": 0.002},  # 2m - a chunk-boundary sliver, excluded
            ],
        }
    )
    user = await _user(db)
    activity = await _activity(db, user)

    credited = await match_activity(db, ValhallaClient(base_url=BASE), activity)
    await db.commit()
    await db.refresh(activity)

    assert credited == 2
    assert activity.ways_matched_at is not None
    rows = (
        (await db.execute(select(ActivityWay).where(ActivityWay.user_id == user.id)))
        .scalars()
        .all()
    )
    assert {row.way_id for row in rows} == {111, 222}
    assert all(row.ride_count == 1 for row in rows)


@respx.mock
async def test_a_matching_failure_leaves_the_activity_intact_but_marks_the_attempt(
    db: AsyncSession,
) -> None:
    """The worst thing this feature could do is lose a ride over a routing
    engine's opinion of it. A track Valhalla cannot place must still read
    back exactly as imported - only ways_matched_at moves."""
    respx.post(f"{BASE}/trace_attributes").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    user = await _user(db)
    activity = await _activity(db, user)
    original_name = activity.name
    original_distance = activity.distance_m

    credited = await match_activity(db, ValhallaClient(base_url=BASE), activity)
    await db.commit()
    await db.refresh(activity)

    assert credited == 0
    assert activity.name == original_name
    assert activity.distance_m == original_distance
    assert activity.ways_matched_at is not None
    rows = (
        (await db.execute(select(ActivityWay).where(ActivityWay.user_id == user.id)))
        .scalars()
        .all()
    )
    assert rows == []


@respx.mock
async def test_an_unreachable_engine_also_leaves_the_activity_intact(db: AsyncSession) -> None:
    respx.post(f"{BASE}/trace_attributes").mock(side_effect=httpx.ConnectError("refused"))
    user = await _user(db)
    activity = await _activity(db, user)

    credited = await match_activity(db, ValhallaClient(base_url=BASE), activity)
    await db.commit()
    await db.refresh(activity)

    assert credited == 0
    assert activity.ways_matched_at is not None


@respx.mock
async def test_riding_the_same_way_twice_increments_ride_count_and_keeps_the_first_date(
    db: AsyncSession,
) -> None:
    respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [{"way_id": 111, "length": 0.1}]}
    )
    user = await _user(db)
    valhalla = ValhallaClient(base_url=BASE)

    first_ride = await _activity(db, user)
    await match_activity(db, valhalla, first_ride)
    await db.commit()
    row = (
        await db.execute(
            select(ActivityWay).where(ActivityWay.user_id == user.id, ActivityWay.way_id == 111)
        )
    ).scalar_one()
    first_seen = row.first_ridden_at

    second_ride = await _activity(db, user)
    await match_activity(db, valhalla, second_ride)
    await db.commit()
    # The upsert runs as Core, bypassing the ORM's identity map entirely, so
    # the session's own cached copy of this row (loaded by the first select
    # above) would otherwise silently mask the update it never heard about.
    # populate_existing forces this one query to refresh it; expiring the
    # whole session would also expire `user`, and touching that outside an
    # await is its own SQLAlchemy trap.
    row = (
        await db.execute(
            select(ActivityWay)
            .where(ActivityWay.user_id == user.id, ActivityWay.way_id == 111)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    assert row.ride_count == 2
    assert row.first_ridden_at == first_seen


@respx.mock
async def test_backfill_processes_only_activities_never_attempted(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """activity_ids=None - the backfill button's request - must reach every
    activity with ways_matched_at still null and nothing else."""
    # The queue opens its own sessions outside any request, via the module-
    # level session_factory - point it at the same throwaway database the
    # `db` fixture uses, the same way test_strava_archive.py's `_run` helper
    # does for the archive import worker.
    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [{"way_id": 999, "length": 0.1}]}
    )
    user = await _user(db)
    already_attempted = await _activity(db, user)
    already_attempted.ways_matched_at = datetime.now(UTC)
    pending = await _activity(db, user)
    await db.commit()

    # Constructed directly rather than via start(), which would also spawn
    # the background _run task - that task and this direct call would both
    # try to consume the same queued item, exactly the race
    # test_strava_archive.py's own `_run` helper avoids the same way.
    queue = WayMatchQueue()
    queue._valhalla = ValhallaClient(base_url=BASE)  # noqa: SLF001
    job = queue.submit(user.id)
    await queue._process(job.id, user.id, None)  # noqa: SLF001

    assert job.status == "done"
    assert job.total == 1
    assert job.matched == 1
    assert job.unmatched == 0

    async with db_factory() as fresh:
        rows = (
            (await fresh.execute(select(ActivityWay).where(ActivityWay.user_id == user.id)))
            .scalars()
            .all()
        )
        assert {row.way_id for row in rows} == {999}
        pending_row = await fresh.get(Activity, pending.id)
        already_row = await fresh.get(Activity, already_attempted.id)
        assert pending_row is not None and pending_row.ways_matched_at is not None
        assert already_row is not None and already_row.ways_matched_at is not None


@respx.mock
async def test_backfill_with_explicit_ids_attempts_even_a_fresh_import(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list of ids - what both import endpoints submit - is attempted
    regardless of ways_matched_at, since a fresh import is always null
    anyway and there is nothing to filter."""
    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    respx.post(f"{BASE}/trace_attributes").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    user = await _user(db)
    activity = await _activity(db, user)

    queue = WayMatchQueue()
    queue._valhalla = ValhallaClient(base_url=BASE)  # noqa: SLF001
    job = queue.submit(user.id, [activity.id])
    await queue._process(job.id, user.id, [activity.id])  # noqa: SLF001

    assert job.status == "done"
    assert job.total == 1
    assert job.matched == 0
    assert job.unmatched == 1


def test_jobs_cannot_grow_past_the_tracked_cap() -> None:
    """When every tracked job is still queued/running, _remember refuses a
    new one rather than inserting anyway - the archive queue's behaviour.
    Before the fix, `break`-and-insert let _jobs overshoot MAX_TRACKED_JOBS
    (observed 74 tracked against the 50 cap under a burst)."""
    import uuid

    from app.services.activity_import import QueueFullError
    from app.services.way_matching import MAX_TRACKED_JOBS

    queue = WayMatchQueue()
    user = uuid.uuid4()
    # Submitting only ever tracks; the worker is never started, so nothing is
    # ever marked done/error and nothing is evictable.
    for _ in range(MAX_TRACKED_JOBS):
        queue.submit(user)
    assert len(queue._jobs) == MAX_TRACKED_JOBS  # noqa: SLF001

    with pytest.raises(QueueFullError):
        queue.submit(user)
    # And it did not sneak in past the cap.
    assert len(queue._jobs) == MAX_TRACKED_JOBS  # noqa: SLF001


def test_a_rederive_is_untracked_so_it_can_never_be_evicted() -> None:
    """A delete's re-derive is fire-and-forget: never put in the bounded _jobs
    tracker, so neither a full tracker nor a later burst of deletes can evict
    it before the worker runs it. It is enqueued for the worker regardless.
    Tracking it once let a burst of ~50 subsequent deletes force-evict an
    earlier re-derive, silently stranding a deleted ride's credit; match jobs
    stay tracked and bounded."""
    import uuid

    from app.services.way_matching import MAX_TRACKED_JOBS

    queue = WayMatchQueue()
    user = uuid.uuid4()
    for _ in range(MAX_TRACKED_JOBS):
        queue.submit(user)  # fill the tracker with match jobs
    assert len(queue._jobs) == MAX_TRACKED_JOBS  # noqa: SLF001

    # Many re-derives, tracker already full: none raise, none are tracked (so
    # none can displace another), and every one is enqueued for the worker.
    before = queue._queue.qsize()  # noqa: SLF001
    ids = [queue.submit(user, clear_first=True).id for _ in range(MAX_TRACKED_JOBS + 5)]
    assert len(queue._jobs) == MAX_TRACKED_JOBS  # noqa: SLF001 - tracker unchanged
    assert all(rid not in queue._jobs for rid in ids)  # noqa: SLF001
    assert queue._queue.qsize() == before + len(ids)  # noqa: SLF001 - all enqueued


@respx.mock
async def test_two_jobs_cannot_double_credit_the_same_activity(db: AsyncSession) -> None:
    """A fresh import's own explicit match job and a concurrent backfill can
    both target the same activity while ways_matched_at is still null. Each
    upserts into activity_ways, so before the atomic claim the shared way's
    ride_count was incremented twice for a single ride. match_activity now
    claims the activity first: the second call finds it already claimed,
    returns None, and credits nothing."""
    respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [{"way_id": 999, "length": 0.5}]}
    )
    user = await _user(db)
    activity = await _activity(db, user)
    valhalla = ValhallaClient(base_url=BASE)

    first = await match_activity(db, valhalla, activity)
    await db.commit()
    second = await match_activity(db, valhalla, activity)
    await db.commit()

    assert first == 1  # claimed and credited
    assert second is None  # already claimed - skipped, not re-credited

    row = (
        await db.execute(
            select(ActivityWay).where(ActivityWay.user_id == user.id, ActivityWay.way_id == 999)
        )
    ).scalar_one()
    assert row.ride_count == 1


async def test_the_claim_commits_before_valhalla_work_so_the_row_lock_is_not_held(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim UPDATE takes the activity's row lock. The old shape - claim,
    match, one commit at the end - held that lock across the whole Valhalla
    round trip (up to 30s per chunk), so a concurrent DELETE, manual route
    link or rename on that ride blocked for the duration. The claim must be
    committed before any network work: this probe runs *inside* the Valhalla
    call and proves both halves from a second session - the claim is already
    visible (committed), and a write to the same row succeeds inside a 1s
    lock_timeout instead of stalling behind the worker."""
    from sqlalchemy import text, update

    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id
    probe_ran: list[bool] = []

    async def probing_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        async with db_factory() as other:
            await other.execute(text("SET LOCAL lock_timeout = '1s'"))
            claimed_at = await other.scalar(
                select(Activity.ways_matched_at).where(Activity.id == act_id)
            )
            assert claimed_at is not None, "claim not committed before Valhalla work"
            # The write that used to block: raises OperationalError (lock
            # timeout) if the worker still holds the row lock.
            await other.execute(
                update(Activity).where(Activity.id == act_id).values(name="renamed mid-match")
            )
            await other.commit()
        probe_ran.append(True)
        return {999: 100.0}

    monkeypatch.setattr(ValhallaClient, "match_ways", probing_match_ways)
    credited = await match_activity(db, ValhallaClient(base_url=BASE), activity)

    assert probe_ran == [True]  # the contention actually happened
    assert credited == 1
    async with db_factory() as fresh:
        row = (await fresh.execute(select(Activity).where(Activity.id == act_id))).scalar_one()
        # The concurrent write survived the match's own commit.
        assert row.name == "renamed mid-match"
        assert row.ways_matched_at is not None


async def test_an_unexpected_failure_unclaims_so_a_backfill_can_retry(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committing the claim before the work makes it durable before the
    outcome is known. Valhalla failing to place a track is not this case
    (match_ways degrades that to None and the attempt is deliberately
    recorded); an UNEXPECTED exception is - and it must un-claim, or the
    ride reads as attempted forever and no backfill will ever retry it.

    The mid-flight assertion inside the probe is what makes this test
    non-vacuous: the OLD code also left ways_matched_at NULL after an
    exception, but for the opposite reason - the claim was simply never
    committed. Asserting from a second session that the claim IS already
    durable when the failure fires discriminates 'durable claim, then
    explicitly un-claimed' from 'no claim ever existed', so a wholesale
    revert of the mechanism fails here instead of passing by accident."""
    # The un-claim runs on a fresh session from the module-level
    # session_factory - point it at this test's throwaway database.
    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id

    async def exploding_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        async with db_factory() as other:
            claimed_at = await other.scalar(
                select(Activity.ways_matched_at).where(Activity.id == act_id)
            )
            assert claimed_at is not None, "claim not durable at the moment of failure"
        raise RuntimeError("bug in matching")

    monkeypatch.setattr(ValhallaClient, "match_ways", exploding_match_ways)

    with pytest.raises(RuntimeError, match="bug in matching"):
        await match_activity(db, ValhallaClient(base_url=BASE), activity)

    async with db_factory() as fresh:
        row = (await fresh.execute(select(Activity).where(Activity.id == activity.id))).scalar_one()
        assert row.ways_matched_at is None  # un-claimed: a backfill sees it again
        credited = (
            (await fresh.execute(select(ActivityWay).where(ActivityWay.user_id == user.id)))
            .scalars()
            .all()
        )
        assert credited == []


async def test_a_lost_commit_ack_after_a_durable_claim_still_unclaims(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim-durability commit can raise AFTER the server applied it -
    the connection dies as the COMMIT ack comes back. Review of this fix
    caught the try block starting one statement too late, so exactly that
    failure stranded the ride claimed-forever with no un-claim ever
    attempted: `ways_matched_at` set, zero coverage, invisible to every
    future backfill. The handler must cover the claim commit itself; the
    timestamp predicate makes the un-claim inert when the claim never
    landed and a reset when it did."""
    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id

    real_commit = AsyncSession.commit
    fault_fired: list[bool] = []

    async def ack_lost_commit(self: AsyncSession) -> None:
        # Real commit first - the claim genuinely lands server-side - then
        # the ack is "lost". Only the first commit fails, so the un-claim's
        # own commit inside the recovery handler works.
        await real_commit(self)
        if not fault_fired:
            fault_fired.append(True)
            raise ConnectionResetError("connection reset as the COMMIT ack arrived")

    monkeypatch.setattr(AsyncSession, "commit", ack_lost_commit)

    with pytest.raises(ConnectionResetError):
        await match_activity(db, ValhallaClient(base_url=BASE), activity)

    assert fault_fired == [True]  # the injected failure actually happened
    async with db_factory() as fresh:
        row = (await fresh.execute(select(Activity).where(Activity.id == act_id))).scalar_one()
        assert row.ways_matched_at is None  # un-claimed despite the durable claim


async def test_a_genuinely_dead_connection_still_unclaims_on_a_fresh_session(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ack-lost test above raises at the Python level over a healthy
    connection, and review round 2 proved that is not the same failure:
    when the connection genuinely dies (backend killed mid-flight), the
    handler's own db.rollback() raises, and an un-claim that shared the
    failed session never ran - the ride stranded exactly as before the fix.
    The un-claim therefore runs on a fresh session from session_factory.
    This test kills the caller session's real Postgres backend (via an
    independent raw asyncpg connection, bypassing the pool so the killer
    cannot be handed the victim's own connection) and asserts the fresh
    session still un-claims."""
    import asyncpg
    from sqlalchemy import text

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id
    killed: list[int] = []

    async def killing_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        # Hold a live connection and open transaction on the caller's
        # session, learn its backend pid, then kill that backend for real.
        pid = await db.scalar(text("select pg_backend_pid()"))
        url = db.get_bind().url
        raw = await asyncpg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            database=url.database,
        )
        try:
            assert await raw.fetchval("select pg_terminate_backend($1)", pid)
        finally:
            await raw.close()
        killed.append(pid)
        raise ConnectionResetError("connection reset (backend killed for real)")

    monkeypatch.setattr(ValhallaClient, "match_ways", killing_match_ways)
    with pytest.raises(ConnectionResetError):
        await match_activity(db, ValhallaClient(base_url=BASE), activity)

    assert len(killed) == 1  # the backend really was terminated
    async with db_factory() as fresh:
        row = (await fresh.execute(select(Activity).where(Activity.id == act_id))).scalar_one()
        assert row.ways_matched_at is None  # un-claimed despite the dead session


@respx.mock
async def test_rederive_rebuilds_coverage_from_the_survivors(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rides both credit way 999 (ride_count 2). Deleting one and running a
    clear_first re-derive on the worker must clear the aggregate, mark the
    survivor unmatched, and re-credit it - leaving ride_count 1, not the stale
    2 a per-(user, way) aggregate with no per-activity link would otherwise
    keep. The clear runs on this single worker (clear_first), never on a second
    session, so it cannot deadlock or race concurrent matching."""
    from sqlalchemy import delete

    from app.models import Activity

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [{"way_id": 999, "length": 0.5}]}
    )
    user = await _user(db)
    uid = user.id  # capture before any expire, so later reads never lazy-load
    valhalla = ValhallaClient(base_url=BASE)
    ride_a = await _activity(db, user)
    a_id = ride_a.id
    ride_b = await _activity(db, user)
    await match_activity(db, valhalla, ride_a)
    await db.commit()
    await match_activity(db, valhalla, ride_b)
    await db.commit()

    async with db_factory() as fresh:
        row = (
            await fresh.execute(
                select(ActivityWay).where(ActivityWay.user_id == uid, ActivityWay.way_id == 999)
            )
        ).scalar_one()
        assert row.ride_count == 2

    # Delete one ride, then run the re-derive the way delete_activity schedules
    # it: a clear_first job on the worker, which clears the aggregate + marks
    # every survivor unmatched + re-matches, all in the worker's own session.
    await db.execute(delete(Activity).where(Activity.id == a_id))
    await db.commit()

    queue = WayMatchQueue()
    queue._valhalla = valhalla  # noqa: SLF001
    job = queue.submit(uid, clear_first=True)
    await queue._process(job.id, uid, None, clear_first=True)  # noqa: SLF001

    async with db_factory() as fresh:
        rebuilt = (
            (await fresh.execute(select(ActivityWay).where(ActivityWay.user_id == uid)))
            .scalars()
            .all()
        )
        # Only the surviving ride's way, credited exactly once.
        assert {(r.way_id, r.ride_count) for r in rebuilt} == {(999, 1)}


async def test_a_hanging_rollback_cannot_delay_the_unclaim(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3's finding: a rollback that HANGS rather than raises (a
    one-sided partition - no RST ever reaches the client, and nothing in
    the stack sets a timeout) is a different failure from round 2's dead
    connection, and sequencing the fresh-session un-claim after the
    rollback kept the ride stranded for as long as the hang lasted. The
    un-claim now runs first, so it must have landed even while the
    caller's rollback is still hanging."""
    import asyncio

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id

    async def exploding_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        raise RuntimeError("bug in matching")

    hang_entered = asyncio.Event()

    async def hanging_rollback(self: AsyncSession) -> None:
        hang_entered.set()
        await asyncio.Event().wait()  # a socket read that never returns

    monkeypatch.setattr(ValhallaClient, "match_ways", exploding_match_ways)
    monkeypatch.setattr(AsyncSession, "rollback", hanging_rollback)

    task = asyncio.ensure_future(match_activity(db, ValhallaClient(base_url=BASE), activity))
    try:
        await asyncio.wait_for(hang_entered.wait(), timeout=5)
        assert not task.done()  # genuinely hung in the rollback

        # The un-claim must already be durable while the hang persists.
        async with db_factory() as fresh:
            row = (await fresh.execute(select(Activity).where(Activity.id == act_id))).scalar_one()
            assert row.ways_matched_at is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cancellation_mid_unclaim_still_unclaims(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4's finding: a cancellation (worker shutdown) landing during
    the un-claim's own awaits skipped its `except Exception`, stranded the
    ride with no log line, and replaced the original error with a bare
    CancelledError. The un-claim is now shielded: cancelling the caller
    mid-un-claim must still leave the ride un-claimed once the shielded
    task completes, while the CancelledError still reaches the caller."""
    import asyncio

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id

    async def exploding_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        raise RuntimeError("bug in matching")

    entered_unclaim_commit = asyncio.Event()
    release_unclaim_commit = asyncio.Event()
    real_commit = AsyncSession.commit

    async def pausing_commit(self: AsyncSession) -> None:
        # Only the recovery session pauses - the caller's own commits (the
        # claim) must land normally so there is a durable claim to reset.
        if self is not db:
            entered_unclaim_commit.set()
            await release_unclaim_commit.wait()
        await real_commit(self)

    monkeypatch.setattr(ValhallaClient, "match_ways", exploding_match_ways)
    monkeypatch.setattr(AsyncSession, "commit", pausing_commit)

    task = asyncio.ensure_future(match_activity(db, ValhallaClient(base_url=BASE), activity))
    await asyncio.wait_for(entered_unclaim_commit.wait(), timeout=5)
    task.cancel()  # shutdown lands exactly during the un-claim's commit
    release_unclaim_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded un-claim keeps running after the caller is gone - give
    # it a moment to finish rather than racing it.
    for _ in range(100):
        async with db_factory() as fresh:
            row = (await fresh.execute(select(Activity).where(Activity.id == act_id))).scalar_one()
            if row.ways_matched_at is None:
                break
        await asyncio.sleep(0.02)
    assert row.ways_matched_at is None  # un-claimed despite the cancellation


async def test_stop_awaits_an_in_flight_unclaim_before_returning(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5's finding: the shielded un-claim was an orphan task nobody
    awaited, and uvicorn's real shutdown (asyncio.run's Runner.close
    cancels every still-pending task) killed it mid-commit with no log -
    whether the ride stranded was a race on whether the COMMIT bytes had
    reached Postgres. An orderly shutdown must wait for in-flight
    un-claims: stop() may only return after the paused un-claim has
    landed."""
    import asyncio

    from app.services import way_matching

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id

    async def exploding_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        raise RuntimeError("bug in matching")

    entered = asyncio.Event()
    release = asyncio.Event()
    real_commit = AsyncSession.commit

    async def pausing_commit(self: AsyncSession) -> None:
        if self is not db:
            entered.set()
            await release.wait()
        await real_commit(self)

    monkeypatch.setattr(ValhallaClient, "match_ways", exploding_match_ways)
    monkeypatch.setattr(AsyncSession, "commit", pausing_commit)

    task = asyncio.ensure_future(match_activity(db, ValhallaClient(base_url=BASE), activity))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(way_matching._pending_unclaims) == 1  # noqa: SLF001 - tracked, not orphaned

    async def release_soon() -> None:
        await asyncio.sleep(0.1)
        release.set()

    releaser = asyncio.ensure_future(release_soon())
    await WayMatchQueue().stop()  # must block until the un-claim lands
    await releaser

    # No polling: if stop() really waited, the row is already un-claimed.
    async with db_factory() as fresh:
        row = (await fresh.execute(select(Activity).where(Activity.id == act_id))).scalar_one()
        assert row.ways_matched_at is None
    assert way_matching._pending_unclaims == set()  # noqa: SLF001


async def test_a_forcibly_killed_unclaim_is_never_silent(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the runner kills the un-claim anyway (the bounded shutdown wait
    expired, or a crashy teardown), the abort must leave a log line naming
    the ride - round 5 measured the old shape stranding with zero trace."""
    import asyncio

    from app.services import way_matching

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    user = await _user(db)
    activity = await _activity(db, user)

    async def exploding_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        raise RuntimeError("bug in matching")

    entered = asyncio.Event()
    release = asyncio.Event()
    real_commit = AsyncSession.commit

    async def pausing_commit(self: AsyncSession) -> None:
        if self is not db:
            entered.set()
            await release.wait()
        await real_commit(self)

    monkeypatch.setattr(ValhallaClient, "match_ways", exploding_match_ways)
    monkeypatch.setattr(AsyncSession, "commit", pausing_commit)

    task = asyncio.ensure_future(match_activity(db, ValhallaClient(base_url=BASE), activity))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (unclaim_task,) = way_matching._pending_unclaims  # noqa: SLF001
    with caplog.at_level("WARNING", logger="app.services.way_matching"):
        unclaim_task.cancel()  # the runner's direct second cancellation
        with pytest.raises(asyncio.CancelledError):
            await unclaim_task
    assert any("killed before its commit landed" in r.message for r in caplog.records)
    assert way_matching._pending_unclaims == set()  # noqa: SLF001


async def test_stop_is_bounded_by_one_shutdown_window_total(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6's finding: stop()'s two sequential waits each carried their
    own SHUTDOWN_WAIT_S, so the worst case was ~2x the documented bound -
    a worker whose cancellation landed inside the Valhalla call finishes
    only when its un-claim does, and a hung recovery commit burned both
    windows back to back (measured 0.404s against a 0.2s bound). A SIGTERM
    grace period sized off the constant would SIGKILL mid-second-wait. The
    two waits now share one deadline: with the worker hung in Valhalla and
    the recovery commit hung too, stop() must return in ~one window."""
    import asyncio

    from app.services import way_matching

    monkeypatch.setattr("app.services.way_matching.session_factory", db_factory)
    monkeypatch.setattr(way_matching, "SHUTDOWN_WAIT_S", 0.5)
    user = await _user(db)
    activity = await _activity(db, user)
    act_id = activity.id

    match_entered = asyncio.Event()
    never = asyncio.Event()
    release_commit = asyncio.Event()
    in_handler = False
    real_commit = AsyncSession.commit

    async def hanging_match_ways(self: ValhallaClient, shape: object) -> dict[int, float]:
        nonlocal in_handler
        in_handler = True  # every commit AFTER this point is the recovery's
        match_entered.set()
        await never.wait()  # the worker's cancellation is consumed HERE
        return {}

    async def maybe_hanging_commit(self: AsyncSession) -> None:
        if in_handler:
            await release_commit.wait()
        await real_commit(self)

    monkeypatch.setattr(ValhallaClient, "match_ways", hanging_match_ways)
    monkeypatch.setattr(AsyncSession, "commit", maybe_hanging_commit)

    queue = WayMatchQueue()
    await queue.start(ValhallaClient(base_url=BASE))
    try:
        queue.submit(user.id, [act_id])
        await asyncio.wait_for(match_entered.wait(), timeout=5)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await queue.stop()
        elapsed = loop.time() - t0
        # One shared window (0.5s), not two (1.0s). Generous slack both ways.
        assert 0.4 <= elapsed <= 0.8, elapsed
    finally:
        release_commit.set()
        never.set()
        pending = set(way_matching._pending_unclaims)  # noqa: SLF001
        if queue._worker is not None:  # noqa: SLF001
            pending.add(queue._worker)  # noqa: SLF001
        if pending:
            await asyncio.wait(pending, timeout=5)
