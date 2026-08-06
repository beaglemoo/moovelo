"""Serialized background worker for Wahoo route pushes.

A single asyncio worker drains the queue so pushes never block requests and
naturally respect the sandbox rate limits. Status lives on the route row
(queued -> pushing -> synced/error), so the UI can poll it and a restart can
re-enqueue anything left behind.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import session_factory
from app.models import Route, WahooAccount
from app.services import wahoo

logger = logging.getLogger(__name__)


class WahooQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._recover()
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def enqueue(self, route_id: uuid.UUID) -> None:
        self._queue.put_nowait(route_id)

    async def _recover(self) -> None:
        """Re-enqueue routes stranded by a restart mid-push."""
        try:
            async with session_factory() as db:
                stranded = (
                    (
                        await db.execute(
                            select(Route.id).where(Route.wahoo_status.in_(["queued", "pushing"]))
                        )
                    )
                    .scalars()
                    .all()
                )
            for route_id in stranded:
                self.enqueue(route_id)
            if stranded:
                logger.info("Re-enqueued %d stranded Wahoo pushes", len(stranded))
        except Exception:  # noqa: BLE001 - DB may not be migrated yet in tests
            logger.exception("Wahoo queue recovery failed")

    async def _run(self) -> None:
        while True:
            route_id = await self._queue.get()
            try:
                await self._push_one(route_id)
            except Exception:  # noqa: BLE001 - worker must never die
                logger.exception("Unexpected error pushing route %s", route_id)
            finally:
                self._queue.task_done()

    async def _push_one(self, route_id: uuid.UUID) -> None:
        async with session_factory() as db:
            route = (
                await db.execute(select(Route).where(Route.id == route_id))
            ).scalar_one_or_none()
            if route is None or route.wahoo_status not in ("queued", "pushing"):
                return
            account = (
                await db.execute(select(WahooAccount).where(WahooAccount.user_id == route.user_id))
            ).scalar_one_or_none()
            if account is None:
                route.wahoo_status = "error"
                route.wahoo_error = "Wahoo account disconnected"
                await db.commit()
                return

            route.wahoo_status = "pushing"
            await db.commit()
            # The commit expires server-onupdate columns (updated_at); reload
            # so push_route can read them outside the async session machinery.
            await db.refresh(route)

            try:
                wahoo_id = await wahoo.push_route(route, account)
            except wahoo.WahooError as exc:
                route.wahoo_status = "error"
                route.wahoo_error = str(exc)[:500]
            except Exception:  # noqa: BLE001
                logger.exception("Wahoo push crashed for route %s", route_id)
                route.wahoo_status = "error"
                route.wahoo_error = "Unexpected error during push"
            else:
                route.wahoo_status = "synced"
                route.wahoo_error = None
                route.wahoo_route_id = wahoo_id or route.wahoo_route_id
                route.wahoo_pushed_at = datetime.now(UTC)
            await db.commit()


queue = WahooQueue()
