"""Bulk import of rides from a Strava bulk-export archive.

Strava's own portability export, not their API. The API is not usable for
this: API Policy 6.2 caps retention of API data at seven days and explicitly
forbids a persistent index, with no exception for the user's own data, and
5.3 forbids use in connection with any AI application - extending to data
"derived from, aggregated from, anonymized from" it. A personal heatmap is
exactly the persistent index 6.2 names, and this app ships an assistant. The
export is the rider's own data handed to them, so neither clause attaches -
and it needs no registered app, client secret or subscription.

An archive is hundreds of files and each one costs seconds of CPU to parse,
so this runs on a worker rather than inside the request.
"""

import asyncio
import csv
import gzip
import io
import logging
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory
from app.models import Activity
from app.services.activities import activity_from_track, name_from_filename, strava_activity_id
from app.services.importer import MAX_FILE_BYTES, RouteImportError, parse_route_file
from app.services.way_matching import queue as match_queue

logger = logging.getLogger(__name__)

# A whole export, not a single ride. Large, but a decade of riding is a real
# archive and refusing it would defeat the point.
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
# Bounds on what the archive may expand to. Every one of these is checked
# while reading rather than after: a zip that only becomes too big once it is
# in memory has already done the damage.
MAX_ENTRIES = 20_000
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
DECOMPRESS_CHUNK = 256 * 1024

# Kept in memory and only in memory. A job is file bytes plus progress, and
# neither survives a restart - so an interrupted job is reported as
# interrupted rather than silently resumed against data that is gone.
MAX_TRACKED_JOBS = 50

# One worker processes one archive at a time, so anything past the one in
# flight is just sitting in memory waiting - and each item carries the whole
# archive, up to MAX_ARCHIVE_BYTES. Unbounded, a burst of submissions holds
# gigabytes resident with nothing to show for it but a longer queue; bounded,
# a submission past this refuses outright rather than accepting work it has
# nowhere to put. 5 * 500 MB is a already a generous 2.5 GB of ride data
# waiting its turn.
MAX_QUEUED_ARCHIVES = 5

# Strava names every activity type, and a heatmap of cycling should not
# include the parkrun. Matched by substring so Gravel Ride, Virtual Ride,
# Mountain Bike Ride and E-Bike Ride all count.
RIDE_MARKERS = ("ride", "cycl", "bike", "biking")


class ArchiveError(Exception):
    """The archive could not be read. The message is user-facing."""


class QueueFullError(Exception):
    """No room for another archive right now. The message is user-facing."""


def _problem_message(exc: Exception) -> str:
    """A user-facing line for one failed entry.

    ArchiveError and RouteImportError already carry a message written for a
    rider to read. A raw database error carries the query and its bound
    parameters (see the DBAPIError caught above), which is neither readable
    nor safe to hand back verbatim - so it collapses to one generic line.
    """
    if isinstance(exc, (ArchiveError, RouteImportError)):
        return str(exc)
    return "could not be saved"


@dataclass
class ImportJob:
    id: uuid.UUID
    user_id: uuid.UUID
    filename: str
    status: str = "queued"  # queued | running | done | error
    total: int = 0
    imported: int = 0
    failed: int = 0
    skipped: int = 0
    duplicates: int = 0
    error: str | None = None
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchiveEntry:
    """One ride file inside the archive, with whatever the CSV said about it."""

    path: str
    name: str | None
    activity_type: str | None


def _safe_path(name: str) -> bool:
    """Reject anything that tries to escape the archive.

    Nothing here is ever written to disk, so traversal cannot overwrite a
    file - but an entry named "../../etc/passwd" is a sign of an archive that
    was not produced by Strava, and there is no reason to read further into
    it.
    """
    if name.startswith("/") or name.startswith("\\"):
        return False
    return ".." not in name.replace("\\", "/").split("/")


def _read_entry(archive: zipfile.ZipFile, path: str, limit: int) -> bytes:
    """Read one entry, giving up the moment it exceeds `limit`.

    Read in chunks against the decompressed stream, so a small entry that
    expands to gigabytes is stopped partway rather than after the fact.
    ZipInfo.file_size is the archive's own claim about the size and a hostile
    archive is free to lie in it, so it is not trusted as the check. A lie
    that understates the size makes zipfile itself refuse the member on a CRC
    mismatch, which is converted here into a per-file failure - one bad ride
    should not take the rest of the archive down with it.
    """
    buffer = bytearray()
    try:
        with archive.open(path) as stream:
            while chunk := stream.read(DECOMPRESS_CHUNK):
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise ArchiveError(f"{path} expands beyond the per-file limit")
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise ArchiveError(f"{path} could not be read from the archive: {exc}") from exc
    return bytes(buffer)


