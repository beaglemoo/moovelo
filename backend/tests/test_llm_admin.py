"""Admin LLM configuration: resolution precedence, key secrecy, gating."""

import json
from collections.abc import Iterator

import pytest
import respx
from httpx import AsyncClient, ConnectError, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LLMSettings
from app.services.llm import LLMClient
from app.services.llm_config import resolve_llm_config
from tests.conftest import register

GATEWAY = "https://gateway.test/v1"
SECRET = "sk-should-never-be-returned"


@pytest.fixture
def env_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "llm_base_url", "https://env.test/v1")
    monkeypatch.setattr(settings, "llm_model", "env/model")
    monkeypatch.setattr(settings, "llm_api_key", "env-key")
    monkeypatch.setattr(settings, "llm_provider_order", "EnvProvider")
    yield


@pytest.fixture
def no_env_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_provider_order", "")
    yield


async def make_admin(client: AsyncClient, db: AsyncSession) -> None:
    """Each test gets a throwaway database, so the first registration is
    already the admin. db is taken so callers can seed rows in the same test."""
    await register(client)


# --- resolution precedence -------------------------------------------------


async def test_env_used_when_no_row(db: AsyncSession, env_llm: None) -> None:
    config = await resolve_llm_config(db)
    assert config.base_url == "https://env.test/v1"
    assert config.model == "env/model"
    assert config.providers == ["EnvProvider"]
    assert config.enabled is True


async def test_row_overrides_env_per_field(db: AsyncSession, env_llm: None) -> None:
    """Only the model is overridden; everything else still comes from env."""
    db.add(LLMSettings(id=True, model="db/model"))
    await db.commit()
    config = await resolve_llm_config(db)
    assert config.model == "db/model"
    assert config.base_url == "https://env.test/v1"
    assert config.api_key == "env-key"
    assert config.providers == ["EnvProvider"]


async def test_blank_row_values_fall_back_rather_than_disable(
    db: AsyncSession, env_llm: None
) -> None:
    """Saving the admin form with blank fields must not disable a working
    env-configured install."""
    db.add(LLMSettings(id=True, base_url="", model="", provider_order=""))
    await db.commit()
    config = await resolve_llm_config(db)
    assert config.base_url == "https://env.test/v1"
    assert config.model == "env/model"
    assert config.enabled is True


async def test_db_alone_enables_without_env(db: AsyncSession, no_env_llm: None) -> None:
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="db/model"))
    await db.commit()
    config = await resolve_llm_config(db)
    assert config.enabled is True
    # The key is genuinely optional - a local endpoint needs none.
    assert config.api_key == ""


async def test_disabled_when_neither_source_set(db: AsyncSession, no_env_llm: None) -> None:
    assert (await resolve_llm_config(db)).enabled is False


async def test_missing_table_falls_back_to_env(db: AsyncSession, env_llm: None) -> None:
    """An install downgraded past migration 0011 has no llm_settings table.
    resolve_llm_config must degrade to env-only config, not raise - the query
    otherwise 500s the unauthenticated GET /api/config on every page load."""
    from sqlalchemy import text

    await db.execute(text("DROP TABLE llm_settings"))
    await db.commit()

    config = await resolve_llm_config(db)
    assert config.base_url == "https://env.test/v1"
    assert config.model == "env/model"
    assert config.enabled is True

    # The failed SELECT must have been rolled back, leaving the session usable.
    assert (await db.execute(text("SELECT 1"))).scalar_one() == 1


