"""POST /api/route/isochrone: auth, contour validation, and the happy path
against a real captured Valhalla response."""

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.services.valhalla import ValhallaClient
from tests.conftest import register

VALHALLA = "http://valhalla.test"

FIXTURES = Path(__file__).parent / "fixtures"
ISOCHRONE_REAL = json.loads((FIXTURES / "isochrone_real.json").read_text())

ORIGIN = {"lat": 51.7926, "lon": -0.6606}


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


async def ask(client: AsyncClient, **body: Any) -> Any:
    payload = {
        "origin": ORIGIN,
        "contours": [{"minutes": 60, "color": "b58900"}],
        **body,
    }
    return await client.post("/api/route/isochrone", json=payload)


async def test_isochrone_needs_a_session(client: AsyncClient) -> None:
    response = await ask(client)
    assert response.status_code == 401


async def test_no_contours_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, contours=[])
    assert response.status_code == 422


async def test_more_than_four_contours_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, contours=[{"minutes": m} for m in (10, 20, 30, 40, 50)])
    assert response.status_code == 422


async def test_both_minutes_and_km_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, contours=[{"minutes": 30, "km": 10}])
    assert response.status_code == 422


async def test_neither_minutes_nor_km_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, contours=[{"color": "268bd2"}])
    assert response.status_code == 422


async def test_bad_color_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, contours=[{"minutes": 30, "color": "#268bd2"}])
    assert response.status_code == 422


@respx.mock
async def test_happy_path_returns_the_fixtures_features(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/isochrone").respond(json=ISOCHRONE_REAL)
    await register(client)

    response = await ask(client, contours=[{"minutes": 30}, {"minutes": 60}])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["features"] == ISOCHRONE_REAL["features"]


@respx.mock
async def test_custom_costing_options_are_forwarded(client: AsyncClient) -> None:
    mock = respx.post(f"{VALHALLA}/isochrone").respond(json=ISOCHRONE_REAL)
    await register(client)

    custom = {
        "bicycle_type": "Mountain",
        "cycling_speed": 14,
        "use_roads": 0.1,
        "use_hills": 0.9,
        "avoid_bad_surfaces": 0.0,
    }
    response = await ask(client, costing_options=custom)

    assert response.status_code == 200, response.text
    sent = json.loads(mock.calls[0].request.content)
    assert sent["costing_options"]["bicycle"] == custom
