from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from app.api.activities import router as activities_router
from app.api.admin import router as admin_router
from app.api.assistant import router as assistant_router
from app.api.auth import router as auth_router
from app.api.custom_presets import router as custom_presets_router
from app.api.llm_admin import router as llm_admin_router
from app.api.places import router as places_router
from app.api.route import router
from app.api.routes import router as routes_router
from app.api.routes import shared_router
from app.api.settings import router as settings_router
from app.api.wahoo import router as wahoo_router
from app.config import settings
from app.services.activity_import import MAX_ARCHIVE_BYTES
from app.services.activity_import import queue as archive_queue
from app.services.importer import MAX_FILE_BYTES
from app.services.valhalla import ValhallaClient
from app.services.wahoo_queue import queue as wahoo_queue
from app.services.way_matching import queue as match_queue

# Multipart framing costs a little on top of the file itself, so the wire
# limit sits slightly above the file limit rather than rejecting a legitimate
# upload that happens to sit right on it.
MAX_UPLOAD_BYTES = MAX_FILE_BYTES + 1024 * 1024
# An activity upload may be a whole Strava export rather than one ride, so it
# gets its own ceiling. Applying the single-file limit to it would reject
# every real archive.
MAX_ACTIVITY_UPLOAD_BYTES = MAX_ARCHIVE_BYTES + 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.valhalla = ValhallaClient()
    await wahoo_queue.start()
    await archive_queue.start()
    # Shares the one Valhalla client every request already uses, rather than
    # owning a second httpx.AsyncClient - main.py closes it once, after every
    # queue (this one included) has stopped.
    await match_queue.start(app.state.valhalla)
    yield
    await wahoo_queue.stop()
    await archive_queue.stop()
    await match_queue.stop()
    await app.state.valhalla.close()


app = FastAPI(title="Moovelo", lifespan=lifespan)


@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next: Any) -> Response:
    """Refuse an over-large upload before its body is read.

    FastAPI resolves an UploadFile parameter by parsing the whole multipart
    body into a spooled temp file, so any size check inside the endpoint runs
    after the cost has been paid. Only a middleware sees the request early
    enough. Requests without a Content-Length still get caught by the
    endpoint's own chunked read.
    """
    if request.method == "POST" and request.url.path.endswith("/import"):
        archive = request.url.path.startswith("/api/activities")
        cap = MAX_ACTIVITY_UPLOAD_BYTES if archive else MAX_UPLOAD_BYTES
        readable = (MAX_ARCHIVE_BYTES if archive else MAX_FILE_BYTES) // (1024 * 1024)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Upload is larger than {readable} MB."},
            )
    response: Response = await call_next(request)
    return response


if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins.split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(router)
app.include_router(auth_router)
app.include_router(places_router)
app.include_router(routes_router)
app.include_router(shared_router)
app.include_router(settings_router)
app.include_router(wahoo_router)
app.include_router(admin_router)
app.include_router(custom_presets_router)
app.include_router(llm_admin_router)
app.include_router(assistant_router)
app.include_router(activities_router)


class SPAStaticFiles(StaticFiles):
    """Serve the SPA's index.html for any path that isn't a real file."""

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and not path.startswith("api"):
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404 and not path.startswith("api"):
            response = await super().get_response("index.html", scope)
        return response


static_dir = Path(settings.static_dir)
if static_dir.is_dir():
    app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="frontend")
