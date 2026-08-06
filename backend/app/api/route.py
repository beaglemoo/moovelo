from fastapi import APIRouter, Request

from app.api.deps import UserDep
from app.config import settings
from app.schemas import RouteRequest, RouteResponse
from app.services.valhalla import ValhallaClient

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def config() -> dict[str, str | None]:
    return {"tile_url_cyclosm": settings.tile_url_cyclosm or None}


@router.post("/route")
async def plan_route(request: Request, body: RouteRequest, _user: UserDep) -> RouteResponse:
    client: ValhallaClient = request.app.state.valhalla
    return await client.route(body)
