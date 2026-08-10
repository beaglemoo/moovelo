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
import json
import logging
from collections.abc import AsyncIterator
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
# A hard ceiling on what one completion can be billed for. The assistant's
# job is short prose plus tool calls, so this is generous for the work and
# still bounds the damage if a model is talked into writing an essay - the
# prompt asks it to stay on topic, this makes the cost of it not doing so
# finite. Applies to every request, tools or not.
MAX_COMPLETION_TOKENS = 1024


NO_REPLY = "The assistant service returned no reply"
RATE_LIMITED = "The assistant service is rate-limiting or unavailable - try again later"


class _Retry(Exception):
    """Ask the attempt loop to try again.

    Raised from wherever the decision is actually made - an HTTP status, or an
    in-band error frame - and always caught by the loop in the same method.
    An earlier version raised this from `stream()` while the only handler sat
    in `complete()`, so the exception escaped uncaught and killed the turn;
    keeping the raise and the catch in one shape is what stops that recurring.
    """

    def __init__(self, reason: str, delay: float) -> None:
        super().__init__(reason)
        self.delay = delay


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
    # Gateway-reported usage and the provider that actually served the call.
    # Both are optional extras rather than part of the OpenAI shape, so they
    # are absent against a plain local endpoint.
    raw_usage: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    # "length" means the reply hit MAX_COMPLETION_TOKENS and stops mid
    # sentence - or mid tool-call JSON. Discarding it presented a truncated
    # answer as a finished one, which is the same failure shape as swallowing
    # an error frame.
    finish_reason: str | None = None


@dataclass(frozen=True)
class TextDelta:
    """A fragment of the assistant's prose, as it arrives."""

    text: str


@dataclass(frozen=True)
class Completed:
    """The assembled reply. Exactly one of these ends every stream."""

    response: LLMResponse


StreamChunk = TextDelta | Completed


def _parse_message(
    message: dict[str, Any],
    usage: dict[str, Any] | None = None,
    provider: str | None = None,
    finish_reason: str | None = None,
) -> LLMResponse:
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
        raw_usage=usage or {},
        provider=provider,
        finish_reason=finish_reason,
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
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": MAX_COMPLETION_TOKENS,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
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
        """One completion, waited out. Retries only 429/5xx."""
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
                _check_status(response, attempt)
                try:
                    body = response.json()
                except ValueError as exc:
                    raise LLMError("The assistant service returned an unreadable response") from exc
                return _completion_from_body(body)
            except _Retry as retry:
                logger.warning("Assistant request retry %d after %s", attempt, retry)
                await asyncio.sleep(min(retry.delay, MAX_BACKOFF_S))
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise LLMError("The assistant service is unreachable") from exc
                await asyncio.sleep(min(2**attempt, MAX_BACKOFF_S))

        raise LLMError(RATE_LIMITED)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """One completion, delivered as it arrives.

        Yields TextDelta for each fragment of prose and exactly one Completed
        carrying the same LLMResponse `complete()` would have returned - the
        same object, built by the same parser, because the two used to build
        it separately and drifted three times running (tool-call ids, then
        error frames, then finish_reason, each fixed in one path only).

        Retries are narrower than `complete()`'s deliberately: once a delta
        has been handed to the caller it has reached the rider's screen, and
        starting again would repeat text rather than replace it. So a failure
        mid-body is terminal, and only one before any output is retried.
        """
        if self._client is None:
            raise LLMError("LLM client used outside its context manager")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = self._payload(messages, tools, stream=True)
        headers = self._headers()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            emitted = False
            content: list[str] = []
            # Keyed by the wire's own `index`, because a reply with two tool
            # calls interleaves their fragments rather than finishing one
            # before starting the next.
            calls: dict[int, dict[str, str]] = {}
            finish_reason: str | None = None
            try:
                async with self._client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout if timeout is not None else REQUEST_TIMEOUT_S,
                ) as response:
                    if response.status_code >= 400:
                        # _error_message needs a body, which a streamed
                        # response has not read yet.
                        await response.aread()
                    _check_status(response, attempt)

                    saw_payload = False
                    async for line in response.aiter_lines():
                        event = _parse_sse_line(line)
                        if event is None:
                            continue
                        # Gateways report a mid-generation failure - quota,
                        # rate limit, content policy - as an error frame on a
                        # 200 that has already gone out. Before any output
                        # that is the same situation as a 5xx, so it retries
                        # like one; after, retrying would repeat text.
                        if "error" in event:
                            message = _stream_error_message(event["error"])
                            if emitted or attempt == MAX_ATTEMPTS:
                                raise LLMError(message)
                            raise _Retry(message, float(2**attempt))
                        # A trailing usage frame carries an empty choices
                        # list; that is not a reply, and counting it meant an
                        # endpoint sending only that looked successful.
                        choices = event.get("choices")
                        if choices:
                            saw_payload = True
                            reason = choices[0].get("finish_reason")
                            if isinstance(reason, str):
                                finish_reason = reason
                        text = _accumulate(event, content, calls)
                        if text:
                            emitted = True
                            yield TextDelta(text)
                    if not saw_payload:
                        raise LLMError(NO_REPLY)
            except _Retry as retry:
                logger.warning("Assistant stream retry %d after %s", attempt, retry)
                await asyncio.sleep(min(retry.delay, MAX_BACKOFF_S))
                continue
            except httpx.TransportError as exc:
                if emitted:
                    raise LLMError("The assistant service disconnected mid-reply") from exc
                if attempt == MAX_ATTEMPTS:
                    raise LLMError("The assistant service is unreachable") from exc
                await asyncio.sleep(min(2**attempt, MAX_BACKOFF_S))
                continue

            yield Completed(_assemble(content, calls, finish_reason))
            return

        raise LLMError(RATE_LIMITED)


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """Retry-After may legally be an HTTP-date rather than delay-seconds;
    anything non-numeric falls back to the exponential default."""
    try:
        return float(response.headers.get("Retry-After", 2**attempt))
    except ValueError:
        return float(2**attempt)


