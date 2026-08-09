"""The route assistant endpoint.

Non-streaming in this PR; the streamed variant and the chat panel follow.

Nothing here writes to the database. The assistant proposes, and whatever
it produces comes back as ordinary waypoints plus a route the planner can
adopt or discard - never a saved route and never a black box.
"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.models import SearchIndexMeta
from app.schemas import BicycleCostingOptions, Preset, RouteResponse, Waypoint
from app.services.assistant.refs import MAX_KNOWN_HANDLES, Handle, HandleTable
from app.services.assistant.tools import ToolContext
from app.services.assistant.turn import run_turn
from app.services.llm import LLMClient
from app.services.llm_config import resolve_llm_config
from app.services.valhalla import ValhallaClient

router = APIRouter(prefix="/api/assistant")

MAX_HISTORY_MESSAGES = 40
MAX_MESSAGE_CHARS = 2000


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class KnownHandle(BaseModel):
    """A handle from an earlier turn, echoed back by the client.

    Conversation state lives on the client so the server keeps none. These
    coordinates come from the client, never from the model, which is why
    accepting them adds nothing the rider could not already send to
    /api/route directly.
    """

    model_config = ConfigDict(extra="forbid")
    ref: str = Field(pattern=r"^(place|poi|loop):[A-Za-z0-9_-]+$")
    kind: Literal["place", "poi", "loop"]
    name: str = Field(max_length=200)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    waypoints: list[Waypoint] | None = Field(default=None, max_length=12)


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_HISTORY_MESSAGES)
    # What is on the planner's screen right now.
    waypoints: list[Waypoint] = Field(default_factory=list, max_length=50)
    centre: Waypoint | None = None
    preset: Preset = "road"
    costing_options: BicycleCostingOptions | None = None
    known_handles: list[KnownHandle] = Field(default_factory=list, max_length=MAX_KNOWN_HANDLES)


class ProposalResponse(BaseModel):
    waypoints: list[Waypoint]
    preset: Preset
    snapshot: RouteResponse


class HandleResponse(BaseModel):
    ref: str
    kind: str
    name: str
    lat: float
    lon: float
    waypoints: list[Waypoint] | None = None


class AssistantChatResponse(BaseModel):
    content: str
    proposal: ProposalResponse | None = None
    handles: list[HandleResponse] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    stopped_early: str | None = None
    error: str | None = None


@router.post("/chat")
async def chat(
    body: AssistantChatRequest,
    request: Request,
    db: DbDep,
    _user: UserDep,
) -> AssistantChatResponse:
    config = await resolve_llm_config(db)
    if not config.enabled:
        # 404 rather than 503, matching the weather and Wahoo gates: the
        # feature does not exist on this install rather than being broken.
        raise HTTPException(status_code=404, detail="The route assistant is not configured")

    built_at = await db.scalar(select(SearchIndexMeta.built_at))
    handles = HandleTable()
    for known in body.known_handles:
        handles.adopt(
            Handle(
                ref=known.ref,
                kind=known.kind,
                name=known.name,
                lat=known.lat,
                lon=known.lon,
                waypoints=tuple(known.waypoints) if known.waypoints else None,
            )
        )
    handles.seed_context(body.waypoints, body.centre)

    valhalla: ValhallaClient = request.app.state.valhalla
    ctx = ToolContext(
        db=db,
        valhalla=valhalla,
        handles=handles,
        waypoints=list(body.waypoints),
        preset=body.preset,
        costing_options=body.costing_options,
        centre=body.centre,
        search_enabled=built_at is not None,
    )

    history: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in body.messages]
    async with LLMClient(config) as llm:
        turn = await run_turn(llm, history, ctx)

    return AssistantChatResponse(
        content=turn.content,
        proposal=(
            ProposalResponse(
                waypoints=turn.proposal.waypoints,
                preset=turn.proposal.preset,
                snapshot=turn.proposal.route,
            )
            if turn.proposal
            else None
        ),
        handles=[
            HandleResponse(
                ref=h.ref,
                kind=h.kind,
                name=h.name,
                lat=h.lat,
                lon=h.lon,
                waypoints=list(h.waypoints) if h.waypoints else None,
            )
            for h in turn.handles
        ],
        tools_called=turn.tools_called,
        stopped_early=turn.stopped_early,
        error=turn.error,
    )