async def test_admin_get_degrades_when_llm_settings_is_missing(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    """GET /api/admin/llm queried LLMSettings directly and 500'd on a missing
    table - the endpoint the first fix's message claimed to cover. It must
    degrade to env-only like every other reader."""
    from sqlalchemy import text

    await register(client)  # first user is the admin
    await db.execute(text("DROP TABLE llm_settings"))
    await db.commit()

    response = await client.get("/api/admin/llm")
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False  # no env, no table -> disabled


async def test_admin_patch_reports_a_missing_table_clearly(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    """A PATCH cannot persist to a missing table, so it returns a clear 503
    naming the cause rather than a bare 500."""
    from sqlalchemy import text

    await register(client)
    await db.execute(text("DROP TABLE llm_settings"))
    await db.commit()

    response = await client.patch("/api/admin/llm", json={"model": "foo/bar"})
    assert response.status_code == 503, response.text
    assert "llm_settings" in response.json()["detail"]


# --- the key must never come back -----------------------------------------


async def test_api_key_never_returned(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    patch = await client.patch(
        "/api/admin/llm",
        json={"base_url": GATEWAY, "model": "m", "api_key": SECRET},
    )
    assert patch.status_code == 200
    # Assert on the serialised body, not the model: a field added later that
    # happens to carry the key would slip past an attribute check.
    assert SECRET not in patch.text
    assert patch.json()["api_key_set"] is True

    get = await client.get("/api/admin/llm")
    assert SECRET not in get.text
    assert get.json()["api_key_set"] is True


async def test_api_key_can_be_cleared(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    await client.patch("/api/admin/llm", json={"api_key": SECRET})
    cleared = await client.patch("/api/admin/llm", json={"api_key": ""})
    assert cleared.json()["api_key_set"] is False


# --- access control --------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/admin/llm"),
        ("patch", "/api/admin/llm"),
        ("get", "/api/admin/llm/models"),
        ("get", "/api/admin/llm/models/some/model/providers"),
        ("post", "/api/admin/llm/test"),
    ],
)
async def test_endpoints_require_auth(client: AsyncClient, method: str, path: str) -> None:
    response = await getattr(client, method)(path, **({"json": {}} if method == "patch" else {}))
    assert response.status_code == 401


async def test_endpoints_require_admin(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(client)  # first user, admin
    monkeypatch.setattr(settings, "signups_enabled", True)
    # Registering again replaces the session cookie with a non-admin user.
    await register(client, email="rider@example.com")
    for method, path in [
        ("get", "/api/admin/llm"),
        ("get", "/api/admin/llm/models"),
        ("post", "/api/admin/llm/test"),
    ]:
        response = await getattr(client, method)(path)
        assert response.status_code == 403, path


# --- validation ------------------------------------------------------------


async def test_rejects_unknown_provider_sort(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    response = await client.patch("/api/admin/llm", json={"provider_sort": "cheapest"})
    assert response.status_code == 422
    assert "provider_sort must be one of" in response.json()["detail"]


async def test_accepts_known_provider_sort(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    response = await client.patch("/api/admin/llm", json={"provider_sort": "price"})
    assert response.status_code == 200
    assert response.json()["provider_sort"] == "price"


async def test_negative_max_price_rejected(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    response = await client.patch("/api/admin/llm", json={"max_prompt_price": -1})
    assert response.status_code == 422


# --- routing controls reach the request payload ---------------------------


@respx.mock
async def test_sort_and_max_price_in_payload(db: AsyncSession, no_env_llm: None) -> None:
    db.add(
        LLMSettings(
            id=True,
            base_url=GATEWAY,
            model="m",
            provider_order="Fireworks",
            provider_sort="price",
            max_prompt_price=0.2,
        )
    )
    await db.commit()
    route = respx.post(f"{GATEWAY}/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    config = await resolve_llm_config(db)
    async with LLMClient(config) as llm:
        await llm.complete([{"role": "user", "content": "hi"}])
    provider = json.loads(route.calls[0].request.content)["provider"]
    assert provider == {
        "order": ["Fireworks"],
        "allow_fallbacks": True,
        "sort": "price",
        "max_price": {"prompt": 0.2},
    }


@respx.mock
async def test_balanced_sends_no_sort(db: AsyncSession, no_env_llm: None) -> None:
    """'balanced' is the gateway's own default, expressed by sending nothing."""
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m", provider_sort="balanced"))
    await db.commit()
    route = respx.post(f"{GATEWAY}/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    async with LLMClient(await resolve_llm_config(db)) as llm:
        await llm.complete([{"role": "user", "content": "hi"}])
    assert "provider" not in json.loads(route.calls[0].request.content)


# --- catalogue proxy -------------------------------------------------------


@respx.mock
async def test_models_filters_to_tool_capable(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.get(f"{GATEWAY}/models").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": "a/tools",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.0000001", "completion": "0.0000006"},
                        "context_length": 1000,
                    },
                    {"id": "b/no-tools", "supported_parameters": ["temperature"]},
                    # No supported_parameters at all - a local endpoint.
                    {"id": "c/unknown"},
                ]
            },
        )
    )
    ids = [m["id"] for m in (await client.get("/api/admin/llm/models")).json()]
    assert ids == ["a/tools", "c/unknown"]

    everything = await client.get("/api/admin/llm/models?tools_only=false")
    assert [m["id"] for m in everything.json()] == ["a/tools", "b/no-tools", "c/unknown"]


@respx.mock
async def test_model_prices_converted_to_per_million(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.get(f"{GATEWAY}/models").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": "a/model",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.0000001", "completion": "0.0000006"},
                    }
                ]
            },
        )
    )
    model = (await client.get("/api/admin/llm/models")).json()[0]
    assert model["prompt_price"] == pytest.approx(0.1)
    assert model["completion_price"] == pytest.approx(0.6)


