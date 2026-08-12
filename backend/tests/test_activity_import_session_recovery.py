"""Whether a failed commit inside the archive worker's per-entry loop leaves
the AsyncSession usable for the entries after it.

test_strava_archive.py's own
test_a_row_that_fails_at_commit_costs_one_ride_not_the_batch already proves
this for a poison entry in the middle of a three-file archive (good, broken,
good) - both neighbours land. This file exists to answer the brief's exact
question directly rather than by inference from one position: the same
check, run with the poison entry FIRST, MIDDLE and LAST in turn, against real
Postgres, so "does one failed flush poison every ride after it" has a proven
answer at every position rather than an assumption extrapolated from the
middle case.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Activity, User
from app.services import activity_import
from app.services.activity_import import ArchiveImportQueue

GPX_TEMPLATE = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>{name}</name><trkseg>
    <trkpt lat="51.7955" lon="-0.6580"><ele>128.0</ele><time>2026-05-03T08:00:00Z</time></trkpt>
    <trkpt lat="51.8000" lon="-0.6580"><ele>150.0</ele><time>2026-05-03T08:01:00Z</time></trkpt>
    <trkpt lat="51.8045" lon="-0.6580"><ele>140.0</ele><time>2026-05-03T08:02:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""


def _gpx(name: str) -> bytes:
    return GPX_TEMPLATE.replace(b"{name}", name.encode())


def _zip(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in members.items():
            archive.writestr(filename, data)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _poison_one_entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make any entry named "poison" fail at commit rather than at parse -
    the same technique test_strava_archive.py's own commit-failure test uses
    (a value only the database will reject: source is String(16)), so this
    is independent of any particular parser bug and exercises the same
    failure the archive worker's per-entry try/except + rollback exists to
    contain.
    """
    real = activity_import.activity_from_track

    def poisoned(track, **kwargs):  # type: ignore[no-untyped-def]
        activity = real(track, **kwargs)
        if kwargs.get("fallback_name") == "poison":
            activity.source = "x" * 999
        return activity

    monkeypatch.setattr(activity_import, "activity_from_track", poisoned)
    yield


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    db_factory: async_sessionmaker[AsyncSession],
    job_user: uuid.UUID,
    data: bytes,
) -> activity_import.ImportJob:
    monkeypatch.setattr("app.services.activity_import.session_factory", db_factory)
    queue = ArchiveImportQueue()
    job = queue.submit(job_user, "export.zip", data)
    await queue._process(job.id, data)
    return job


@pytest.mark.parametrize(
    "members",
    [
        pytest.param(
            {"poison.gpx": _gpx("poison"), "a.gpx": _gpx("a"), "b.gpx": _gpx("b")},
            id="poison-first",
        ),
        pytest.param(
            {"a.gpx": _gpx("a"), "poison.gpx": _gpx("poison"), "b.gpx": _gpx("b")},
            id="poison-middle",
        ),
        pytest.param(
            {"a.gpx": _gpx("a"), "b.gpx": _gpx("b"), "poison.gpx": _gpx("poison")},
            id="poison-last",
        ),
    ],
)
async def test_a_poisoned_entry_never_takes_its_neighbours_with_it(
    members: dict[str, bytes],
    db: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever position the failing commit sits at, both of its neighbours
    - before it and after it - must still land. The archive worker commits
    per entry and rolls back on SQLAlchemyError specifically so a failed
    flush ends its own transaction rather than the session; this proves that
    is actually true against real Postgres rather than assumed from how
    AsyncSession.rollback() is documented to behave.
    """
    user = User(email="rider@example.com", password_hash="x")
    db.add(user)
    await db.commit()

    job = await _run(monkeypatch, db_factory, user.id, _zip(members))

    assert job.status == "done"
    assert (job.imported, job.failed) == (2, 1)
    assert job.problems and "poison.gpx" in job.problems[0]

    stored = (await db.execute(select(Activity.name).order_by(Activity.name))).scalars().all()
    # Both good rides landed, wherever the poison sat relative to them - not
    # merely "the count is right", but the actual rows naming the actual
    # rides, which is what would go missing first if the session were left
    # unusable after the failed commit.
    assert stored == ["a", "b"]
