"""LLM client tests: retry policy, error mapping, and the payload we send."""

import json
from collections.abc import Iterator

import pytest
import respx
from httpx import AsyncClient, ConnectError, Response

from app.config import settings
from app.services.llm import Completed, LLMClient, LLMError
from tests.conftest import register

LLM_URL = "https://llm.test/v1"
COMPLETIONS = f"{LLM_URL}/chat/completions"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_place",
            "description": "Find a named place.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
]


def _reply(content: str = "Hello", tool_calls: list[dict[str, object]] | None = None) -> dict:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


@pytest.fixture
def llm_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "llm_base_url", LLM_URL)
    monkeypatch.setattr(settings, "llm_model", "test/model")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_provider_order", "")
    yield


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Retries are exercised for behaviour, not for wall-clock time."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.services.llm.asyncio.sleep", instant)
    yield


@respx.mock
async def test_completion_parses_content(llm_settings: None) -> None:
    respx.post(COMPLETIONS).mock(return_value=Response(200, json=_reply("Ride safely")))
    async with LLMClient() as llm:
        result = await llm.complete([{"role": "user", "content": "hi"}])
    assert result.content == "Ride safely"
    assert result.tool_calls == []


@respx.mock
async def test_completion_parses_tool_calls(llm_settings: None) -> None:
    respx.post(COMPLETIONS).mock(
        return_value=Response(
            200,
            json=_reply(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        # The wire format carries arguments as a JSON string.
                        "function": {"name": "search_place", "arguments": '{"query": "Tring"}'},
                    }
                ],
            ),
        )
    )
    async with LLMClient() as llm:
        result = await llm.complete([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "search_place"
    assert json.loads(call.arguments) == {"query": "Tring"}


@respx.mock
async def test_message_kept_verbatim_for_replay(llm_settings: None) -> None:
    """Reasoning models attach extra fields, and replaying them unchanged is
    what the endpoints expect - so the raw message survives parsing."""
    raw = {
        "role": "assistant",
        "content": "thinking done",
        "reasoning": "some chain",
        "reasoning_details": [{"type": "text"}],
    }
    respx.post(COMPLETIONS).mock(return_value=Response(200, json={"choices": [{"message": raw}]}))
    async with LLMClient() as llm:
        result = await llm.complete([{"role": "user", "content": "hi"}])
    assert result.message == raw


@respx.mock
async def test_retries_429_then_succeeds(llm_settings: None) -> None:
    route = respx.post(COMPLETIONS).mock(
        side_effect=[
            Response(429, headers={"Retry-After": "1"}),
            Response(200, json=_reply("second time lucky")),
        ]
    )
    async with LLMClient() as llm:
        result = await llm.complete([{"role": "user", "content": "hi"}])
    assert result.content == "second time lucky"
    assert route.call_count == 2


@respx.mock
async def test_non_numeric_retry_after_does_not_crash(llm_settings: None) -> None:
    """Retry-After may legally be an HTTP-date."""
    route = respx.post(COMPLETIONS).mock(
        side_effect=[
            Response(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            Response(200, json=_reply("recovered")),
        ]
    )
    async with LLMClient() as llm:
        result = await llm.complete([{"role": "user", "content": "hi"}])
    assert result.content == "recovered"
    assert route.call_count == 2


@respx.mock
async def test_exhausted_5xx_raises(llm_settings: None) -> None:
    route = respx.post(COMPLETIONS).mock(return_value=Response(500))
    async with LLMClient() as llm:
        with pytest.raises(LLMError, match="rate-limiting or unavailable"):
            await llm.complete([{"role": "user", "content": "hi"}])
    assert route.call_count == 3


@respx.mock
async def test_transport_error_raises_unreachable(llm_settings: None) -> None:
    respx.post(COMPLETIONS).mock(side_effect=ConnectError("no route to host"))
    async with LLMClient() as llm:
        with pytest.raises(LLMError, match="unreachable"):
            await llm.complete([{"role": "user", "content": "hi"}])


@respx.mock
async def test_4xx_is_not_retried_and_surfaces_endpoint_message(llm_settings: None) -> None:
    route = respx.post(COMPLETIONS).mock(
        return_value=Response(400, json={"error": {"message": "Provider returned error"}})
    )
    async with LLMClient() as llm:
        with pytest.raises(LLMError, match="Provider returned error"):
            await llm.complete([{"role": "user", "content": "hi"}])
    assert route.call_count == 1


@respx.mock
async def test_empty_choices_raises(llm_settings: None) -> None:
    respx.post(COMPLETIONS).mock(return_value=Response(200, json={"choices": []}))
    async with LLMClient() as llm:
        with pytest.raises(LLMError, match="no reply"):
            await llm.complete([{"role": "user", "content": "hi"}])


@respx.mock
async def test_api_key_sent_only_when_set(
    llm_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.post(COMPLETIONS).mock(return_value=Response(200, json=_reply()))
    async with LLMClient() as llm:
        await llm.complete([{"role": "user", "content": "hi"}])
    assert "Authorization" not in route.calls[0].request.headers

    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    async with LLMClient() as llm:
        await llm.complete([{"role": "user", "content": "hi"}])
    assert route.calls[1].request.headers["Authorization"] == "Bearer sk-test"


@respx.mock
async def test_provider_order_sent_with_fallbacks_on(
    llm_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preference, never a hard pin: a strictly pinned provider that rejects
    a mid-conversation tool replay fails the turn instead of routing around."""
    monkeypatch.setattr(settings, "llm_provider_order", "Fireworks, Together ,")
    route = respx.post(COMPLETIONS).mock(return_value=Response(200, json=_reply()))
    async with LLMClient() as llm:
        await llm.complete([{"role": "user", "content": "hi"}])
    body = json.loads(route.calls[0].request.content)
    assert body["provider"] == {"order": ["Fireworks", "Together"], "allow_fallbacks": True}


@respx.mock
async def test_no_provider_block_when_unset(llm_settings: None) -> None:
    route = respx.post(COMPLETIONS).mock(return_value=Response(200, json=_reply()))
    async with LLMClient() as llm:
        await llm.complete([{"role": "user", "content": "hi"}])
    assert "provider" not in json.loads(route.calls[0].request.content)


async def test_config_reports_assistant_disabled_by_default(client: AsyncClient) -> None:
    await register(client)
    response = await client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["assistant_enabled"] is False


async def test_config_reports_assistant_enabled(client: AsyncClient, llm_settings: None) -> None:
    await register(client)
    response = await client.get("/api/config")
    assert response.json()["assistant_enabled"] is True


async def test_assistant_needs_both_url_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", LLM_URL)
    monkeypatch.setattr(settings, "llm_model", "")
    assert settings.assistant_enabled is False
    monkeypatch.setattr(settings, "llm_model", "test/model")
    assert settings.assistant_enabled is True
    # The API key is deliberately not part of it - a local Ollama needs none.
    monkeypatch.setattr(settings, "llm_api_key", "")
    assert settings.assistant_enabled is True


# --- the two transports share one parser and one retry policy ---------------


@respx.mock
async def test_streamed_replies_carry_finish_reason(llm_settings: None) -> None:
    """The truncation guard reads finish_reason, and the chat panel only ever
    uses the streamed path - so a streamed reply that dropped it left the
    guard dead exactly where max_tokens matters most."""
    body = (
        'data: {"choices": [{"delta": {"content": "cut off"}, "finish_reason": "length"}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(COMPLETIONS).mock(
        return_value=Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )
    )
    async with LLMClient() as llm:
        chunks = [chunk async for chunk in llm.stream([{"role": "user", "content": "hi"}])]
    final = [c for c in chunks if isinstance(c, Completed)]
    assert final and final[0].response.finish_reason == "length"


@respx.mock
async def test_an_in_band_error_retries_before_any_output(llm_settings: None) -> None:
    """Raised inside stream() and caught inside stream().

    The first attempt at this raised the retry from stream() while the only
    handler sat in complete(), so it escaped uncaught and killed the turn -
    a fix that never ran on the path that raises it.
    """
    good = (
        'data: {"choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx.post(COMPLETIONS).mock(
        side_effect=[
            Response(
                200,
                content=b'data: {"error": {"message": "upstream blip"}}\n\n',
                headers={"content-type": "text/event-stream"},
            ),
            Response(200, content=good.encode(), headers={"content-type": "text/event-stream"}),
        ]
    )
    async with LLMClient() as llm:
        chunks = [chunk async for chunk in llm.stream([{"role": "user", "content": "hi"}])]
    assert route.call_count == 2, "the in-band error was not retried"
    final = [c for c in chunks if isinstance(c, Completed)]
    assert final and final[0].response.content == "recovered"
