"""Reading a Strava bulk-export archive.

The caps are the security surface of this feature - the file arrives from a
third party by definition - so each one is tripped on its own rather than
assumed to work because the others do.
"""

import io
import uuid
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Activity, User
from app.services import activity_import
from app.services.activity_import import (
    MAX_ENTRIES,
    MAX_QUEUED_ARCHIVES,
    MAX_TRACKED_JOBS,
    ArchiveError,
    ArchiveImportQueue,
    ImportJob,
    QueueFullError,
    is_ride,
    list_entries,
    read_manifest,
)
from tests.conftest import register

GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>From the file</name><trkseg>
    <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele><time>2026-05-03T08:00:00Z</time></trkpt>
    <trkpt lat="51.8000" lon="-0.6580"><ele>150.0</ele><time>2026-05-03T08:01:00Z</time></trkpt>
    <trkpt lat="51.8045" lon="-0.6580"><ele>140.0</ele><time>2026-05-03T08:02:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""

MANIFEST = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Filename\n"
    '4821990112,"3 May 2026, 08:00:00",Morning Ride,Ride,activities/4821990112.gpx\n'
    '4821990113,"4 May 2026, 08:00:00",Parkrun,Run,activities/4821990113.gpx\n'
)


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _export() -> bytes:
    return _zip(
        {
            "activities.csv": MANIFEST.encode(),
            "activities/4821990112.gpx": GPX,
            "activities/4821990113.gpx": GPX,
        }
    )


# --- Reading the archive ----------------------------------------------------


def test_reads_the_manifest_and_keeps_only_rides() -> None:
    entries, skipped = list_entries(_export())
    assert [entry.path for entry in entries] == ["activities/4821990112.gpx"]
    assert [entry.name for entry in entries] == ["Morning Ride"]
    assert skipped == 1, "the run is skipped, and counted rather than dropped in silence"


def test_a_zip_without_a_manifest_takes_every_track() -> None:
    """A hand-assembled zip of GPX files is a legitimate thing to upload."""
    entries, skipped = list_entries(_zip({"one.gpx": GPX, "two.gpx": GPX}))
    assert len(entries) == 2
    assert skipped == 0


def test_ignores_everything_that_is_not_a_track() -> None:
    entries, _ = list_entries(_zip({"profile.json": b"{}", "media/photo.jpg": b"x", "a.gpx": GPX}))
    assert [entry.path for entry in entries] == ["a.gpx"]


def test_reads_gzipped_members() -> None:
    entries, _ = list_entries(_zip({"activities/4821990112.fit.gz": b"ignored"}))
    assert [entry.path for entry in entries] == ["activities/4821990112.fit.gz"]


@pytest.mark.parametrize(
    "activity_type,expected",
    [
        ("Ride", True),
        ("Gravel Ride", True),
        ("Virtual Ride", True),
        ("Mountain Bike Ride", True),
        ("E-Bike Ride", True),
        ("Run", False),
        ("Swim", False),
        ("Walk", False),
        # Unknown or untranslated types are ridden rather than lost.
        ("", True),
        (None, True),
        ("Fietsen", False),
    ],
)
def test_which_activity_types_count_as_riding(activity_type: str | None, expected: bool) -> None:
    assert is_ride(activity_type) is expected


# --- The caps, each tripped on its own --------------------------------------


def test_a_traversal_entry_is_not_read() -> None:
    entries, _ = list_entries(_zip({"../../etc/passwd.gpx": GPX, "fine.gpx": GPX}))
    assert [entry.path for entry in entries] == ["fine.gpx"]


def test_an_absolute_path_is_not_read() -> None:
    entries, _ = list_entries(_zip({"/etc/passwd.gpx": GPX, "fine.gpx": GPX}))
    assert [entry.path for entry in entries] == ["fine.gpx"]


def test_too_many_entries_is_refused() -> None:
    data = _zip({f"activities/{i}.gpx": b"x" for i in range(MAX_ENTRIES + 1)})
    with pytest.raises(ArchiveError, match="more than"):
        list_entries(data)


