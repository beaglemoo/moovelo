"""API wiring for ride time: computed on read, never stored, never touching
duration_s. Unit coverage of the model itself lives in test_ride_time.py."""

import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.schemas import RouteResponse
from app.services.valhalla import ValhallaClient
from tests.conftest import register
from tests.test_auth_routes import TRACE_ATTRS_RESPONSE, WAYPOINTS, save_body

VALHALLA = "http://valhalla.test"

PLAN_TRIP = {
    "trip": {
        "summary": {"length": 1.93, "time": 463.0},
        "legs": [
            {
                "shape": "cfjyHhc}Ig@i@Zf@",
                "maneuvers": [
                    {"type": 1, "instruction": "Ride southwest.", "begin_shape_index": 0},
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 1},
                ],
            }
        ],
    }
}
HEIGHT_RESPONSE = {"range_height": [[0, 55.0], [500, 63.0], [1200, 70.0], [1930, 66.0]]}


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


@respx.mock
async def test_plan_route_carries_ride_time_sized_to_elevation(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/route").respond(json=PLAN_TRIP)
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)

    response = await client.post("/api/route", json={"waypoints": WAYPOINTS, "preset": "road"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["ride_time"]) == len(body["elevation"])
    assert body["ride_time"][0]["time_s"] == 0.0
    # duration_s is untouched - it is Valhalla's own summary time.
    assert body["duration_s"] == 463.0


async def test_settings_change_alters_a_saved_routes_displayed_time(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    """The property that matters: ride_time is computed on read, not stored,
    so a settings change is reflected on an already-saved route without
    touching the route row at all."""
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    original_duration = created["duration_s"]
    before = created["ride_time"][-1]["time_s"]
    assert before > 0

    # Default flat_speed_kmh is 22; halving it should exactly double a
    # multiplicative model's total time (gradient/surface factors are
    # unchanged, and this fixture's grades are gentle enough not to hit the
    # model's speed floor).
    await client.patch("/api/settings", json={"flat_speed_kmh": 11.0})

    fetched = (await client.get(f"/api/routes/{created['id']}")).json()
    after = fetched["ride_time"][-1]["time_s"]

    assert after == pytest.approx(before * 2, rel=1e-6)
    # duration_s never moves.
    assert fetched["duration_s"] == original_duration == snapshot.duration_s


async def test_share_endpoint_uses_default_settings_regardless_of_owner(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    await client.patch("/api/settings", json={"flat_speed_kmh": 10.0})
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    owner_time = created["ride_time"][-1]["time_s"]
    token = (await client.post(f"/api/routes/{created['id']}/share")).json()["share_token"]

    await client.post("/api/auth/logout")
    shared = (await client.get(f"/api/shared/{token}")).json()

    # Faster than the owner's slow setting, since the anonymous viewer gets
    # the plain 22 km/h default rather than the owner's 10 km/h.
    assert shared["ride_time"][-1]["time_s"] < owner_time
    assert shared["duration_s"] == created["duration_s"]


@respx.mock
async def test_duplicate_and_reverse_responses_carry_ride_time(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    respx.post(f"{VALHALLA}/route").respond(json=PLAN_TRIP)
    respx.post(f"{VALHALLA}/height").respond(json={"range_height": []})
    respx.post(f"{VALHALLA}/trace_attributes").respond(json=TRACE_ATTRS_RESPONSE)
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()

    duplicate = (await client.post(f"/api/routes/{created['id']}/duplicate")).json()
    assert len(duplicate["ride_time"]) == len(duplicate["elevation"])
    assert duplicate["duration_s"] == created["duration_s"]

    reversed_route = (await client.post(f"/api/routes/{created['id']}/reverse")).json()
    assert len(reversed_route["ride_time"]) == len(reversed_route["elevation"])
    assert reversed_route["duration_s"] == pytest.approx(463.0)


async def test_export_endpoints_are_untouched_by_ride_time(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    """Grep-level confirmation, executed: build_fit/GPX still consume
    duration_s only, not ride_time."""
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()

    fit_response = await client.get(f"/api/routes/{created['id']}/export.fit")
    assert fit_response.status_code == 200
    gpx_response = await client.get(f"/api/routes/{created['id']}/export.gpx")
    assert gpx_response.status_code == 200
