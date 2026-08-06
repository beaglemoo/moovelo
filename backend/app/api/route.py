from fastapi import APIRouter, Request

from app.schemas import RouteRequest, RouteResponse
from app.services.valhalla import ValhallaClient

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/route")
async def plan_route(request: Request, body: RouteRequest) -> RouteResponse:
    client: ValhallaClient = request.app.state.valhalla
    return await client.route(body)
