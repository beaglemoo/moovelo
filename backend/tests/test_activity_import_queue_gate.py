"""What the queue-full gate on the archive endpoint does, and what it does not.

Before this gate, import_archive() called `_read_capped(file,
MAX_ARCHIVE_BYTES)` - buffering up to 500 MB into a bytes object - before
`archive_queue.submit()` ever consulted whether the queue had room. The
MAX_QUEUED_ARCHIVES cap exists precisely to bound how much archive data sits
in memory at once (see activity_import.py's own module docstring: "5 * 500 MB
is already a generous 2.5 GB"), so checking occupancy only *after* that copy
bounded nothing - the memory was already spent.

The gate closes that. It does NOT, despite what this file and the endpoint
both used to claim, refuse "before reading the body": FastAPI resolves the
`file: UploadFile` parameter by parsing the entire multipart body and
spooling it before the endpoint function runs at all, so by the time any
line inside import_archive() executes, the body has already been received.
`main.py`'s own `reject_oversized_uploads` middleware documents this exact
mechanism as the reason a size check had to live in middleware - and the
gate, sitting in the endpoint, cannot escape it.

So both facts are pinned below: the in-memory copy is genuinely skipped, and
the body is genuinely already spooled. The second test exists so nobody
re-derives the comfortable, wrong half of that from the first one's name.
"""

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
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


async def test_a_saturated_queue_refuses_without_the_in_memory_copy(
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
        "_read_capped ran even though the queue already had no room - gating on "
        "queue.full() exists to skip exactly that copy"
    )


class _CountingApp:
    """Counts the bytes handed to the application over the ASGI receive
    channel, which is where the real cost of a request body actually shows
    up - not in whatever the endpoint later chooses to do with it."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.body_bytes = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def counting_receive() -> Any:
            message = await receive()
            if message["type"] == "http.request":
                self.body_bytes += len(message.get("body", b""))
            return message

        await self._app(scope, counting_receive, send)


async def test_the_body_is_already_spooled_by_the_time_the_gate_runs(
    client: AsyncClient,
) -> None:
    """The half the gate cannot do, stated so it is not assumed away.

    A saturated queue still receives the whole upload before answering 429,
    because FastAPI must parse the multipart body to produce the UploadFile
    the endpoint signature asks for. Refusing genuinely before the read would
    have to happen in middleware, as the size cap does.
    """
    for i in range(MAX_QUEUED_ARCHIVES):
        archive_queue.submit(uuid.uuid4(), f"a{i}.zip", b"x")
    await register(client)

    counting = _CountingApp(app)
    payload = b"x" * (2 * 1024 * 1024)
    async with AsyncClient(
        transport=ASGITransport(app=counting),
        base_url="http://test",
        cookies=client.cookies,
    ) as http:
        response = await http.post(
            "/api/activities/import/archive",
            files={"file": ("export.zip", payload, "application/zip")},
        )

    assert response.status_code == 429
    assert counting.body_bytes >= len(payload), (
        "if this ever drops below the payload size the gate has genuinely moved "
        "ahead of the body read, and the endpoint's comment should say so"
    )
