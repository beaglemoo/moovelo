"""The archive upload endpoint must not pay the cost of reading a body it is
about to refuse anyway.

Before this file's fix, import_archive() called `_read_capped(file,
MAX_ARCHIVE_BYTES)` - buffering up to 500 MB in memory - before
`archive_queue.submit()` ever consulted whether the queue had room. The
MAX_QUEUED_ARCHIVES cap exists precisely to bound how much archive data sits
in memory at once (see activity_import.py's own module docstring: "5 * 500 MB
is already a generous 2.5 GB"), and a queue that is only checked *after* the
read does not bound anything - the memory is already spent by the time the
check runs. Gating the read on the queue's own occupancy closes the common
case (a queue already saturated by earlier jobs) without the read ever
starting.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.services.activity_import import MAX_QUEUED_ARCHIVES
from app.services.activity_import import queue as archive_queue
from tests.conftest import register


@pytest.fixture(autouse=True)
def _restore_queue_afterwards() -> None:
    """archive_queue is a process-wide singleton (app.services.activity_import.queue,
    imported by app.api.activities) - filling it to capacity here must not
    leak into whatever test runs next.
    """
    yield
    archive_queue._queue = type(archive_queue._queue)(maxsize=MAX_QUEUED_ARCHIVES)
    archive_queue._jobs.clear()


async def test_a_saturated_queue_refuses_before_reading_the_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(MAX_QUEUED_ARCHIVES):
        archive_queue.submit(uuid.uuid4(), f"a{i}.zip", b"x")

    read = False

    async def _spy_read_capped(*args: object, **kwargs: object) -> bytes:
        nonlocal read
        read = True
        return b"PK\x05\x06" + b"\x00" * 18  # an empty zip's End Of Central Directory

    monkeypatch.setattr("app.api.activities._read_capped", _spy_read_capped)
    await register(client)

    response = await client.post(
        "/api/activities/import/archive",
        files={"file": ("export.zip", b"anything, never read", "application/zip")},
    )

    assert response.status_code == 429
    assert not read, (
        "the body was read even though the queue already had no room for it - "
        "the whole point of gating on queue.full() is that this never happens"
    )
