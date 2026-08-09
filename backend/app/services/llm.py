"""Minimal OpenAI-compatible chat-completions client for the route assistant.

Deliberately not an SDK: the whole surface we need is one POST to
`/chat/completions`, and a raw httpx call keeps the dependency list short
and works unchanged against OpenRouter, Ollama, LM Studio or anything else
speaking the same shape.

Retry policy follows services/weather.py and services/wahoo.py: only 429 and
5xx are retried, Retry-After is honoured when numeric, and a terminal failure
raises LLMError carrying a message fit to show a user.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services.llm_config import LLMConfig, config_from_env

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Generous because completion latency varies hugely by model and provider:
# a fast hosted model answers in about a second, while a slow route through
# the same gateway has been measured at over 40 seconds for one call.
REQUEST_TIMEOUT_S = 90.0
MAX_BACKOFF_S = 60.0


class LLMError(Exception):
    """Terminal failure talking to the model endpoint, safe to show a user."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # The wire format carries arguments as a JSON *string*, not an object.
    # Parsing (and failing to parse) is the caller's business.
    arguments: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The assistant message exactly as received, for verbatim replay into the
    # next request. Reasoning models attach extra fields here and replaying
    # them unchanged is what the endpoints expect.
    message: dict[str, Any] = field(default_factory=dict)


def _parse_message(message: dict[str, Any]) -> LLMResponse:
    raw_calls = message.get("tool_calls") or []
    calls: list[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        call_id = raw.get("id")
        if not isinstance(name, str) or not isinstance(call_id, str):
            continue
        arguments = function.get("arguments")
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments if isinstance(arguments, str) else "{}",
            )
        )
    content = message.get("content")
    return LLMResponse(
        content=content if isinstance(content, str) else "",
        tool_calls=calls,
        message=message,
    )


class LLMClient:
    """One client per assistant turn.

    A turn makes several sequential completions, so the connection is worth
    reusing across them; holding one open for the lifetime of the process is
    not, given the assistant is off by default.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Falling back to the environment keeps the client usable outside a
        # request, where there is no database session to resolve against.
        self.config = config if config is not None else config_from_env()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "LLMClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = tools
        provider: dict[str, Any] = {}
        if self.config.providers:
            # A preference, never a constraint. allow_fallbacks stays true:
            # a strictly pinned provider that rejects a mid-conversation tool
            # replay fails the whole turn rather than routing around it.
            provider["order"] = self.config.providers
            provider["allow_fallbacks"] = True
        if self.config.provider_sort and self.config.provider_sort != "balanced":
            # "balanced" is the gateway's own default, expressed by sending
            # nothing. Sending an explicit mode measured materially cheaper
            # than letting it choose.
            provider["sort"] = self.config.provider_sort
        if self.config.max_prompt_price is not None:
            # A hard ceiling in dollars per million prompt tokens. The same
            # model can be served an order of magnitude apart in price, and
            # this is the only setting that actually caps what a call costs.
            provider["max_price"] = {"prompt": self.config.max_prompt_price}
        if provider:
            payload["provider"] = provider
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        """One completion, retrying only 429/5xx."""
        if self._client is None:
            raise LLMError("LLM client used outside its context manager")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = self._payload(messages, tools)
        headers = self._headers()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout if timeout is not None else REQUEST_TIMEOUT_S,
                )
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise LLMError("The assistant service is unreachable") from exc
                await asyncio.sleep(min(2**attempt, MAX_BACKOFF_S))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    break
                # Retry-After may legally be an HTTP-date rather than
                # delay-seconds; anything non-numeric falls back to the
                # exponential default instead of crashing the request.
                try:
                    retry_after = float(response.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    retry_after = float(2**attempt)
                logger.warning(
                    "Assistant request retry %d after HTTP %d", attempt, response.status_code
                )
                await asyncio.sleep(min(retry_after, MAX_BACKOFF_S))
                continue

            if response.status_code >= 400:
                raise LLMError(_error_message(response))

            try:
                body = response.json()
            except ValueError as exc:
                raise LLMError("The assistant service returned an unreadable response") from exc

            choices = body.get("choices") if isinstance(body, dict) else None
            if not isinstance(choices, list) or not choices:
                raise LLMError("The assistant service returned no reply")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if not isinstance(message, dict):
                raise LLMError("The assistant service returned no reply")
            return _parse_message(message)

        raise LLMError("The assistant service is rate-limiting or unavailable - try again later")


def _error_message(response: httpx.Response) -> str:
    """Pull the endpoint's own error text out when it offers one."""
    try:
        body = response.json()
    except ValueError:
        return f"The assistant service rejected the request ({response.status_code})"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return f"The assistant service rejected the request: {message}"
        if isinstance(error, str) and error:
            return f"The assistant service rejected the request: {error}"
    return f"The assistant service rejected the request ({response.status_code})"
