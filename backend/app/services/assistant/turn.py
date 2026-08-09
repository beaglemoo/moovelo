"""The tool-calling loop for one assistant turn.

Named turn.py rather than loop.py: services/loop.py already means the route
loop generator, and one of these tools calls it.

Shape of a turn: send the conversation, run whatever tools come back, send
the results, repeat until the model answers in prose or a budget runs out.
Every foreseeable failure becomes a tool result the model can read and
correct, because the alternative - failing the turn - loses the rider's
question over something the model could have fixed itself.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.schemas import Preset, RouteResponse, Waypoint
from app.services.assistant.prompt import SCOPE_REMINDER, SYSTEM_PROMPT, build_context_note
from app.services.assistant.refs import Handle
from app.services.assistant.tools import ToolContext, run_tool, tool_schemas
from app.services.llm import LLMClient, LLMError, TextDelta

logger = logging.getLogger(__name__)

# One completion per reasoning round, plus the final answer. A realistic
# request (search, search, plan, find water, summarise) measured five tool
# calls over six completions, so nine leaves room for one full
# self-correction round without paying for a model that is flailing.
MAX_COMPLETION_CALLS = 9
# Covers the slowest route measured end to end (about 29s) several times
# over, while still bounding a request rather than letting it hang.
WALL_CLOCK_S = 120.0
# A weak model that gets an argument wrong tends to repeat the identical
# call rather than correct it. Two strikes and the turn ends.
MAX_IDENTICAL_ERRORS = 2
# A single completion may request any number of tool calls, and each one is
# real work - routing, a PostGIS query - that neither the completion budget
# nor the wall clock was checked against between them. Five is more than any
# sensible round needs and stops one malformed reply becoming an unbounded
# amount of work.
MAX_TOOL_CALLS_PER_ROUND = 5


@dataclass
class Proposal:
    waypoints: list[Waypoint]
    route: RouteResponse
    preset: Preset


@dataclass
class AssistantTurn:
    content: str = ""
    proposal: Proposal | None = None
    handles: list[Handle] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    # Set when the turn ended on a budget rather than on the model finishing,
    # so the caller can say so rather than presenting a truncated answer as
    # a complete one.
    stopped_early: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ToolStarted:
    name: str


@dataclass(frozen=True)
class ToolFinished:
    name: str
    # The tool's own error code ("unknown_reference", "unavailable", ...) when
    # it failed recoverably, so the panel can say a step did not work rather
    # than showing a silent pause.
    error: str | None


@dataclass(frozen=True)
class Delta:
    """A fragment of prose for the rider.

    **Every word the rider should see arrives as one of these**, including
    the messages this module writes itself when a budget runs out. A consumer
    renders deltas and nothing else; `AssistantTurn.content` is the same text
    joined up, for callers that want it whole.
    """

    text: str


@dataclass(frozen=True)
class Finished:
    turn: "AssistantTurn"


TurnEvent = ToolStarted | ToolFinished | Delta | Finished


def build_messages(
    history: list[dict[str, Any]],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    note = build_context_note(
        waypoint_count=len(ctx.waypoints),
        has_route=ctx.route is not None,
        has_search=ctx.search_enabled,
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": note},
        *history,
        # Last, after everything the rider and the tools have said. A leading
        # instruction block is trivially talked past because every later
        # message is more recent than it; this makes the rules the most
        # recent thing in the conversation as well as the first.
        {"role": "system", "content": SCOPE_REMINDER},
    ]


async def run_turn(
    llm: LLMClient,
    history: list[dict[str, Any]],
    ctx: ToolContext,
) -> AssistantTurn:
    """The whole turn, waited out. See `run_turn_events` for the live one."""
    turn = AssistantTurn()
    async for event in run_turn_events(llm, history, ctx):
        if isinstance(event, Finished):
            turn = event.turn
    return turn


async def run_turn_events(
    llm: LLMClient,
    history: list[dict[str, Any]],
    ctx: ToolContext,
    stream_text: bool = False,
) -> AsyncIterator[TurnEvent]:
    """One turn, reported as it happens.

    Transport-agnostic on purpose: this is the only implementation of the
    loop, and both the plain JSON endpoint and the SSE one consume it. The
    single behavioural difference is `stream_text` - with it, prose is
    fetched a fragment at a time and forwarded as it arrives; without it, one
    completion is awaited whole and its text arrives as a single Delta. The
    budgets, the recovery paths and the proposal are identical either way,
    because there is only one copy of them.

    Exactly one Finished is yielded, last, on every path.
    """
    turn = AssistantTurn()
    messages = build_messages(history, ctx)
    tools = tool_schemas()
    deadline = time.monotonic() + WALL_CLOCK_S
    recent_errors: list[str] = []
    # Every fragment of prose the model produced this turn, including any that
    # accompanied tool calls rather than ending the turn.
    prose: list[str] = []
    streamed_any_text = False

    for _ in range(MAX_COMPLETION_CALLS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            turn.stopped_early = "timeout"
            break
        try:
            if stream_text:
                response = None
                async for chunk in llm.stream(messages, tools=tools, timeout=remaining):
                    if isinstance(chunk, TextDelta):
                        streamed_any_text = True
                        yield Delta(chunk.text)
                    else:
                        response = chunk.response
                if response is None:
                    raise LLMError("The assistant service returned no reply")
            else:
                response = await llm.complete(messages, tools=tools, timeout=remaining)
        except LLMError as exc:
            # Keep anything already computed: a route planned two calls ago is
            # still a real route, and discarding it helps nobody.
            turn.error = str(exc)
            break

        # Prose can arrive alongside tool calls - a model narrating what it is
        # about to do - and dropping it lost the rider text the streamed
        # transport was already showing them.
        if response.content:
            prose.append(response.content)
            if not stream_text:
                yield Delta(response.content)

        if not response.tool_calls:
            break

        messages.append(response.message)
        for call in response.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]:
            if time.monotonic() >= deadline:
                # Checked per call, not just per round: one reply asking for
                # several routes can blow well past the wall clock inside a
                # single iteration of the outer loop.
                turn.stopped_early = "timeout"
                break
            turn.tools_called.append(call.name)
            yield ToolStarted(call.name)
            arguments, parse_error = _parse_arguments(call.arguments)
            if parse_error is not None:
                result: dict[str, Any] = {
                    "error": "invalid_arguments",
                    "message": parse_error,
                }
            else:
                try:
                    result = await run_tool(call.name, arguments, ctx)
                except Exception:
                    # run_tool turns everything foreseeable into a recoverable
                    # result; anything left is a bug. Ending the turn cleanly
                    # keeps whatever was already computed - a route planned two
                    # calls ago is still a real route - where letting it escape
                    # would skip Finished entirely and discard it.
                    logger.exception("Assistant tool %s failed unexpectedly", call.name)
                    turn.error = "That step failed unexpectedly, so I stopped."
                    break
            error = result.get("error")
            yield ToolFinished(call.name, error if isinstance(error, str) else None)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    # json.dumps, so a name containing quotes or newlines
                    # cannot break out of its own field.
                    "content": json.dumps(result),
                }
            )
            signature = _error_signature(call.name, result)
            if signature is None:
                recent_errors.clear()
            else:
                recent_errors.append(signature)
                # Only the most recent MAX_IDENTICAL_ERRORS count. Testing the
                # whole accumulated list meant one earlier, differently-shaped
                # error permanently defeated the guard: set() could never
                # collapse to one again, so a model repeating the same failure
                # ran to the full completion budget instead of stopping.
                recent = recent_errors[-MAX_IDENTICAL_ERRORS:]
                if len(recent) >= MAX_IDENTICAL_ERRORS and len(set(recent)) == 1:
                    turn.stopped_early = "repeated_error"
                    break
        if turn.stopped_early or turn.error:
            break
    else:
        turn.stopped_early = "budget"

    if ctx.route is not None and ctx.proposed_waypoints:
        turn.proposal = Proposal(
            waypoints=list(ctx.proposed_waypoints),
            route=ctx.route,
            preset=ctx.preset,
        )
    turn.content = "".join(prose)
    turn.handles = ctx.handles.export()
    if not turn.content and turn.stopped_early and not turn.error:
        # Written here rather than by the caller so a stopped turn costs no
        # extra completion, and emitted as a Delta like everything else the
        # rider reads - a consumer that renders deltas needs no special case.
        turn.content = _stopped_message(turn.stopped_early, turn.proposal is not None)
        # Unless prose is already on screen, where appending a budget notice
        # to a half-finished sentence reads worse than letting it stand.
        if not streamed_any_text:
            yield Delta(turn.content)
    yield Finished(turn)


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}, "The arguments were not valid JSON. Send them as a JSON object."
    if not isinstance(parsed, dict):
        return {}, "The arguments must be a JSON object."
    return parsed, None


def _error_signature(name: str, result: dict[str, Any]) -> str | None:
    """A stable key for 'the same mistake again', ignoring the message text."""
    error = result.get("error")
    return f"{name}:{error}" if isinstance(error, str) else None


def _stopped_message(reason: str, has_proposal: bool) -> str:
    tail = (
        " I have left the route I got to on the map, so you can carry on from there."
        if has_proposal
        else " Could you try asking in a more specific way?"
    )
    if reason == "timeout":
        return "That took too long, so I stopped." + tail
    if reason == "repeated_error":
        return "I got stuck repeating the same failed step, so I stopped." + tail
    return "That needed more steps than I am allowed in one go, so I stopped." + tail
