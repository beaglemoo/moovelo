"""POST /api/assistant/suggest-name: gating, capping, and the index-absent
degradation path.

Reverse-geocode degradation needs no fixture flag - the places table is
simply empty in a throwaway test database, so reverse_geocode returns None
exactly as it does on an install where the indexer sidecar has never run.
"""

import json

import pytest
import respx
from httpx import AsyncClient, Response

from tests.conftest import register

GATEWAY = "https://gateway.test/v1"

WAYPOINTS = [{"lat": 51.7963, "lon": -0.6706}, {"lat": 51.8177, "lon": -0.6199}]


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "llm_base_url", GATEWAY)
    monkeypatch.setattr(settings, "llm_model", "m")


def _completion(content: str) -> Response:
    return Response(200, json={"choices": [{"message": {"content": content}}]})


# --- gating ------------------------------------------------------------


async def test_suggest_name_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": WAYPOINTS, "distance_m": 20_000, "ascent_m": 150},
    )
    assert response.status_code == 401


async def test_suggest_name_404s_when_unconfigured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")
    await register(client)
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": WAYPOINTS, "distance_m": 20_000, "ascent_m": 150},
    )
    assert response.status_code == 404


# --- name capping --------------------------------------------------------


@respx.mock
async def test_suggest_name_caps_and_cleans_the_reply(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    await register(client)
    hostile = (
        '"A very long ride out through the hills and back again along the towpath\n'
        'next to the canal, past the mill"'
    )
    respx.post(f"{GATEWAY}/chat/completions").mock(return_value=_completion(hostile))
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": WAYPOINTS, "distance_m": 42_000, "ascent_m": 300},
    )
    assert response.status_code == 200
    name = response.json()["name"]
    assert len(name) <= 60
    assert "\n" not in name
    assert '"' not in name


@respx.mock
async def test_suggest_name_strips_quotes_within_the_cap(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    await register(client)
    respx.post(f"{GATEWAY}/chat/completions").mock(return_value=_completion('"Tring Loop"'))
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": WAYPOINTS, "distance_m": 12_000, "ascent_m": 80},
    )
    assert response.json()["name"] == "Tring Loop"


@respx.mock
async def test_suggest_name_falls_back_when_the_reply_is_blank(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    await register(client)
    respx.post(f"{GATEWAY}/chat/completions").mock(return_value=_completion("   \n  "))
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": WAYPOINTS, "distance_m": 20_000, "ascent_m": 150},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "20 km ride"


# --- degradation when the place index is absent ---------------------------


@respx.mock
async def test_suggest_name_degrades_to_distance_only_facts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No places table rows means both reverse-geocode lookups return None,
    same as an install where the indexer has never run. The call must still
    go out, carrying only the numbers actually measured."""
    _configure(monkeypatch)
    await register(client)
    route = respx.post(f"{GATEWAY}/chat/completions").mock(
        return_value=_completion("Twenty Kilometre Ride")
    )
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": WAYPOINTS, "distance_m": 20_000, "ascent_m": 150},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Twenty Kilometre Ride"

    sent = json.loads(route.calls[0].request.content)
    user_message = next(m["content"] for m in sent["messages"] if m["role"] == "user")
    assert "No place names are available" in user_message
    # The real numbers, not invented ones - distance in km, ascent in metres.
    assert "20.0 km" in user_message
    assert "150" in user_message
    # Never given a search tool: this is a single plain completion.
    assert "tools" not in sent


# --- validation ------------------------------------------------------------


async def test_suggest_name_rejects_a_single_waypoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    await register(client)
    response = await client.post(
        "/api/assistant/suggest-name",
        json={"waypoints": [WAYPOINTS[0]], "distance_m": 1000, "ascent_m": 10},
    )
    assert response.status_code == 422
