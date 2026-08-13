"""Stored natural-language summaries on public share pages.

The one rule that must never break: /api/shared/{token} is public and
unauthenticated, so no code path reachable from it may ever call the LLM -
that would let anonymous traffic spend the operator's money. Summaries are
generated only from share_route (an authenticated, owner-triggered write)
and served afterwards as plain stored text.
"""

from collections.abc import Iterator

import pytest
import respx
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LLMSettings
from app.schemas import RouteResponse
from tests.conftest import register
from tests.test_auth_routes import save_body

GATEWAY = "https://gateway.test/v1"


@pytest.fixture
def no_env_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate these tests from whatever LLM_* env vars the shell has set."""
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_provider_order", "")
    yield


async def configure_llm(db: AsyncSession) -> None:
    db.add(LLMSettings(id=True, base_url=GATEWAY, model="m"))
    await db.commit()


def mock_completion(text: str = "A gentle 1.9 km ride with 18 m of climbing.") -> respx.Route:
    return respx.post(f"{GATEWAY}/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": text}}]})
    )


# --- the hard constraint ----------------------------------------------------


@respx.mock
async def test_unauthenticated_shared_read_never_calls_the_llm(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    await configure_llm(db)
    completion = mock_completion()
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    token = (await client.post(f"/api/routes/{created['id']}/share")).json()["share_token"]
    assert completion.call_count == 1  # the owner-triggered share did call it, once

    await client.post("/api/auth/logout")
    response = await client.get(f"/api/shared/{token}")

    assert response.status_code == 200
    assert completion.call_count == 1, "the public read path must never call the LLM"


@respx.mock
async def test_no_state_a_shared_route_can_be_in_reaches_the_llm(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    """The guarantee above, for every branch rather than the happy one.

    The test that precedes this only ever reads a route whose summary is
    fresh, so it says nothing about the other two states `get_shared` can
    find a route in. That gap was demonstrated, not assumed: an LLM call
    injected into the stale branch of `get_shared` left all five tests in
    this file passing, including the one named "never calls the llm".

    So this walks each state a shared route can actually be in - summary
    fresh, summary stale, no summary at all - and asserts the gateway is
    untouched by the public read in every one. A regeneration-on-read
    "fix" for the stale case is the obvious way for someone to reintroduce
    anonymous spend, and this is what stops it.
    """
    await register(client)
    routes = {
        state: (await client.post("/api/routes", json=save_body(snapshot))).json()
        for state in ("fresh", "stale", "absent")
    }

    # Shared before the gateway is configured, so nothing is ever stored.
    tokens = {
        "absent": (await client.post(f"/api/routes/{routes['absent']['id']}/share")).json()[
            "share_token"
        ]
    }

    await configure_llm(db)
    completion = mock_completion()
    for state in ("fresh", "stale"):
        tokens[state] = (await client.post(f"/api/routes/{routes[state]['id']}/share")).json()[
            "share_token"
        ]

    # Re-route the stale one so its stored summary no longer describes it.
    reversed_shape = snapshot.model_copy(
        update={"legs": [leg.model_copy() for leg in reversed(snapshot.legs)]}
    )
    patched = await client.patch(
        f"/api/routes/{routes['stale']['id']}", json={"snapshot": reversed_shape.model_dump()}
    )
    assert patched.status_code == 200

    await client.post("/api/auth/logout")
    generated = completion.call_count
    assert generated == 2, "one owner-triggered generation each for fresh and stale"

    expected_summary = {"fresh": True, "stale": False, "absent": False}
    for state, token in tokens.items():
        response = await client.get(f"/api/shared/{token}")
        assert response.status_code == 200, state
        # Spend first: a read that regenerates identical text would still be
        # a bug, and this is the assertion that should name it.
        assert completion.call_count == generated, f"the {state} read path called the LLM"
        assert (response.json()["summary"] is not None) is expected_summary[state], state


# --- generation on share -----------------------------------------------------


@respx.mock
async def test_share_generates_and_serves_a_summary(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    await configure_llm(db)
    mock_completion("A gentle 1.9 km ride with 18 m of climbing.")
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()

    shared = await client.post(f"/api/routes/{created['id']}/share")
    token = shared.json()["share_token"]

    await client.post("/api/auth/logout")
    public = (await client.get(f"/api/shared/{token}")).json()
    assert public["summary"] == "A gentle 1.9 km ride with 18 m of climbing."


@respx.mock
async def test_unconfigured_assistant_is_a_no_op(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    """No LLMSettings row and no env vars: sharing still succeeds, with no
    summary and no outbound request attempted at all."""
    completion = mock_completion()
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()

    shared = await client.post(f"/api/routes/{created['id']}/share")
    assert shared.status_code == 200
    assert not completion.called

    token = shared.json()["share_token"]
    await client.post("/api/auth/logout")
    public = (await client.get(f"/api/shared/{token}")).json()
    assert public["summary"] is None


@respx.mock
async def test_rotating_an_unchanged_route_does_not_regenerate(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    """Rotating the link without editing the route already has a summary
    whose signature matches the current geometry - regenerating it would
    only spend money on text that would read the same."""
    await configure_llm(db)
    completion = mock_completion()
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()

    first = await client.post(f"/api/routes/{created['id']}/share")
    assert completion.call_count == 1

    second = await client.post(f"/api/routes/{created['id']}/share")
    assert completion.call_count == 1  # no second call
    assert second.json()["share_token"] != first.json()["share_token"]

    await client.post("/api/auth/logout")
    public = (await client.get(f"/api/shared/{second.json()['share_token']}")).json()
    assert public["summary"] is not None


# --- staleness ---------------------------------------------------------------


@respx.mock
async def test_stale_signature_suppresses_the_summary(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    """A route re-routed after being shared has a stored summary that no
    longer describes it; the read path must suppress it, not show it stale."""
    await configure_llm(db)
    mock_completion()
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    token = (await client.post(f"/api/routes/{created['id']}/share")).json()["share_token"]

    await client.post("/api/auth/logout")
    assert (await client.get(f"/api/shared/{token}")).json()["summary"] is not None

    await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "correct-horse-9"}
    )
    reversed_shape = snapshot.model_copy(
        update={"legs": [leg.model_copy() for leg in reversed(snapshot.legs)]}
    )
    patched = await client.patch(
        f"/api/routes/{created['id']}", json={"snapshot": reversed_shape.model_dump()}
    )
    assert patched.status_code == 200

    await client.post("/api/auth/logout")
    stale = await client.get(f"/api/shared/{token}")
    assert stale.json()["summary"] is None


async def test_share_still_works_when_llm_settings_is_missing(
    client: AsyncClient, db: AsyncSession, snapshot: RouteResponse, no_env_llm: None
) -> None:
    """An install downgraded past migration 0011 has no llm_settings table.
    share_route assigns route.share_token, then asks resolve_llm_config for a
    summary - which used to roll the whole transaction back on the missing
    table, discarding the token and 500ing via MissingGreenlet. The savepoint
    leaves the caller's work intact, so sharing degrades (no summary) rather
    than failing."""
    from sqlalchemy import text

    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    await db.execute(text("DROP TABLE llm_settings"))
    await db.commit()

    shared = await client.post(f"/api/routes/{created['id']}/share")
    assert shared.status_code == 200, shared.text
    token = shared.json()["share_token"]
    assert token
    # The token really persisted - the summary's failed lookup did not take
    # the share write down with it - and the public read serves it.
    assert (await client.get(f"/api/shared/{token}")).status_code == 200