def _decompress_member(path: str, data: bytes) -> bytes:
    """Gunzip a .gz member, capped the same way as everything else."""
    if not path.lower().endswith(".gz"):
        return data
    buffer = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            while chunk := stream.read(DECOMPRESS_CHUNK):
                buffer.extend(chunk)
                if len(buffer) > MAX_FILE_BYTES:
                    raise ArchiveError(f"{path} expands beyond the per-file limit")
    except (OSError, EOFError) as exc:
        raise ArchiveError(f"{path} is not readable gzip: {exc}") from exc
    return bytes(buffer)


def read_manifest(archive: zipfile.ZipFile) -> dict[str, tuple[str | None, str | None]]:
    """activities.csv as {path: (name, type)}, or empty when there is none.

    A hand-assembled zip of GPX files has no manifest, and that is fine - it
    just means every parseable file is taken at face value.
    """
    manifest_path = next(
        (n for n in archive.namelist() if n.lower().endswith("activities.csv") and _safe_path(n)),
        None,
    )
    if manifest_path is None:
        return {}

    try:
        raw = _read_entry(archive, manifest_path, MAX_FILE_BYTES)
    except ArchiveError:
        return {}

    rows: dict[str, tuple[str | None, str | None]] = {}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    for row in reader:
        # Column names have changed across Strava's export versions and vary
        # by locale, so they are matched loosely rather than by exact key.
        lookup = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        path = _first(lookup, "filename")
        if not path:
            continue
        rows[path.lstrip("./")] = (_first(lookup, "activity name"), _first(lookup, "activity type"))
    return rows


def _first(row: dict[str, str], needle: str) -> str | None:
    for key, value in row.items():
        if needle in key:
            return value or None
    return None


def is_ride(activity_type: str | None) -> bool:
    """Unknown types are ridden, not skipped.

    A manifest that does not name the type, or names it in a language this
    does not read, should not silently lose the ride - a file that turns out
    not to be one will simply fail to parse or add a stray line to the map.
    """
    if not activity_type:
        return True
    lowered = activity_type.lower()
    return any(marker in lowered for marker in RIDE_MARKERS)


