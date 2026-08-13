"""Admin-only configuration for the route assistant.

Admin rather than per-user because the endpoint, the key and the spend are
install-wide - unlike /settings, which is the rider's own model.

The model and provider listings are proxied rather than fetched from the
browser: the API key never leaves the backend, and the frontend never needs
to know which gateway is in use.
"""

import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.api.admin import AdminDep
from app.api.deps import DbDep
from app.models import LLMSettings
from app.services.llm import LLMClient, LLMError
from app.services.llm_config import PROVIDER_SORTS, LLMConfig, resolve_llm_config

router = APIRouter(prefix="/api/admin/llm")

CATALOGUE_TIMEOUT_S = 20.0
# The probe is a real completion, so it is kept as small as one can be while
# still proving the thing that matters: that the model will call a tool.
PROBE_TIMEOUT_S = 60.0
PROBE_TOOL: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_place",
            "description": "Find a named place. Call this to locate any place.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
]


class LLMSettingsResponse(BaseModel):
    """What the admin page shows.

    Note what is absent: the API key itself is never returned by any
    endpoint, only whether one is set.
    """

    base_url: str | None = None
    model: str | None = None
    provider_order: str | None = None
    provider_sort: str | None = None
    max_prompt_price: float | None = None
    api_key_set: bool = False
    # The effective values once environment fallback is applied, so the page
    # can show what is actually in force rather than only what was overridden.
    effective_base_url: str = ""
    effective_model: str = ""
    effective_providers: list[str] = Field(default_factory=list)
    enabled: bool = False
    # True when the env vars alone would configure the assistant, so the UI
    # can explain where a value it did not set is coming from.
    env_configured: bool = False


class LLMSettingsPatch(BaseModel):
    base_url: str | None = None
    model: str | None = None
    provider_order: str | None = None
    provider_sort: str | None = None
    max_prompt_price: float | None = Field(default=None, ge=0)
    # Write-only. Send an empty string to clear a stored key.
    api_key: str | None = None


class ModelInfo(BaseModel):
    id: str
    name: str = ""
    prompt_price: float | None = None
    completion_price: float | None = None
    context_length: int | None = None
    supports_tools: bool = False


class ProviderInfo(BaseModel):
    name: str
    prompt_price: float | None = None
    completion_price: float | None = None
    context_length: int | None = None


class ProbeResult(BaseModel):
    ok: bool
    latency_s: float
    model: str
    provider: str | None = None
    cost: float | None = None
    tool_called: bool = False
    detail: str = ""


def _to_response(
    row: LLMSettings | None, resolved: LLMConfig, env: LLMConfig
) -> LLMSettingsResponse:
    return LLMSettingsResponse(
        base_url=row.base_url if row else None,
        model=row.model if row else None,
        provider_order=row.provider_order if row else None,
        provider_sort=row.provider_sort if row else None,
        max_prompt_price=row.max_prompt_price if row else None,
        api_key_set=bool(resolved.api_key),
        effective_base_url=resolved.base_url,
        effective_model=resolved.model,
        effective_providers=resolved.providers,
        enabled=resolved.enabled,
        env_configured=env.enabled,
    )


@router.get("")
async def get_llm_settings(db: DbDep, _admin: AdminDep) -> LLMSettingsResponse:
    from app.services.llm_config import config_from_env, select_llm_settings

    # Through select_llm_settings, not a raw query: on an install downgraded
    # past 0011 the table is gone, and this admin page must degrade to env-only
    # like every other reader rather than 500 (the exact endpoint the first fix
    # claimed to cover but missed, because it queried LLMSettings directly).
    row = await select_llm_settings(db)
    return _to_response(row, await resolve_llm_config(db), config_from_env())


@router.patch("")
async def update_llm_settings(
    body: LLMSettingsPatch, db: DbDep, _admin: AdminDep
) -> LLMSettingsResponse:
    from app.services.llm_config import config_from_env, select_llm_settings

    changes = body.model_dump(exclude_unset=True)
    sort = changes.get("provider_sort")
    if sort not in (None, "") and sort not in PROVIDER_SORTS:
        raise HTTPException(
            status_code=422,
            detail=f"provider_sort must be one of: {', '.join(PROVIDER_SORTS)}",
        )
    # Blank strings mean "clear this override and fall back to the env var",
    # which the resolver already treats as unset. Normalising to NULL here
    # keeps the stored row honest about what is actually overridden.
    for key, value in list(changes.items()):
        if isinstance(value, str) and not value.strip():
            changes[key] = None

    try:
        row = await select_llm_settings(db)
        if row is None:
            row = LLMSettings(id=True, **changes)
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                # Two concurrent first PATCHes both see no row and both insert
                # the same singleton primary key. Recover rather than 500ing:
                # re-select and apply this request's changes on top.
                await db.rollback()
                row = (await db.execute(select(LLMSettings))).scalar_one()
                for field_name, value in changes.items():
                    setattr(row, field_name, value)
                await db.commit()
        else:
            for field_name, value in changes.items():
                setattr(row, field_name, value)
            await db.commit()
    except ProgrammingError as exc:
        # The table is gone (downgraded past 0011): a write cannot land, so say
        # so clearly instead of 500ing. Unlike a read, a PATCH has nowhere to
        # degrade to - the operator has to restore the migration or use env.
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail=(
                "The llm_settings table is missing (this install was downgraded "
                "past migration 0011). Restore the migration, or configure the "
                "assistant with the LLM_* environment variables instead."
            ),
        ) from exc
    await db.refresh(row)
    return _to_response(row, await resolve_llm_config(db), config_from_env())