def test_a_member_that_expands_past_the_per_file_limit_is_refused() -> None:
    """The bomb case. A tiny compressed member expanding to hundreds of
    megabytes is caught partway through reading, not after it is all in
    memory - and not by trusting the size the archive claims for itself."""
    bomb = b"\0" * (25 * 1024 * 1024)
    archive = zipfile.ZipFile(io.BytesIO(_zip({"activities/big.gpx": bomb})))
    with pytest.raises(ArchiveError, match="per-file limit"):
        activity_import._read_entry(archive, "activities/big.gpx", activity_import.MAX_FILE_BYTES)


def test_a_member_with_a_lying_size_header_fails_as_one_ride() -> None:
    """ZipInfo.file_size is the archive's own claim, and understating it does
    not buy anything: zipfile stops at the claimed length and refuses the
    member on a CRC mismatch. What matters is that the refusal surfaces as an
    ArchiveError - a raw BadZipFile would take the whole job down over one
    corrupt file."""
    data = _zip({"activities/big.gpx": GPX})
    archive = zipfile.ZipFile(io.BytesIO(data))
    archive.getinfo("activities/big.gpx").file_size = 10  # a hostile archive would
    with pytest.raises(ArchiveError, match="could not be read"):
        activity_import._read_entry(archive, "activities/big.gpx", activity_import.MAX_FILE_BYTES)


def test_something_that_is_not_a_zip_says_so() -> None:
    with pytest.raises(ArchiveError, match="not a readable zip"):
        list_entries(b"this is not a zip file at all")


def test_a_manifest_too_large_to_read_does_not_fail_the_import() -> None:
    """Losing the manifest costs the ride names, not the ride."""
    data = _zip({"activities.csv": b"x" * (25 * 1024 * 1024), "a.gpx": GPX})
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert read_manifest(archive) == {}


# --- Importing ---------------------------------------------------------------


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    db_factory: async_sessionmaker[AsyncSession],
    job_user: uuid.UUID,
    data: bytes,
) -> activity_import.ImportJob:
    """Drive the worker directly.

    The queue's own task only runs under the app lifespan, which the test
    transport does not start - so the processing step is called by hand
    rather than polled for, which also makes the assertions deterministic.
    """
    monkeypatch.setattr("app.services.activity_import.session_factory", db_factory)
    queue = ArchiveImportQueue()
    job = queue.submit(job_user, "export.zip", data)
    await queue._process(job.id, data)
    return job


