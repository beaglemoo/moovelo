"""POST /api/route: exclude_locations validation at the schema boundary.

Payload-forwarding is covered in test_valhalla.py; this only checks the
endpoint enforces RouteRequest's max_length cap.
"""

import json

import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.services.polyline import encode_polyline6
from app.services.valhalla import ValhallaClient
from tests.conftest import register

VALHALLA = "http://valhalla.test"

WAYPOINTS = [{"lat": 53.7996, "lon": -1.5491}, {"lat": 53.785, "lon": -1.575}]

TRIP_RESPONSE = {
    "trip": {
        "summary": {"length": 1.93, "time": 470.0},
        "legs": [
            {
                "shape": encode_polyline6([(53.7996, -1.5491), (53.785, -1.575)]),
                "maneuvers": [
                    {"type": 1, "instruction": "Ride southwest.", "begin_shape_index": 0},
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 1},
                ],
            }
        ],
    }
}


def _exclusions(n: int) -> list[dict[str, float]]:
    return [{"lat": 53.79 + i * 0.0001, "lon": -1.55} for i in range(n)]


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


async def test_eleven_exclude_locations_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        "/api/route",
        json={"waypoints": WAYPOINTS, "preset": "road", "exclude_locations": _exclusions(11)},
    )

    assert response.status_code == 422


@respx.mock
async def test_ten_exclude_locations_is_accepted_and_forwarded(client: AsyncClient) -> None:
    route_mock = respx.post(f"{VALHALLA}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{VALHALLA}/height").respond(json={"range_height": []})
    await register(client)

    exclusions = _exclusions(10)
    response = await client.post(
        "/api/route",
        json={"waypoints": WAYPOINTS, "preset": "road", "exclude_locations": exclusions},
    )

    assert response.status_code == 200, response.text
    sent = json.loads(route_mock.calls[0].request.content)
    assert sent["exclude_locations"] == exclusions
