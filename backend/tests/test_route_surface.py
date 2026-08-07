"""Surface breakdown: POST /api/route/surface and its round trip through
saved routes and share links."""

import json

import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.schemas import RouteResponse, SurfaceBreakdown
from app.services.valhalla import ValhallaClient
from tests.conftest import register
from tests.test_auth_routes import save_body

VALHALLA = "http://valhalla.test"

# [lon, lat], matching GeoJSON and PoiQuery.line.
LINE = [[-1.5491, 53.7996], [-1.5523, 53.8008], [-1.5600, 53.7950]]

TRACE_ATTRS_RESPONSE = {
    "units": "kilometers",
    "edges": [
        {
            "length": 0.5,
            "surface": "paved",
            "road_class": "residential",
            "use": "road",
            "cycle_lane": "lane",
        },
        {
            "length": 0.3,
            "surface": "gravel",
            "road_class": "path",
            "use": "path",
            "cycle_lane": "none",
        },
    ],
}


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


async def test_route_surface_needs_a_session(client: AsyncClient) -> None:
    response = await client.post("/api/route/surface", json={"line": LINE})
    assert response.status_code == 401


@respx.mock
async def test_route_surface_returns_the_breakdown(client: AsyncClient) -> None:
    mock = respx.post(f"{VALHALLA}/trace_attributes").respond(json=TRACE_ATTRS_RESPONSE)
    await register(client)

    response = await client.post("/api/route/surface", json={"line": LINE, "preset": "gravel"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_m"] == pytest.approx(800.0)
    assert body["surface_m"] == {"paved": 500.0, "gravel": 300.0}
    assert body["road_class_m"] == {"residential": 500.0, "path": 300.0}
    assert body["use_m"] == {"road": 500.0, "path": 300.0}
    # Only the "lane" edge counts - "none" is not marked infrastructure.
    assert body["cycle_lane_m"] == pytest.approx(500.0)

    sent = json.loads(mock.calls[0].request.content)
    assert sent["shape_match"] == "edge_walk"
    assert sent["costing_options"]["bicycle"]["bicycle_type"] == "Cross"
    # [lon, lat] in, (lat, lon) out to Valhalla.
    assert sent["shape"][0] == {"lat": LINE[0][1], "lon": LINE[0][0]}


@respx.mock
async def test_route_surface_is_null_when_edge_walk_fails(client: AsyncClient) -> None:
    """An unmatched line fails edge_walk by design; the endpoint says so with
    a null body rather than an error the planner would have to handle."""
    respx.post(f"{VALHALLA}/trace_attributes").respond(
        status_code=400, json={"error": "edge_walk algorithm failed to find exact route match"}
    )
    await register(client)

    response = await client.post("/api/route/surface", json={"line": LINE})

    assert response.status_code == 200
    assert response.json() is None


async def test_a_line_of_one_point_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await client.post("/api/route/surface", json={"line": [LINE[0]]})
    assert response.status_code == 422


def _surface() -> SurfaceBreakdown:
    return SurfaceBreakdown(
        total_m=800.0,
        surface_m={"paved": 500.0, "gravel": 300.0},
        road_class_m={"residential": 500.0, "path": 300.0},
        use_m={"road": 500.0, "path": 300.0},
        cycle_lane_m=500.0,
    )


async def test_saving_a_route_with_surface_round_trips(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    with_surface = snapshot.model_copy(update={"surface": _surface()})

    created = (await client.post("/api/routes", json=save_body(with_surface))).json()
    assert created["surface"]["total_m"] == pytest.approx(800.0)
    assert created["surface"]["cycle_lane_m"] == pytest.approx(500.0)

    fetched = (await client.get(f"/api/routes/{created['id']}")).json()
    assert fetched["surface"]["surface_m"] == {"paved": 500.0, "gravel": 300.0}


async def test_saving_a_route_without_surface_reads_back_null(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    """Pre-Phase-7 snapshots, and any route whose edge_walk match failed,
    carry no surface breakdown - the None default must survive both the
    write and the read."""
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    assert created["surface"] is None

    fetched = (await client.get(f"/api/routes/{created['id']}")).json()
    assert fetched["surface"] is None


async def test_share_carries_the_surface_breakdown(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    with_surface = snapshot.model_copy(update={"surface": _surface()})
    created = (await client.post("/api/routes", json=save_body(with_surface))).json()
    token = (await client.post(f"/api/routes/{created['id']}/share")).json()["share_token"]

    await client.post("/api/auth/logout")
    shared = (await client.get(f"/api/shared/{token}")).json()

    assert shared["surface"]["surface_m"] == {"paved": 500.0, "gravel": 300.0}