@respx.mock
async def test_models_unreachable_gateway_is_503(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.get(f"{GATEWAY}/models").mock(side_effect=ConnectError("down"))
    response = await client.get("/api/admin/llm/models")
    assert response.status_code == 503


async def test_models_404_when_nothing_configured(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    assert (await client.get("/api/admin/llm/models")).status_code == 404


@respx.mock
async def test_providers_handles_slashed_model_id(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    """Model ids contain a slash, so the route needs a path converter."""
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.get(f"{GATEWAY}/models/openai/gpt-5.6-luna-pro/endpoints").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "endpoints": [
                        {"provider_name": "OpenAI", "pricing": {"prompt": "0.0000001"}},
                        {"provider_name": "Azure", "pricing": {"prompt": "0.000001"}},
                    ]
                }
            },
        )
    )
    providers = (await client.get("/api/admin/llm/models/openai/gpt-5.6-luna-pro/providers")).json()
    assert [p["name"] for p in providers] == ["OpenAI", "Azure"]
    # The spread that makes the routing controls worth having.
    assert providers[1]["prompt_price"] == pytest.approx(10 * providers[0]["prompt_price"])


# --- the probe -------------------------------------------------------------


async def test_probe_refuses_when_unconfigured(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    response = await client.post("/api/admin/llm/test")
    assert response.status_code == 400


@respx.mock
async def test_probe_reports_success_with_tool_call(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.post(f"{GATEWAY}/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "search_place",
                                        "arguments": '{"query":"Tring"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"cost": 0.00025},
                "provider": "OpenAI",
            },
        )
    )
    result = (await client.post("/api/admin/llm/test")).json()
    assert result["ok"] is True
    assert result["tool_called"] is True
    assert result["provider"] == "OpenAI"
    assert result["cost"] == pytest.approx(0.00025)


@respx.mock
async def test_probe_reports_failure_when_no_tool_called(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    """A model that answers in prose looks fine but is useless in the planner,
    so this must be a failure rather than a success."""
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.post(f"{GATEWAY}/chat/completions").mock(
        return_value=Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Tring is a town."}}]},
        )
    )
    result = (await client.post("/api/admin/llm/test")).json()
    assert result["ok"] is False
    assert result["tool_called"] is False
    assert "tool calling" in result["detail"]


@respx.mock
async def test_probe_reports_unreachable_without_raising(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    await make_admin(client, db)
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    respx.post(f"{GATEWAY}/chat/completions").mock(side_effect=ConnectError("down"))
    response = await client.post("/api/admin/llm/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False


# --- the bug this PR fixed -------------------------------------------------


async def test_config_reports_db_configured_assistant(
    client: AsyncClient, db: AsyncSession, no_env_llm: None
) -> None:
    """/api/config read the bare env flag, so an install configured entirely
    through the admin page kept the panel hidden."""
    assert (await client.get("/api/config")).json()["assistant_enabled"] is False
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()
    assert (await client.get("/api/config")).json()["assistant_enabled"] is True