def _check_status(response: httpx.Response, attempt: int) -> None:
    """The single place an HTTP status decides retry, fail, or proceed.

    Both transports call this, so neither can quietly grow its own policy -
    which is how they came to disagree about in-band errors and truncation.
    """
    if response.status_code == 429 or response.status_code >= 500:
        if attempt == MAX_ATTEMPTS:
            raise LLMError(RATE_LIMITED)
        raise _Retry(f"HTTP {response.status_code}", _retry_after(response, attempt))
    if response.status_code >= 400:
        raise LLMError(_error_message(response))


def _completion_from_body(body: Any) -> LLMResponse:
    """A whole (non-streamed) response body, through the shared parser."""
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LLMError(NO_REPLY)
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMError(NO_REPLY)
    usage = body.get("usage")
    provider = body.get("provider")
    reason = first.get("finish_reason")
    return _parse_message(
        message,
        usage if isinstance(usage, dict) else None,
        provider if isinstance(provider, str) else None,
        reason if isinstance(reason, str) else None,
    )


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """One `data:` frame, or None for anything that carries no payload.

    Comment lines (`: keep-alive`), blank separators and the terminal
    `[DONE]` sentinel all arrive here and none of them are data.
    """
    # A leading BOM survives httpx's decoding and would stop the first frame
    # ever matching, silently losing it.
    line = line.lstrip("\ufeff")
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        # A malformed frame is not worth failing a reply that is otherwise
        # arriving fine; the assembled result simply omits it.
        logger.warning("Ignoring an unparseable frame from the assistant service")
        return None
    return parsed if isinstance(parsed, dict) else None


def _accumulate(
    event: dict[str, Any],
    content: list[str],
    calls: dict[int, dict[str, str]],
) -> str:
    """Fold one frame into the running reply, returning any new prose."""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return ""

    for raw in delta.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        if not isinstance(index, int):
            index = 0
        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if isinstance(raw.get("id"), str):
            call["id"] = raw["id"]
        function = raw.get("function")
        if isinstance(function, dict):
            if isinstance(function.get("name"), str):
                call["name"] += function["name"]
            if isinstance(function.get("arguments"), str):
                call["arguments"] += function["arguments"]

    text = delta.get("content")
    if isinstance(text, str) and text:
        content.append(text)
        return text
    return ""


def _assemble(
    content: list[str],
    calls: dict[int, dict[str, str]],
    finish_reason: str | None = None,
) -> LLMResponse:
    """Rebuild the assistant message the non-streaming path would have got.

    Only the standard fields survive a stream - `complete()` replays the
    message dict verbatim, extra vendor fields included, and there is no
    equivalent here. That costs nothing today because the loop only replays
    messages that carried tool calls, and those are reconstructed exactly.
    """
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    tool_calls = []
    for index in sorted(calls):
        call = calls[index]
        if not call["name"]:
            continue
        # Some gateways send the id only on the opening fragment, and a
        # dropped one used to take the whole call with it - the model would
        # then see a reply where the tool it asked for simply never ran.
        # The id only has to be unique within this message.
        tool_calls.append(
            {
                "id": call["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
        )
    if tool_calls:
        message["tool_calls"] = tool_calls
    # Same parser as the non-streamed path, so finish_reason, tool-call
    # parsing and content extraction cannot diverge between transports again.
    return _parse_message(message, finish_reason=finish_reason)


def _stream_error_message(error: Any) -> str:
    """An error frame's own text, when it offers one."""
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return f"The assistant service failed mid-reply: {message}"
    if isinstance(error, str) and error:
        return f"The assistant service failed mid-reply: {error}"
    return "The assistant service failed mid-reply"


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
