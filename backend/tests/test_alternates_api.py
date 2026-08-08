"""POST /api/route/alternates: auth, the exactly-2-waypoints schema
constraint (rejected before Valhalla ever sees it), count bounds, and the
happy path against a real captured Valhalla response with one alternate."""

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
# Tring -> Wendover, road preset, "alternates": 2 requested. Valhalla
# returned only 1 (primary 8.237 km, alternate 11.785 km).
ROUTE_ALTERNATES_REAL = json.loads((FIXTURES / "route_alternates_real.json").read_text())
HEIGHT_RESPONSE = {"range_height": [[0, 55.0], [4000, 60.5], [8237, 48.0]]}

ORIGIN = {"lat": 51.7926, "lon": -0.6606}
DESTINATION = {"lat": 51.7645, "lon": -0.7442}


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


async def ask(client: AsyncClient, **body: Any) -> Any:
    payload = {"waypoints": [ORIGIN, DESTINATION], **body}
    return await client.post("/api/route/alternates", json=payload)


async def test_alternates_needs_a_session(client: AsyncClient) -> None:
    response = await ask(client)
    assert response.status_code == 401


async def test_one_waypoint_is_rejected_before_valhalla(client: AsyncClient) -> None:
    """The length-2 constraint is a schema check, not a Valhalla error - no
    Valhalla mock is registered, so a request that reached it would error
    with a connection failure, not a clean 422."""
    await register(client)
    response = await ask(client, waypoints=[ORIGIN])
    assert response.status_code == 422


async def test_three_waypoints_is_rejected_before_valhalla(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, waypoints=[ORIGIN, {"lat": 51.78, "lon": -0.70}, DESTINATION])
    assert response.status_code == 422


async def test_count_zero_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, count=0)
    assert response.status_code == 422


async def test_count_four_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await ask(client, count=4)
    assert response.status_code == 422


@respx.mock
async def test_happy_path_returns_primary_and_the_one_alternate(client: AsyncClient) -> None:
    route_mock = respx.post(f"{VALHALLA}/route").respond(json=ROUTE_ALTERNATES_REAL)
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)

    response = await ask(client, count=2)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["primary"]["distance_m"] == pytest.approx(8237.0)
    assert len(body["alternates"]) == 1
    assert body["alternates"][0]["distance_m"] == pytest.approx(11785.0)

    sent = json.loads(route_mock.calls[0].request.content)
    assert sent["alternates"] == 2
    assert sent["locations"] == [
        {"lat": ORIGIN["lat"], "lon": ORIGIN["lon"], "type": "break"},
        {"lat": DESTINATION["lat"], "lon": DESTINATION["lon"], "type": "break"},
    ]


@respx.mock
async def test_primary_and_alternate_both_carry_ride_time(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/route").respond(json=ROUTE_ALTERNATES_REAL)
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)
    await client.patch("/api/settings", json={"flat_speed_kmh": 20.0})

    response = await ask(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["primary"]["ride_time"]) == len(body["primary"]["elevation"])
    assert len(body["alternates"][0]["ride_time"]) == len(body["alternates"][0]["elevation"])


@respx.mock
async def test_custom_costing_options_are_forwarded(client: AsyncClient) -> None:
    mock = respx.post(f"{VALHALLA}/route").respond(json=ROUTE_ALTERNATES_REAL)
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
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
