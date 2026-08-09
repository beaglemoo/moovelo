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
from dataclasses import dataclass, field
from typing import Any

from app.schemas import Preset, RouteResponse, Waypoint
from app.services.assistant.prompt import SYSTEM_PROMPT, build_context_note
from app.services.assistant.refs import Handle
from app.services.assistant.tools import ToolContext, run_tool, tool_schemas
from app.services.llm import LLMClient, LLMError

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
    ]


async def run_turn(
    llm: LLMClient,
    history: list[dict[str, Any]],
    ctx: ToolContext,
) -> AssistantTurn:
    turn = AssistantTurn()
    messages = build_messages(history, ctx)
    tools = tool_schemas()
    deadline = time.monotonic() + WALL_CLOCK_S
    recent_errors: list[str] = []

    for _ in range(MAX_COMPLETION_CALLS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            turn.stopped_early = "timeout"
            break
        try:
            response = await llm.complete(messages, tools=tools, timeout=remaining)
        except LLMError as exc:
            # Keep anything already computed: a route planned two calls ago is
            # still a real route, and discarding it helps nobody.
            turn.error = str(exc)
            break

        if not response.tool_calls:
            turn.content = response.content
            break

        messages.append(response.message)
        for call in response.tool_calls:
            turn.tools_called.append(call.name)
            arguments, parse_error = _parse_arguments(call.arguments)
            if parse_error is not None:
                result: dict[str, Any] = {
                    "error": "invalid_arguments",
                    "message": parse_error,
                }
            else:
                result = await run_tool(call.name, arguments, ctx)
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
                if len(recent_errors) >= MAX_IDENTICAL_ERRORS and len(set(recent_errors)) == 1:
                    turn.stopped_early = "repeated_error"
                    break
        if turn.stopped_early:
            break
    else:
        turn.stopped_early = "budget"

    if ctx.route is not None and ctx.proposed_waypoints:
        turn.proposal = Proposal(
            waypoints=list(ctx.proposed_waypoints),
            route=ctx.route,
            preset=ctx.preset,
        )
    turn.handles = ctx.handles.export()
    if not turn.content and turn.stopped_early and not turn.error:
        turn.content = _stopped_message(turn.stopped_early, turn.proposal is not None)
    return turn


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