async def _gateway_get(config: LLMConfig, path: str) -> Any:
    """GET a catalogue endpoint on the configured gateway."""
    if not config.base_url:
        raise HTTPException(status_code=404, detail="No assistant endpoint is configured")
    url = config.base_url.rstrip("/") + path
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    try:
        async with httpx.AsyncClient(timeout=CATALOGUE_TIMEOUT_S) as client:
            response = await client.get(url, headers=headers)
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503, detail="The assistant endpoint is unreachable"
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"The assistant endpoint returned {response.status_code} for its model list",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="The model list was unreadable") from exc


def _price(pricing: Any, key: str) -> float | None:
    if not isinstance(pricing, dict):
        return None
    raw = pricing.get(key)
    if not isinstance(raw, str | int | float):
        return None
    try:
        # Gateways quote per-token; per-million is the unit humans compare in.
        return float(raw) * 1_000_000
    except ValueError:
        return None


@router.get("/models")
async def list_models(db: DbDep, _admin: AdminDep, tools_only: bool = True) -> list[ModelInfo]:
    """The gateway's catalogue.

    tools_only defaults to true because a model without tool-calling support
    cannot drive the assistant at all - it would answer in prose and never
    plan anything, with no error to explain why.
    """
    config = await resolve_llm_config(db)
    body = await _gateway_get(config, "/models")
    raw = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return []
    models: list[ModelInfo] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        supported = item.get("supported_parameters")
        # Ollama and LM Studio do not advertise supported_parameters at all;
        # absent means unknown, and filtering those out would empty the list
        # on exactly the self-hosted setups this project prefers.
        supports_tools = "tools" in supported if isinstance(supported, list) else True
        if tools_only and not supports_tools:
            continue
        pricing = item.get("pricing")
        raw_name = item.get("name")
        models.append(
            ModelInfo(
                id=item["id"],
                name=raw_name if isinstance(raw_name, str) else "",
                prompt_price=_price(pricing, "prompt"),
                completion_price=_price(pricing, "completion"),
                context_length=(
                    item.get("context_length")
                    if isinstance(item.get("context_length"), int)
                    else None
                ),
                supports_tools=supports_tools,
            )
        )
    models.sort(key=lambda m: m.id)
    return models


@router.get("/models/{model_id:path}/providers")
async def list_providers(model_id: str, db: DbDep, _admin: AdminDep) -> list[ProviderInfo]:
    """Which providers serve a model, and at what price.

    This is the whole reason the routing controls exist: the same model can
    be served an order of magnitude apart in price.
    """
    config = await resolve_llm_config(db)
    body = await _gateway_get(config, f"/models/{model_id}/endpoints")
    data = body.get("data") if isinstance(body, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        return []
    providers: list[ProviderInfo] = []
    for item in endpoints:
        if not isinstance(item, dict):
            continue
        name = item.get("provider_name")
        if not isinstance(name, str):
            continue
        pricing = item.get("pricing")
        providers.append(
            ProviderInfo(
                name=name,
                prompt_price=_price(pricing, "prompt"),
                completion_price=_price(pricing, "completion"),
                context_length=(
                    item.get("context_length")
                    if isinstance(item.get("context_length"), int)
                    else None
                ),
            )
        )
    return providers


@router.post("/test")
async def test_endpoint(db: DbDep, _admin: AdminDep) -> ProbeResult:
    """One small completion carrying one tool, to answer three questions at
    once: does the endpoint work, how slow is it, and will this model
    actually call a tool.

    The third matters most. A model that answers in prose instead of calling
    the tool will look fine here and be useless in the planner, so a reply
    without a tool call is reported as a failure rather than a success.
    """
    config = await resolve_llm_config(db)
    if not config.enabled:
        raise HTTPException(status_code=400, detail="Set an endpoint and a model before testing")
    messages = [
        {
            "role": "system",
            "content": "You are a cycling route assistant. Use the tools available to you.",
        },
        {"role": "user", "content": "Find the town of Tring."},
    ]
    started = time.monotonic()
    try:
        async with LLMClient(config) as llm:
            result = await llm.complete(messages, tools=PROBE_TOOL, timeout=PROBE_TIMEOUT_S)
    except LLMError as exc:
        return ProbeResult(
            ok=False,
            latency_s=round(time.monotonic() - started, 2),
            model=config.model,
            detail=str(exc),
        )
    elapsed = round(time.monotonic() - started, 2)
    tool_called = bool(result.tool_calls)
    usage = result.raw_usage
    return ProbeResult(
        ok=tool_called,
        latency_s=elapsed,
        model=config.model,
        provider=result.provider,
        cost=usage.get("cost") if isinstance(usage.get("cost"), int | float) else None,
        tool_called=tool_called,
        detail=(
            "Endpoint reachable and the model called the tool."
            if tool_called
            else "The endpoint replied, but the model answered without calling the tool. "
            "This model cannot drive the assistant - pick one that supports tool calling."
        ),
    )
