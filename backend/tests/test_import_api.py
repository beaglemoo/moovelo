"""Importing a route file through the API."""

import httpx
import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.services.polyline import encode_polyline6
from app.services.valhalla import ValhallaClient
from tests.conftest import register

VALHALLA = "http://valhalla.test"

# A track that wanders far enough that thinning keeps every point.
TRACK = [(53.7996, -1.5491), (53.8008, -1.5523), (53.7950, -1.5600)]

GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>Canal towpath</name></metadata>
  <trk><trkseg>
    <trkpt lat="53.7996" lon="-1.5491"><ele>55.0</ele></trkpt>
    <trkpt lat="53.8008" lon="-1.5523"><ele>63.0</ele></trkpt>
    <trkpt lat="53.7950" lon="-1.5600"><ele>48.0</ele></trkpt>
  </trkseg></trk>
</gpx>
"""

TRACE_RESPONSE = {
    "trip": {
        "summary": {"length": 1.93, "time": 463.0},
        "legs": [
            {
                "shape": encode_polyline6(TRACK),
                "maneuvers": [
                    {"type": 1, "instruction": "Ride southwest.", "begin_shape_index": 0},
                    {
                        "type": 15,
                        "instruction": "Turn left onto Mill Lane.",
                        "begin_shape_index": 1,
                    },
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 2},
                ],
            }
        ],
    }
}

HEIGHT_RESPONSE = {"range_height": [[0, 55.0], [900, 63.0], [1930, 48.0]]}


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


async def upload(http: AsyncClient, data: bytes = GPX, filename: str = "ride.gpx"):  # type: ignore[no-untyped-def]
    return await http.post(
        "/api/routes/import",
        files={"file": (filename, data, "application/gpx+xml")},
        data={"preset": "gravel"},
    )


@respx.mock
async def test_import_saves_a_matched_route_with_cues(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/trace_route").respond(json=TRACE_RESPONSE)
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)

    response = await upload(client)

    assert response.status_code == 201, response.text
    route = response.json()
    assert route["name"] == "Canal towpath"
    assert route["source"] == "imported"
    assert route["preset"] == "gravel"
    # The file had no instructions; matching gave it some.
    assert [m["instruction"] for m in route["legs"][0]["maneuvers"]][
        1
    ] == "Turn left onto Mill Lane."
    assert route["distance_m"] == pytest.approx(1930.0)
    # Only the endpoints - an imported line is not reconstructible from waypoints.
    assert len(route["waypoints"]) == 2
    assert route["waypoints"][0]["lat"] == pytest.approx(53.7996)

    listed = (await client.get("/api/routes")).json()
    assert [r["source"] for r in listed] == ["imported"]


@respx.mock
async def test_unmatchable_track_is_kept_without_cues(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/trace_route").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)

    response = await upload(client)

    # The ride still exists, so keep the line and lose only the turn cues.
    assert response.status_code == 201, response.text
    route = response.json()
    assert route["legs"][0]["maneuvers"] == []
    assert route["distance_m"] > 0
    # Elevation falls back to the file's own values.
    assert [p["elev_m"] for p in route["elevation"]] == [55.0, 63.0, 48.0]
    assert route["ascent_m"] == pytest.approx(8.0)


@respx.mock
async def test_name_falls_back_to_the_filename(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/trace_route").respond(json=TRACE_RESPONSE)
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)

    nameless = GPX.replace(b"<metadata><name>Canal towpath</name></metadata>", b"")
    response = await upload(client, nameless, "peak_district-loop.gpx")

    assert response.json()["name"] == "peak district loop"


async def test_unreadable_file_is_rejected_with_a_usable_message(client: AsyncClient) -> None:
    await register(client)
    response = await upload(client, b"%PDF-1.7 not a route", "notes.pdf")
    assert response.status_code == 422
    assert "Unsupported file type" in response.json()["detail"]


async def test_import_requires_authentication(client: AsyncClient) -> None:
    assert (await upload(client)).status_code == 401


@respx.mock
async def test_unreachable_routing_engine_is_retryable_not_a_silent_downgrade(
    client: AsyncClient,
) -> None:
    """A 503 means try again later; storing a cueless route would make the
    loss permanent even though the file would match fine once Valhalla is up."""
    respx.post(f"{VALHALLA}/trace_route").mock(side_effect=httpx.ConnectError("refused"))
    await register(client)

    response = await upload(client)

    assert response.status_code == 503
    assert (await client.get("/api/routes")).json() == []


@respx.mock
async def test_reverse_works_for_an_unmatched_import(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/trace_route").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    respx.post(f"{VALHALLA}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)
    imported = (await upload(client)).json()
    assert imported["legs"][0]["maneuvers"] == []

    # Reverse must fall back the same way import did, rather than 422ing.
    response = await client.post(f"/api/routes/{imported['id']}/reverse")

    assert response.status_code == 201, response.text
    assert response.json()["name"].endswith("(reversed)")
