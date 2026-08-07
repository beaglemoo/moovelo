import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.schemas import ElevationPoint, RouteRequest, Waypoint
from app.services.geo import evenly_sampled, haversine, resample_by_distance
from app.services.polyline import encode_polyline6
from app.services.presets import PRESETS
from app.services.valhalla import ValhallaClient, ascent_descent

BASE = "http://valhalla.test"

SHAPE = [(53.7996, -1.5491), (53.8008, -1.5523), (53.7950, -1.5600)]

FIXTURES = Path(__file__).parent / "fixtures"
# A real edge_walk response: 133 edges over a 16.949 km route, captured
# against the dev Valhalla. sum(edge.length) equals the route's exact
# summary distance, including the boundary edges that carry
# source_percent_along / target_percent_along.
TRACE_ATTRS_REAL = json.loads((FIXTURES / "trace_attributes_real.json").read_text())
# A real edge_walk 400: an unmatched track fails this by design.
TRACE_ATTRS_FAIL = json.loads((FIXTURES / "trace_attributes_fail.json").read_text())

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


def test_evenly_sampled_limits_points() -> None:
    points = [(float(i), float(i)) for i in range(2000)]
    sampled = evenly_sampled(points, 500)
    assert len(sampled) == 500
    assert sampled[0] == points[0]
    assert sampled[-1] == points[-1]


def test_ascent_descent() -> None:
    profile = [
        ElevationPoint(dist_m=d, elev_m=e)
        for d, e in [(0, 100.0), (100, 110.0), (200, 105.0), (300, 120.0)]
    ]
    ascent, descent = ascent_descent(profile)
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