async def test_imports_the_rides_and_reports_what_it_skipped(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(email="rider@example.com", password_hash="x")
    db.add(user)
    await db.commit()

    job = await _run(monkeypatch, db_factory, user.id, _export())

    assert job.status == "done"
    assert (job.imported, job.skipped, job.failed) == (1, 1, 0)
    stored = (await db.execute(select(Activity))).scalars().all()
    assert len(stored) == 1
    # The manifest's name wins: it is what the rider actually called the ride.
    assert stored[0].name == "Morning Ride"
    assert stored[0].source == "strava_export"
    assert stored[0].source_ref == "4821990112"


async def test_a_second_overlapping_export_adds_only_what_is_new(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(email="rider@example.com", password_hash="x")
    db.add(user)
    await db.commit()

    await _run(monkeypatch, db_factory, user.id, _export())
    again = await _run(monkeypatch, db_factory, user.id, _export())

    assert (again.imported, again.duplicates) == (0, 1)
    assert await db.scalar(select(func.count()).select_from(Activity)) == 1


async def test_a_file_that_will_not_parse_costs_one_ride_not_the_import(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(email="rider@example.com", password_hash="x")
    db.add(user)
    await db.commit()

    job = await _run(
        monkeypatch, db_factory, user.id, _zip({"good.gpx": GPX, "broken.gpx": b"<gpx></gpx>"})
    )

    assert (job.imported, job.failed) == (1, 1)
    assert job.problems and "broken.gpx" in job.problems[0]
    assert await db.scalar(select(func.count()).select_from(Activity)) == 1


# NaN in a <ele> element is a real device/export glitch, not a hostile input:
# it passes float() and Pydantic without complaint, but the elevation
# profile is stored as JSONB and Postgres refuses the literal "NaN" token as
# invalid JSON. Before app/services/importer.py's own fix, that refusal
# reached this far - see test_a_row_that_fails_at_commit_costs_one_ride_not_
# the_batch below for the queue's side of the same defect, reproduced
# without relying on this one input closing over time.
GPX_NAN_ELEVATION = GPX.replace(b"<ele>128.0</ele>", b"<ele>NaN</ele>")


async def test_a_nan_elevation_ride_is_imported_without_its_elevation(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The importer now drops a non-finite elevation at parse time (see
    test_importer.py::test_nonfinite_elevation_is_dropped_not_stored), so a
    ride carrying one is a perfectly normal import - not a failure, and not
    a reason for its neighbours in the same archive to fail either."""
    user = User(email="rider@example.com", password_hash="x")
    db.add(user)
    await db.commit()

    job = await _run(
        monkeypatch,
        db_factory,
        user.id,
        _zip({"good1.gpx": GPX, "nan-elevation.gpx": GPX_NAN_ELEVATION, "good2.gpx": GPX}),
    )

    assert job.status == "done"
    assert (job.imported, job.failed) == (3, 0)
    assert await db.scalar(select(func.count()).select_from(Activity)) == 3


async def test_a_row_that_fails_at_commit_costs_one_ride_not_the_batch(
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The general case behind the NaN-elevation bug: SQLAlchemy batches
    pending inserts sharing a table into one multi-row INSERT, so *any* row
    Postgres refuses - not only a non-finite elevation, which the parser now
    catches on its own - takes every row queued alongside it down in the
    same statement unless each entry is committed before the next is added.

    Forces a value only the database will reject (source_ref is `Text` but
    the ORM's default `Activity` construction would never build a value that
    fails a column check on its own) by monkeypatching
    activity_from_track for one entry, so this is independent of any
    particular parser bug and exercises the queue's own containment.
    """
    import app.services.activity_import as activity_import

    real = activity_import.activity_from_track

    def poisoned(track, **kwargs):  # type: ignore[no-untyped-def]
        activity = real(track, **kwargs)
        if kwargs.get("fallback_name") == "broken":
            # source is String(16); Postgres enforces that at insert time
            # regardless of what the ORM's own type hints say.
            activity.source = "x" * 999
        return activity

    monkeypatch.setattr(activity_import, "activity_from_track", poisoned)

    user = User(email="rider@example.com", password_hash="x")
    db.add(user)
    await db.commit()

    job = await _run(
        monkeypatch,
        db_factory,
        user.id,
        _zip({"good1.gpx": GPX, "broken.gpx": GPX, "good2.gpx": GPX}),
    )

    assert job.status == "done"
    assert (job.imported, job.failed) == (2, 1)
    stored = await db.scalar(select(func.count()).select_from(Activity))
    # The counter must never claim more than what actually landed.
    assert stored == job.imported == 2


# --- The endpoints -----------------------------------------------------------


async def test_queues_an_archive_and_reports_progress(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/activities/import/archive",
        files={"file": ("export.zip", _export(), "application/zip")},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] in ("queued", "running", "done")

    status = await client.get(f"/api/activities/import/archive/{body['id']}")
    assert status.status_code == 200
    assert status.json()["filename"] == "export.zip"


async def test_a_zip_on_the_single_ride_endpoint_points_at_the_right_one(
    client: AsyncClient,
) -> None:
    await register(client)
    response = await client.post(
        "/api/activities/import", files={"file": ("export.zip", _export(), "application/zip")}
    )
    assert response.status_code == 400
    assert "archive" in response.json()["detail"]


async def test_a_single_ride_on_the_archive_endpoint_is_refused(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/activities/import/archive", files={"file": ("ride.gpx", GPX, "text/xml")}
    )
    assert response.status_code == 400


async def test_another_rider_cannot_poll_someone_elses_job(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    queued = await client.post(
        "/api/activities/import/archive",
        files={"file": ("export.zip", _export(), "application/zip")},
    )
    job_id = queued.json()["id"]
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    assert (await client.get(f"/api/activities/import/archive/{job_id}")).status_code == 404


async def test_polling_an_unknown_job_is_a_404(client: AsyncClient) -> None:
    await register(client)
    response = await client.get(f"/api/activities/import/archive/{uuid.uuid4()}")
    assert response.status_code == 404


# --- Backpressure -------------------------------------------------------------
#
# One worker processes one archive at a time, and every queued item carries
# the whole archive - up to MAX_ARCHIVE_BYTES. Before this, asyncio.Queue()
# had no maxsize, so nothing stopped a burst of submissions holding
# gigabytes resident with nothing to show for it. No worker task runs in
# these tests (submit() alone never drains the queue - see _run's own note
# above), so filling it to capacity is deterministic.


async def test_the_queue_refuses_once_it_is_full() -> None:
    queue = ArchiveImportQueue()
    for i in range(MAX_QUEUED_ARCHIVES):
        queue.submit(uuid.uuid4(), f"a{i}.zip", b"x")

    with pytest.raises(QueueFullError, match="already queued"):
        queue.submit(uuid.uuid4(), "one-too-many.zip", b"x")


async def test_the_archive_endpoint_reports_a_full_queue_as_429(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _always_full(*args: object, **kwargs: object) -> ImportJob:
        raise QueueFullError("no room right now")

    monkeypatch.setattr("app.api.activities.archive_queue.submit", _always_full)
    await register(client)

    response = await client.post(
        "/api/activities/import/archive",
        files={"file": ("export.zip", _export(), "application/zip")},
    )

    assert response.status_code == 429
    assert "no room right now" in response.json()["detail"]


def test_remember_refuses_rather_than_growing_past_the_tracked_cap() -> None:
    """The scenario _remember's own eviction loop cannot resolve on its own:
    every tracked slot occupied by a job that is still queued or running, so
    there is nothing finished to evict and make room for a new one. The old
    code fell through its `while` loop and inserted anyway, silently growing
    _jobs past MAX_TRACKED_JOBS - reproduced directly here since the queue's
    own bound (MAX_QUEUED_ARCHIVES, far below MAX_TRACKED_JOBS) now makes
    this unreachable through submit() in ordinary use.
    """
    queue = ArchiveImportQueue()
    for i in range(MAX_TRACKED_JOBS):
        job = ImportJob(id=uuid.uuid4(), user_id=uuid.uuid4(), filename=f"a{i}.zip")
        job.status = "running"
        queue._jobs[job.id] = job

    with pytest.raises(QueueFullError):
        queue._remember(ImportJob(id=uuid.uuid4(), user_id=uuid.uuid4(), filename="new.zip"))

    assert len(queue._jobs) == MAX_TRACKED_JOBS, (
        "the cap must hold even when nothing can be evicted"
    )


# --- The pre-read upload guard -----------------------------------------------
#
# app/main.py's reject_oversized_uploads middleware used to match on
# request.url.path.endswith("/import"), which /api/activities/import/archive
# does not - so the one route that most needs a tight pre-read cap (up to
# MAX_ARCHIVE_BYTES) got none at all, and an oversized POST there read its
# whole body before the endpoint's own cap ever got a say. One test per
# guarded route, matching test_import_api.py's own
# test_oversized_upload_is_refused_before_the_body_is_parsed for
# /api/routes/import.


async def test_the_single_ride_endpoint_is_refused_before_the_body_is_parsed(
    client: AsyncClient,
) -> None:
    await register(client)
    response = await client.post(
        "/api/activities/import",
        headers={
            "content-type": "multipart/form-data; boundary=x",
            "content-length": "2000000000",
        },
        content=b"",
    )
    assert response.status_code == 413
    assert "larger than" in response.json()["detail"]


async def test_the_archive_endpoint_is_refused_before_the_body_is_parsed(
    client: AsyncClient,
) -> None:
    await register(client)
    response = await client.post(
        "/api/activities/import/archive",
        headers={
            "content-type": "multipart/form-data; boundary=x",
            "content-length": "600000000000",
        },
        content=b"",
    )
    assert response.status_code == 413
    assert "larger than" in response.json()["detail"]
