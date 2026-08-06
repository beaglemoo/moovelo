import json

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.schemas import ElevationPoint, RouteRequest, Waypoint
from app.services.geo import haversine, resample_by_distance
from app.services.presets import PRESETS
from app.services.valhalla import ValhallaClient, _ascent_descent, _downsample
from tests.test_polyline import encode_polyline6

BASE = "http://valhalla.test"

SHAPE = [(53.7996, -1.5491), (53.8008, -1.5523), (53.7950, -1.5600)]

TRIP_RESPONSE = {
    "trip": {
        "summary": {"length": 1.234, "time": 296.0},
        "legs": [
            {
                "shape": encode_polyline6(SHAPE),
                "maneuvers": [
                    {"type": 1, "instruction": "Ride west.", "begin_shape_index": 0},
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 2},
                ],
            }
        ],
    }
}

HEIGHT_RESPONSE = {"range_height": [[0, 55.0], [400, 60.5], [1234, 48.0]]}


def make_request() -> RouteRequest:
    return RouteRequest(
        waypoints=[Waypoint(lat=53.7996, lon=-1.5491), Waypoint(lat=53.7950, lon=-1.5600)],
        preset="gravel",
    )


@respx.mock
async def test_route_success() -> None:
    route_mock = respx.post(f"{BASE}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    result = await client.route(make_request())

    assert result.distance_m == pytest.approx(1234.0)
    assert result.duration_s == pytest.approx(296.0)
    assert len(result.legs) == 1
    assert len(result.legs[0].maneuvers) == 2
    assert result.legs[0].maneuvers[0]["instruction"] == "Ride west."
    assert [p.elev_m for p in result.elevation] == [55.0, 60.5, 48.0]
    assert result.ascent_m == pytest.approx(5.5)
    assert result.descent_m == pytest.approx(12.5)

    sent = json.loads(route_mock.calls[0].request.content)
    assert sent["costing"] == "bicycle"
    assert sent["costing_options"]["bicycle"]["bicycle_type"] == "Cross"
    assert [loc["type"] for loc in sent["locations"]] == ["break", "break"]


@respx.mock
async def test_route_valhalla_error_maps_to_422() -> None:
    respx.post(f"{BASE}/route").respond(
        status_code=400, json={"error": "No path could be found for input"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.route(make_request())
    assert exc.value.status_code == 422
    assert "No path" in exc.value.detail


@respx.mock
async def test_route_unreachable_maps_to_503() -> None:
    respx.post(f"{BASE}/route").mock(side_effect=httpx.ConnectError("refused"))
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.route(make_request())
    assert exc.value.status_code == 503


@respx.mock
async def test_height_failure_returns_route_without_profile() -> None:
    respx.post(f"{BASE}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(status_code=400, json={"error": "no elevation"})
    client = ValhallaClient(base_url=BASE)
    result = await client.route(make_request())
    assert result.elevation == []
    assert result.ascent_m == 0.0


def test_downsample_limits_points() -> None:
    points = [(float(i), float(i)) for i in range(2000)]
    sampled = _downsample(points, 500)
    assert len(sampled) == 500
    assert sampled[0] == points[0]
    assert sampled[-1] == points[-1]


def test_ascent_descent() -> None:
    profile = [
        ElevationPoint(dist_m=d, elev_m=e)
        for d, e in [(0, 100.0), (100, 110.0), (200, 105.0), (300, 120.0)]
    ]
    ascent, descent = _ascent_descent(profile)
    assert ascent == pytest.approx(25.0)
    assert descent == pytest.approx(5.0)


@respx.mock
async def test_trace_route_snaps_a_track_and_recovers_maneuvers() -> None:
    trace_mock = respx.post(f"{BASE}/trace_route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_route(SHAPE, "quiet")

    payload = json.loads(trace_mock.calls.last.request.content)
    assert payload["shape_match"] == "map_snap"
    assert payload["costing"] == "bicycle"
    assert payload["costing_options"]["bicycle"] == PRESETS["quiet"]
    # The imported track had no instructions; matching gives it some.
    assert [m["instruction"] for m in result.legs[0].maneuvers] == [
        "Ride west.",
        "You have arrived.",
    ]
    assert result.distance_m == pytest.approx(1234.0)
    assert result.ascent_m == pytest.approx(5.5)


@respx.mock
async def test_trace_route_thins_densely_recorded_tracks() -> None:
    trace_mock = respx.post(f"{BASE}/trace_route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    # 400 points spread over ~1m each: a rider crawling up a climb.
    dense = [(53.7996 + i * 0.00001, -1.5491) for i in range(400)]
    client = ValhallaClient(base_url=BASE)
    await client.trace_route(dense, "road")

    sent = json.loads(trace_mock.calls.last.request.content)["shape"]
    assert len(sent) < 40
    assert sent[0] == {"lat": dense[0][0], "lon": dense[0][1]}
    assert sent[-1] == {"lat": dense[-1][0], "lon": dense[-1][1]}


@respx.mock
async def test_trace_route_chunks_long_tracks_and_sums_them() -> None:
    trace_mock = respx.post(f"{BASE}/trace_route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    # Points 100m apart survive thinning, so this needs several requests.
    long_track = [(53.7996 + i * 0.001, -1.5491) for i in range(2500)]
    client = ValhallaClient(base_url=BASE)
    result = await client.trace_route(long_track, "road")

    assert trace_mock.call_count == 3
    # Chunks share a boundary point so the matched legs join up.
    first, second = (json.loads(call.request.content)["shape"] for call in trace_mock.calls[:2])
    assert first[-1] == second[0]
    assert len(result.legs) == 3
    assert result.distance_m == pytest.approx(1234.0 * 3)


@respx.mock
async def test_trace_route_unmatchable_track_maps_to_422() -> None:
    respx.post(f"{BASE}/trace_route").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.trace_route(SHAPE, "road")
    assert exc.value.status_code == 422
    assert "map extract" in exc.value.detail


async def test_trace_route_rejects_a_single_point_track() -> None:
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.trace_route([(53.7996, -1.5491)], "road")
    assert exc.value.status_code == 422


def test_resample_by_distance_keeps_both_ends() -> None:
    points = [(53.7996 + i * 0.00001, -1.5491) for i in range(500)]
    thinned = resample_by_distance(points, 15.0)
    assert thinned[0] == points[0]
    assert thinned[-1] == points[-1]
    assert 2 < len(thinned) < len(points)
    gaps = [haversine(a, b) for a, b in zip(thinned, thinned[1:], strict=False)]
    assert min(gaps[:-1]) >= 15.0


def test_resample_by_distance_passes_short_tracks_through() -> None:
    points = [(53.7996, -1.5491), (53.7997, -1.5492)]
    assert resample_by_distance(points, 15.0) == points