@respx.mock
async def test_chunk_boundaries_do_not_add_arrival_cues_mid_route() -> None:
    """Every chunk is a trip, so every chunk ends in a destination maneuver -
    but only the last one is a real arrival."""
    respx.post(f"{BASE}/trace_route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    long_track = [(53.7996 + i * 0.001, -1.5491) for i in range(2500)]
    result = await ValhallaClient(base_url=BASE).trace_route(long_track, "road")

    arrivals = [m for leg in result.legs for m in leg.maneuvers if int(m.get("type", 0)) == 4]
    assert len(arrivals) == 1
    assert result.legs[-1].maneuvers[-1]["type"] == 4


@respx.mock
async def test_one_unmatchable_chunk_keeps_the_rest_of_the_ride() -> None:
    """Chunking exists so a failure costs one chunk, not the whole track."""
    responses = [
        httpx.Response(200, json=TRIP_RESPONSE),
        httpx.Response(400, json={"error": "No suitable edges near location"}),
        httpx.Response(200, json=TRIP_RESPONSE),
    ]
    respx.post(f"{BASE}/trace_route").mock(side_effect=responses)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    long_track = [(53.7996 + i * 0.001, -1.5491) for i in range(2500)]
    result = await ValhallaClient(base_url=BASE).trace_route(long_track, "road")

    # Three legs: two matched with cues, one kept as a plain line.
    assert len(result.legs) == 3
    assert [bool(leg.maneuvers) for leg in result.legs] == [True, False, True]


@respx.mock
async def test_an_unreachable_engine_still_fails_the_whole_trace() -> None:
    """A 5xx is retryable, so it must not be downgraded to a partial result."""
    respx.post(f"{BASE}/trace_route").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(HTTPException) as exc:
        await ValhallaClient(base_url=BASE).trace_route(SHAPE, "road")
    assert exc.value.status_code == 503


@respx.mock
async def test_a_failed_final_chunk_still_leaves_an_arrival_cue() -> None:
    """Destinations are stripped from every chunk but the last that matched,
    so a route whose final stretch could not be matched still ends properly."""
    responses = [
        httpx.Response(200, json=TRIP_RESPONSE),
        httpx.Response(200, json=TRIP_RESPONSE),
        httpx.Response(400, json={"error": "No suitable edges near location"}),
    ]
    respx.post(f"{BASE}/trace_route").mock(side_effect=responses)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    long_track = [(53.7996 + i * 0.001, -1.5491) for i in range(2500)]
    result = await ValhallaClient(base_url=BASE).trace_route(long_track, "road")

    arrivals = [m for leg in result.legs for m in leg.maneuvers if int(m.get("type", 0)) == 4]
    assert len(arrivals) == 1


@respx.mock
async def test_trace_attributes_success_bucket_sums_are_exact() -> None:
    mock = respx.post(f"{BASE}/trace_attributes").respond(json=TRACE_ATTRS_REAL)

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes(SHAPE, "road")

    assert result is not None
    edges = TRACE_ATTRS_REAL["edges"]
    expected_surface: dict[str, float] = {}
    expected_road_class: dict[str, float] = {}
    expected_use: dict[str, float] = {}
    expected_cycle_lane = 0.0
    for edge in edges:
        length_m = edge["length"] * 1000.0
        expected_surface[edge["surface"]] = expected_surface.get(edge["surface"], 0.0) + length_m
        expected_road_class[edge["road_class"]] = (
            expected_road_class.get(edge["road_class"], 0.0) + length_m
        )
        expected_use[edge["use"]] = expected_use.get(edge["use"], 0.0) + length_m
        if edge["cycle_lane"] != "none":
            expected_cycle_lane += length_m

    # The 133 real edges sum to exactly the route's 16.949 km summary
    # distance, boundary edges included.
    assert result.total_m == pytest.approx(16949.0)
    assert result.surface_m == pytest.approx(expected_surface)
    assert result.road_class_m == pytest.approx(expected_road_class)
    assert result.use_m == pytest.approx(expected_use)
    assert result.cycle_lane_m == pytest.approx(expected_cycle_lane)

    sent = json.loads(mock.calls[0].request.content)
    assert sent["shape_match"] == "edge_walk"
    assert sent["costing"] == "bicycle"
    assert sent["costing_options"]["bicycle"] == PRESETS["road"]
    assert sent["filters"]["attributes"] == [
        "edge.length",
        "edge.surface",
        "edge.road_class",
        "edge.use",
        "edge.cycle_lane",
    ]


@respx.mock
async def test_trace_attributes_percent_along_edges_contribute_raw_length() -> None:
    """source_percent_along and target_percent_along mark the first and last
    edge of the whole trace, but the real fixture's edge lengths already sum
    to the route's exact summary distance - Valhalla has accounted for the
    partial edges - so these keys need no correction here."""
    edges = TRACE_ATTRS_REAL["edges"]
    boundary = [edges[0], edges[-1]]
    assert "source_percent_along" in boundary[0]
    assert "target_percent_along" in boundary[1]
    respx.post(f"{BASE}/trace_attributes").respond(json={"units": "kilometers", "edges": boundary})

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes(SHAPE, "road")

    assert result is not None
    expected = (boundary[0]["length"] + boundary[1]["length"]) * 1000.0
    assert result.total_m == pytest.approx(expected)


@respx.mock
async def test_trace_attributes_edge_walk_mismatch_returns_none() -> None:
    """A real edge_walk 400: an unmatched track fails this by design and must
    degrade silently rather than block saving the route."""
    respx.post(f"{BASE}/trace_attributes").respond(status_code=400, json=TRACE_ATTRS_FAIL)

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes(SHAPE, "road")

    assert result is None


@respx.mock
async def test_trace_attributes_unreachable_engine_returns_none() -> None:
    """The opposite of trace_route's own unreachable-engine test
    (test_an_unreachable_engine_still_fails_the_whole_trace): surface is
    decorative and never touches FIT or export, so there is nothing here
    worth retrying for."""
    respx.post(f"{BASE}/trace_attributes").mock(side_effect=httpx.ConnectError("refused"))

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes(SHAPE, "road")

    assert result is None


@respx.mock
async def test_trace_attributes_chunks_long_shapes_without_overlap() -> None:
    edge = {
        "length": 0.1,
        "surface": "paved",
        "road_class": "residential",
        "use": "road",
        "cycle_lane": "none",
    }
    mock = respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [edge]}
    )
    long_shape = [(53.7996 + i * 0.0001, -1.5491) for i in range(2500)]

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes(long_shape, "road")

    assert mock.call_count == 3
    chunks_sent = [json.loads(call.request.content)["shape"] for call in mock.calls]
    assert [len(c) for c in chunks_sent] == [1000, 1000, 500]
    # Opposite of trace_route's chunking test: no point is shared between
    # consecutive chunks, since only aggregate metres are accumulated here.
    assert chunks_sent[0][-1] != chunks_sent[1][0]
    assert sum(len(c) for c in chunks_sent) == len(long_shape)
    assert result is not None
    assert result.total_m == pytest.approx(100.0 * 3)


@respx.mock
async def test_trace_attributes_drops_a_trailing_single_point_chunk() -> None:
    """A shape of exactly N*1000 + 1 points would otherwise end in a one-point
    chunk, which Valhalla rejects with a 400 - and that one bad chunk would
    null the surface for the whole route."""
    edge = {
        "length": 0.1,
        "surface": "paved",
        "road_class": "residential",
        "use": "road",
        "cycle_lane": "none",
    }
    mock = respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [edge]}
    )
    long_shape = [(53.7996 + i * 0.0001, -1.5491) for i in range(2001)]

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes(long_shape, "road")

    assert mock.call_count == 2
    chunks_sent = [json.loads(call.request.content)["shape"] for call in mock.calls]
    assert [len(c) for c in chunks_sent] == [1000, 1000]
    assert result is not None
    assert result.total_m == pytest.approx(100.0 * 2)
