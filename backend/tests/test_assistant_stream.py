"""The SSE transport for the assistant (POST /api/assistant/chat/stream).

The loop itself is covered in test_assistant.py; what matters here is the
wire contract the panel depends on - which frames arrive, in what order, and
that exactly one `done` closes the stream however the turn ends.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models import LLMSettings, SearchIndexMeta
from tests.conftest import register

GATEWAY = "https://gateway.test/v1"
COMPLETIONS = f"{GATEWAY}/chat/completions"


@pytest.fixture(autouse=True)
def stream_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No env-configured endpoint, no lifespan, no retry backoff.

    `app.state.valhalla` is normally set by the lifespan, which the test
    transport never runs; none of these turns route, so a placeholder is
    enough to get past ToolContext construction.
    """
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_provider_order", "")
    monkeypatch.setattr(app.state, "valhalla", object(), raising=False)
    # Otherwise the unreachable-endpoint test sleeps out the real backoff.
    monkeypatch.setattr("app.services.llm.MAX_ATTEMPTS", 1)
    yield


async def configure_llm(db: AsyncSession) -> None:
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()


def sse(*events: dict[str, Any]) -> str:
    """A gateway's streamed reply, terminated the way they really terminate."""
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return body + "data: [DONE]\n\n"


def text_delta(text: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": text}}]}


def tool_delta(name: str, arguments: str, index: int = 0) -> dict[str, Any]:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                }
            }
        ]
    }


def stream_response(*events: dict[str, Any]) -> Response:
    return Response(
        200,
        content=sse(*events).encode(),
        headers={"content-type": "text/event-stream"},
    )


async def collect(client: AsyncClient, **body: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every frame the endpoint sent, as (event, data) in arrival order."""
    payload = {"messages": [{"role": "user", "content": "hi"}], **body}
    frames: list[tuple[str, dict[str, Any]]] = []
    async with client.stream("POST", "/api/assistant/chat/stream", json=payload) as response:
        assert response.status_code == 200, await response.aread()
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                event = ""
                data = "{}"
                for line in raw.splitlines():
                    if line.startswith("event:"):
                        event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:") :].strip()
                if event:
                    frames.append((event, json.loads(data)))
    return frames


# --- the contract -----------------------------------------------------------


@respx.mock
async def test_prose_arrives_as_tokens_and_exactly_one_done(
    client: AsyncClient, db: AsyncSession
) -> None:
    await configure_llm(db)
    respx.post(COMPLETIONS).mock(
        return_value=stream_response(text_delta("Two "), text_delta("lakes."))
    )
    await register(client)

    frames = await collect(client)
    names = [name for name, _ in frames]

    assert names == ["token", "token", "done"]
    assert "".join(data["text"] for name, data in frames if name == "token") == "Two lakes."
    assert names.count("done") == 1
    assert frames[-1][1]["stopped_early"] is None


@respx.mock
async def test_tool_progress_is_reported_before_the_answer(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The frames that make a 20-second turn tolerable to sit through."""
    await configure_llm(db)
    respx.post(COMPLETIONS).mock(
        side_effect=[
            # No route is on screen, so route_stats fails recoverably and the
            # model gets a second go - the ordinary self-correction path.
            stream_response(tool_delta("route_stats", "{}")),
            stream_response(text_delta("Nothing planned yet.")),
        ]
    )
    await register(client)

    frames = await collect(client)
    names = [name for name, _ in frames]

    assert names == ["tool_call", "tool_result", "token", "done"]
    assert frames[0][1]["name"] == "route_stats"
    assert frames[1][1]["error"] == "no_route"
    assert frames[-1][1]["tools_called"] == ["route_stats"]


@respx.mock
async def test_an_unreachable_endpoint_closes_the_stream_in_band(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The 200 has already gone out, so this cannot be an HTTP error - it has
    to be an `error` frame followed by the same single `done`."""
    await configure_llm(db)
    respx.post(COMPLETIONS).mock(side_effect=httpx.ConnectError("refused"))
    await register(client)

    frames = await collect(client)
    names = [name for name, _ in frames]

    assert names[-2:] == ["error", "done"]
    assert names.count("done") == 1
    assert "unreachable" in frames[-2][1]["message"]


@respx.mock
async def test_the_database_is_still_open_inside_the_stream(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A database-backed tool runs after the response has started.

    This covers the wiring - that ToolContext's session is usable once frames
    are already going out - and not much more. It is deliberately not sold as
    a guard on FastAPI's dependency lifetime: measured on the version in use,
    `yield` dependencies are exited only after the body is exhausted, but even
    an early teardown would not fail here, because closing an AsyncSession
    returns its connection to the pool and leaves the session able to acquire
    another. Injecting `await db.close()` before the first frame leaves this
    test green, which is how that was established rather than assumed.
    """
    await configure_llm(db)
    db.add(
        SearchIndexMeta(
            id=True,
            built_at=datetime.now(UTC),
            source_files=["test.osm.pbf"],
            place_count=0,
            poi_count=0,
            cycle_way_count=0,
        )
    )
    await db.commit()
    respx.post(COMPLETIONS).mock(
        side_effect=[
            stream_response(tool_delta("search_place", '{"query": "Tring"}')),
            stream_response(text_delta("I could not find it.")),
        ]
    )
    await register(client)

    frames = await collect(client)
    results = [data for name, data in frames if name == "tool_result"]

    assert results, "the tool never ran"
    # The index is built but empty, so the query succeeds and finds nothing.
    # An error here means the session was gone, not that the place was.
    assert results[0]["error"] is None


# --- gating (resolved before the stream opens, so ordinary HTTP errors) ------


async def test_stream_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/api/assistant/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


async def test_stream_404s_when_unconfigured(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/assistant/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


# --- guardrails --------------------------------------------------------------


@respx.mock
async def test_a_rider_cannot_spend_without_limit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The part of the scope guardrail that is not persuasion.

    The system prompt asks the assistant to stay on route planning, which a
    determined rider can talk it out of. This cannot be talked out of: past
    the allowance the endpoint refuses regardless of what the model would
    have been willing to do.
    """
    from app.services import rate_limit

    monkeypatch.setattr(rate_limit.assistant, "max_events", 2)
    await configure_llm(db)
    respx.post(COMPLETIONS).mock(return_value=stream_response(text_delta("ok")))
    await register(client)

    for _ in range(2):
        frames = await collect(client)
        assert frames[-1][0] == "done"

    blocked = await client.post(
        "/api/assistant/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert blocked.status_code == 429
    # Refused before the stream opens, so it is an ordinary HTTP error the
    # client can act on rather than an in-band frame.
    assert blocked.headers["content-type"].startswith("application/json")
