from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.config import settings
from app.models import SearchIndexMeta
from app.schemas import AppConfig, RouteRequest, RouteResponse
from app.services.valhalla import ValhallaClient

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def config(db: DbDep) -> AppConfig:
    # One row at most, so this is a cheap probe on every page load rather
    # than a count of a table the indexer may have filled with hundreds of
    # thousands of places. Taking built_at rather than a bare EXISTS costs
    # nothing and doubles as the tile cache key.
    built_at = await db.scalar(select(SearchIndexMeta.built_at))
    return AppConfig(
        tile_url_cyclosm=settings.tile_url_cyclosm or None,
        search_enabled=built_at is not None,
        search_index_version=str(int(built_at.timestamp())) if built_at else None,
    )


@router.post("/route")
async def plan_route(request: Request, body: RouteRequest, _user: UserDep) -> RouteResponse:
    client: ValhallaClient = request.app.state.valhalla
    return await client.route(body)
