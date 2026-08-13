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


def test_a_rederive_always_schedules_even_when_the_tracker_is_full() -> None:
    """A re-derive (a delete's clear_first job) is correctness-critical: it
    must schedule even when the tracker is full of queued match jobs,
    displacing one rather than raising. Dropping it would strand a deleted
    ride's coverage credit with no recovery, since the backfill button only
    matches unmatched rides and cannot remove a credit whose activity is gone
    - which was the false claim in delete_activity's own comment."""
    import uuid

    from app.services.way_matching import MAX_TRACKED_JOBS

    queue = WayMatchQueue()
    user = uuid.uuid4()
    for _ in range(MAX_TRACKED_JOBS):
        queue.submit(user)  # fill the tracker with queued match jobs
    assert len(queue._jobs) == MAX_TRACKED_JOBS  # noqa: SLF001

    # Does not raise - a queued match is displaced to make room.
    rederive = queue.submit(user, clear_first=True)
    assert len(queue._jobs) == MAX_TRACKED_JOBS  # noqa: SLF001 - cap still holds
    assert rederive.id in queue._jobs  # noqa: SLF001 - and the re-derive is tracked


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
