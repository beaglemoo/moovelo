"""The route assistant endpoints.

Two transports over one loop: `/chat` answers with plain JSON once the turn
is over, `/chat/stream` reports the same turn as it happens over SSE. The
loop itself lives in services/assistant/turn.py and knows about neither.

Nothing here writes to the database. The assistant proposes, and whatever
it produces comes back as ordinary waypoints plus a route the planner can
adopt or discard - never a saved route and never a black box.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.api.deps import DbDep, UserDep
from app.models import SearchIndexMeta, User
from app.schemas import BicycleCostingOptions, Preset, RouteResponse, Waypoint
from app.services import rate_limit
from app.services.assistant.naming import suggest_name as generate_name
from app.services.assistant.refs import MAX_KNOWN_HANDLES, Handle, HandleTable
from app.services.assistant.tools import ToolContext
from app.services.assistant.turn import (
    AssistantTurn,
    Delta,
    Finished,
    ToolFinished,
    ToolStarted,
    run_turn,
    run_turn_events,
)
from app.services.llm import LLMClient, LLMError
from app.services.llm_config import LLMConfig, resolve_llm_config
from app.services.places import reverse_geocode
from app.services.valhalla import ValhallaClient

logger = logging.getLogger(__name__)

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

    @model_validator(mode="after")
    def _kind_matches_ref(self) -> "KnownHandle":
        """`kind` is derived from `ref`, not trusted alongside it.

        Both are client-supplied and were validated independently, so a
        handle could arrive claiming to be a `loop` under a `place:` ref.
        Nothing downstream currently branches on kind in a way that matters,
        which is exactly why it is worth closing now rather than after
        something does.
        """
        prefix = self.ref.split(":", 1)[0]
        if prefix != self.kind:
            raise ValueError(f"kind {self.kind!r} does not match ref prefix {prefix!r}")
        return self


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


def _spend_a_turn(user_id: uuid.UUID) -> None:
    """Bound what one account can spend at the configured endpoint.

    The system prompt asks the assistant to stay on route planning, and a
    prompt is persuasion rather than a guarantee - nothing stops a determined
    rider talking it into something else on the operator's tab. This is the
    part that is not persuasion: past the allowance the endpoint refuses,
    whatever the model would have been willing to do.
    """
    if not rate_limit.assistant.check_and_record(f"assistant:{user_id}"):
        raise HTTPException(
            status_code=429,
            detail="That is a lot of assistant requests - try again later.",
        )


async def _prepare(
    body: AssistantChatRequest,
    request: Request,
    db: DbDep,
    user: User,
) -> tuple[LLMConfig, ToolContext, list[dict[str, Any]]]:
    """Everything both chat transports need before the loop starts.

    Shared so the streamed and non-streamed endpoints cannot drift on which
    handles they adopt or what the tools are allowed to see.
    """
    config = await resolve_llm_config(db)
    if not config.enabled:
        # 404 rather than 503, matching the weather and Wahoo gates: the
        # feature does not exist on this install rather than being broken.
        raise HTTPException(status_code=404, detail="The route assistant is not configured")
    _spend_a_turn(user.id)

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
    return config, ctx, history


def _proposal_of(turn: AssistantTurn) -> ProposalResponse | None:
    if turn.proposal is None:
        return None
    return ProposalResponse(
        waypoints=turn.proposal.waypoints,
        preset=turn.proposal.preset,
        snapshot=turn.proposal.route,
    )


def _handles_of(turn: AssistantTurn) -> list[HandleResponse]:
    return [
        HandleResponse(
            ref=h.ref,
            kind=h.kind,
            name=h.name,
            lat=h.lat,
            lon=h.lon,
            waypoints=list(h.waypoints) if h.waypoints else None,
        )
        for h in turn.handles
    ]


@router.post("/chat")
async def chat(
    body: AssistantChatRequest,
    request: Request,
    db: DbDep,
    user: UserDep,
) -> AssistantChatResponse:
    config, ctx, history = await _prepare(body, request, db, user)
    async with LLMClient(config) as llm:
        turn = await run_turn(llm, history, ctx)

    return AssistantChatResponse(
        content=turn.content,
        proposal=_proposal_of(turn),
        handles=_handles_of(turn),
        tools_called=turn.tools_called,
        stopped_early=turn.stopped_early,
        error=turn.error,
    )


def _frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame. Blank line terminates it; json.dumps keeps newlines in
    model text from splitting one frame into two."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    body: AssistantChatRequest,
    request: Request,
    db: DbDep,
    user: UserDep,
) -> StreamingResponse:
    """The same turn as /chat, reported as it happens.

    Auth and configuration are resolved before the response starts, so an
    unauthenticated or unconfigured caller gets an ordinary 401/404 with a
    JSON body rather than a 200 whose first frame is an apology. Only a
    failure *after* the first byte has to be expressed in-band, as an `error`
    frame.

    Frames: `token` (prose, incremental), `tool_call` and `tool_result`
    (progress, and the reason a step failed), `handles` (refs with their
    coordinates - server to frontend only, never back into model context),
    `proposal`, `error`, and exactly one `done` on every path, so the client
    has one unambiguous stop signal.

    The request-scoped session is used throughout the stream. FastAPI exits
    `yield` dependencies only once the body is exhausted - measured on the
    version in use, not assumed - and an AsyncSession would survive an early
    exit anyway, since closing one only returns its connection to the pool.
    """
    config, ctx, history = await _prepare(body, request, db, user)

    async def frames() -> AsyncIterator[str]:
        done_sent = False
        try:
            async with LLMClient(config) as llm:
                async for event in run_turn_events(llm, history, ctx, stream_text=True):
                    if isinstance(event, Delta):
                        yield _frame("token", {"text": event.text})
                    elif isinstance(event, ToolStarted):
                        yield _frame("tool_call", {"name": event.name})
                    elif isinstance(event, ToolFinished):
                        yield _frame("tool_result", {"name": event.name, "error": event.error})
                    elif isinstance(event, Finished):
                        turn = event.turn
                        handles = _handles_of(turn)
                        if handles:
                            yield _frame(
                                "handles",
                                {"handles": [h.model_dump() for h in handles]},
                            )
                        proposal = _proposal_of(turn)
                        if proposal is not None:
                            yield _frame("proposal", proposal.model_dump(mode="json"))
                        if turn.error:
                            yield _frame("error", {"message": turn.error})
                        done_sent = True
                        yield _frame(
                            "done",
                            {
                                "stopped_early": turn.stopped_early,
                                "tools_called": turn.tools_called,
                            },
                        )
        except Exception:
            # The status line went out long ago, so a traceback here would
            # reach the rider as a silently truncated stream. Log it properly
            # and close the stream the same way every other path closes it.
            logger.exception("Assistant stream failed")
            if not done_sent:
                yield _frame("error", {"message": "The assistant failed unexpectedly"})
                yield _frame("done", {"stopped_early": "error", "tools_called": []})

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Belt and braces for proxies that buffer by default. The
            # documented Caddy deployment does not, but a self-hoster's nginx
            # would turn this feature into a long pause and one big dump.
            "X-Accel-Buffering": "no",
        },
    )


class SuggestNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waypoints: list[Waypoint] = Field(min_length=2, max_length=50)
    # Whatever the planner is showing right now, never recomputed here - the
    # naming call must describe the route the rider is looking at, not a
    # figure of its own invention.
    distance_m: float = Field(ge=0)
    ascent_m: float = Field(ge=0)


class SuggestNameResponse(BaseModel):
    name: str


@router.post("/suggest-name")
async def suggest_name(
    body: SuggestNameRequest,
    db: DbDep,
    user: UserDep,
) -> SuggestNameResponse:
    """Propose a short name for the route about to be saved.

    One plain completion, no tools: naming needs no route-building
    primitives, just the two place names (when the index resolves them) and
    the distance/ascent the planner already measured. Both reverse-geocode
    lookups are None on an install with no place index built, which the
    prompt is told explicitly rather than left to guess at.
    """
    config = await resolve_llm_config(db)
    if not config.enabled:
        # 404 rather than 503, matching /chat: the feature does not exist on
        # this install rather than being broken.
        raise HTTPException(status_code=404, detail="The route assistant is not configured")
    _spend_a_turn(user.id)

    start = await reverse_geocode(db, body.waypoints[0].lat, body.waypoints[0].lon)
    end = await reverse_geocode(db, body.waypoints[-1].lat, body.waypoints[-1].lon)

    try:
        async with LLMClient(config) as llm:
            name = await generate_name(
                llm,
                distance_km=body.distance_m / 1000.0,
                ascent_m=body.ascent_m,
                start_name=start.name if start else None,
                end_name=end.name if end else None,
            )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not name:
        # A blank reply is a bad completion, not a reason to send the rider
        # back to a plain date-based fallback the frontend already has - a
        # figure we actually measured beats nothing.
        name = f"{round(body.distance_m / 1000.0)} km ride"
    return SuggestNameResponse(name=name)