def list_entries(data: bytes) -> tuple[list[ArchiveEntry], int]:
    """The ride files in an archive, and how many non-rides were skipped."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError("That is not a readable zip archive.") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError(f"Archive holds more than {MAX_ENTRIES} entries.")
        claimed = sum(info.file_size for info in infos)
        if claimed > MAX_TOTAL_UNCOMPRESSED:
            raise ArchiveError("Archive expands to more than this can handle.")

        manifest = read_manifest(archive)
        entries: list[ArchiveEntry] = []
        skipped = 0
        for info in infos:
            path = info.filename
            if info.is_dir() or not _safe_path(path):
                continue
            if not _looks_like_a_track(path):
                continue
            name, activity_type = manifest.get(path.lstrip("./"), (None, None))
            if not is_ride(activity_type):
                skipped += 1
                continue
            entries.append(ArchiveEntry(path=path, name=name, activity_type=activity_type))
    return entries, skipped


def _looks_like_a_track(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith(".gz"):
        lowered = lowered[: -len(".gz")]
    return lowered.endswith((".gpx", ".tcx", ".fit"))


class ArchiveImportQueue:
    """One worker, one job at a time.

    A second worker buys nothing here - the work is CPU-bound and already
    leaves the event loop per file - and it would double the ways a partial
    import can end up half-applied.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[uuid.UUID, bytes]] = asyncio.Queue(
            maxsize=MAX_QUEUED_ARCHIVES
        )
        self._jobs: OrderedDict[uuid.UUID, ImportJob] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    def full(self) -> bool:
        """Whether there is nowhere to put another archive right now.

        Exposed so the upload endpoint can refuse before spending the cost of
        the in-memory copy: `_read_capped` buffers up to MAX_ARCHIVE_BYTES
        into a bytes object, and a queue consulted only once that copy exists
        bounds nothing, since the memory is already spent by the time the
        check runs.

        This does not get ahead of the request body itself. FastAPI parses and
        spools the whole multipart payload to produce the endpoint's
        UploadFile parameter, so the bytes have already been received before
        any code in the endpoint runs - only middleware sees a request early
        enough for that, which is why the size cap lives there.

        Nor is it a reservation: two requests racing this check against the
        same not-yet-full queue can both pass it and both go on to read, so it
        closes the common case - a queue already saturated by earlier jobs -
        rather than every concurrent-burst case. submit()'s own check remains
        the actual enforcement of the cap.
        """
        return self._queue.full()

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        # Anything still in flight died with the process, and its bytes went
        # with it. Say so rather than leaving a job reading "running" forever
        # against work that will never resume.
        for job in self._jobs.values():
            if job.status in ("queued", "running"):
                job.status = "error"
                job.error = "Interrupted by a restart - upload the archive again."

    def submit(self, user_id: uuid.UUID, filename: str, data: bytes) -> ImportJob:
        # Checked before a job is even built: if there is nowhere to put the
        # work, nothing should be tracked or created for it either. No await
        # sits between this check and put_nowait, so nothing else can run in
        # between and race it.
        if self._queue.full():
            raise QueueFullError(
                f"{MAX_QUEUED_ARCHIVES} archive imports are already queued. "
                "Try again in a few minutes."
            )
        job = ImportJob(id=uuid.uuid4(), user_id=user_id, filename=filename)
        self._remember(job)
        self._queue.put_nowait((job.id, data))
        return job

    def get(self, job_id: uuid.UUID, user_id: uuid.UUID) -> ImportJob | None:
        job = self._jobs.get(job_id)
        return job if job is not None and job.user_id == user_id else None

    def _remember(self, job: ImportJob) -> None:
        # Only finished jobs are evicted. Dropping a running one would lose
        # the only record of work that is still happening. In practice the
        # queue itself (MAX_QUEUED_ARCHIVES, well under MAX_TRACKED_JOBS)
        # keeps the number of queued/running jobs far below this cap, so
        # this loop should always find a finished job to make room - but if
        # it ever cannot, the old code fell through and inserted anyway,
        # silently growing past MAX_TRACKED_JOBS. Refusing instead means the
        # cap holds even if that invariant is ever broken by a future
        # change.
        while len(self._jobs) >= MAX_TRACKED_JOBS:
            finished = next(
                (i for i, j in self._jobs.items() if j.status in ("done", "error")), None
            )
            if finished is None:
                raise QueueFullError(
                    "Too many imports are already tracked. Try again in a few minutes."
                )
            del self._jobs[finished]
        self._jobs[job.id] = job

    async def _run(self) -> None:
        while True:
            job_id, data = await self._queue.get()
            try:
                await self._process(job_id, data)
            except Exception:  # noqa: BLE001 - the worker must never die
                logger.exception("Archive import %s crashed", job_id)
                job = self._jobs.get(job_id)
                if job is not None:
                    job.status = "error"
                    job.error = "Unexpected error while reading the archive"
            finally:
                self._queue.task_done()

    async def _process(self, job_id: uuid.UUID, data: bytes) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = "running"

        try:
            entries, skipped = await asyncio.to_thread(list_entries, data)
        except ArchiveError as exc:
            job.status = "error"
            job.error = str(exc)
            return

        job.total = len(entries)
        job.skipped = skipped

        async with session_factory() as db:
            seen = set(
                (
                    await db.execute(
                        select(Activity.source_ref).where(
                            Activity.user_id == job.user_id, Activity.source_ref.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )

            # Ids only, not the ORM objects - they collect across the whole
            # archive and are only needed after commit to queue matching.
            created: list[uuid.UUID] = []
            archive = zipfile.ZipFile(io.BytesIO(data))
            with archive:
                for entry in entries:
                    ref = strava_activity_id(entry.path)
                    if ref is not None and ref in seen:
                        job.duplicates += 1
                        continue
                    # Committed per entry, inside the same try/except that
                    # already isolates a bad file: SQLAlchemy batches pending
                    # inserts sharing a table into one multi-row INSERT, so a
                    # single row Postgres refuses (a NaN that slipped past
                    # the importer's own guard, say) takes every row queued
                    # alongside it down in the same statement - and without
                    # a commit in between, that includes rides already
                    # counted as imported. Committing here is also what
                    # keeps `imported` honest: it is only incremented once
                    # the row is known to have actually landed.
                    try:
                        activity = await self._one(db, job, archive, entry, ref)
                        await db.commit()
                    except (ArchiveError, RouteImportError, SQLAlchemyError) as exc:
                        await db.rollback()
                        job.failed += 1
                        if len(job.problems) < 20:
                            job.problems.append(f"{entry.path}: {_problem_message(exc)}")
                        continue
                    if ref is not None:
                        seen.add(ref)
                    job.imported += 1
                    created.append(activity.id)

        # Off the request path, same reasoning as the single-file import
        # endpoint: map matching is Valhalla round trips per ride, and a
        # whole archive's worth would turn "reading..." into a much longer
        # wait for no benefit - coverage can lag an import by however long
        # the queue takes to reach it.
        if created:
            match_queue.submit(job.user_id, created)

        job.status = "done"

    async def _one(
        self,
        db: AsyncSession,
        job: ImportJob,
        archive: zipfile.ZipFile,
        entry: ArchiveEntry,
        ref: str | None,
    ) -> Activity:
        raw = await asyncio.to_thread(_read_entry, archive, entry.path, MAX_FILE_BYTES)
        data = await asyncio.to_thread(_decompress_member, entry.path, raw)
        track = await asyncio.to_thread(parse_route_file, entry.path, data)
        activity = activity_from_track(
            track,
            user_id=job.user_id,
            fallback_name=entry.name or name_from_filename(entry.path),
            source="strava_export",
            source_ref=ref,
        )
        # The manifest's name is the one the rider actually gave the ride, so
        # it wins over whatever the file calls itself.
        if entry.name:
            activity.name = entry.name[:200]
        db.add(activity)
        return activity


queue = ArchiveImportQueue()
