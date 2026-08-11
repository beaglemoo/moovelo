import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.schemas import (
    BicycleCostingOptions,
    ElevationPoint,
    IsochroneContour,
    RouteRequest,
    Waypoint,
)
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
# A real /isochrone response: 2 polygon features (30 and 60 minute contours),
# captured against the dev Valhalla with the road preset.
ISOCHRONE_REAL = json.loads((FIXTURES / "isochrone_real.json").read_text())
# A real /route response with alternates: Tring -> Wendover, road preset,
# "alternates": 2 requested. Valhalla returned only 1 (primary 8.237 km,
# alternate 11.785 km) - fewer than requested is normal, not a failure.
ROUTE_ALTERNATES_REAL = json.loads((FIXTURES / "route_alternates_real.json").read_text())
# A real map_snap trace_attributes response: the exact shape of a real
# /route leg near Tring (51.7955,-0.6580 -> 51.8045,-0.6560, gravel costing,
# 1.335 km) fed straight back in as a trace. 30 edges over 5 distinct way
# ids; sum(edge.length) is 1.334 km, matching the route's own summary
# distance to the nearest metre.
TRACE_ATTRS_MAP_SNAP_REAL = json.loads(
    (FIXTURES / "trace_attributes_map_snap_real.json").read_text()
)

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


CUSTOM_OPTIONS = BicycleCostingOptions(
    bicycle_type="Mountain",
    cycling_speed=14,
    use_roads=0.1,
    use_hills=0.9,
    avoid_bad_surfaces=0.0,
)


@respx.mock
async def test_route_with_custom_costing_sends_the_custom_bundle() -> None:
    route_mock = respx.post(f"{BASE}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    request = RouteRequest(
        waypoints=[Waypoint(lat=53.7996, lon=-1.5491), Waypoint(lat=53.7950, lon=-1.5600)],
        preset="road",
        costing_options=CUSTOM_OPTIONS,
    )
    await client.route(request)

    sent = json.loads(route_mock.calls[0].request.content)
    # The custom bundle wins over "road" entirely - not merged with it.
    assert sent["costing_options"]["bicycle"] == CUSTOM_OPTIONS.model_dump()
    assert sent["costing_options"]["bicycle"] != PRESETS["road"]


@respx.mock
async def test_route_forwards_exclude_locations_when_set() -> None:
    route_mock = respx.post(f"{BASE}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    request = RouteRequest(
        waypoints=[Waypoint(lat=53.7996, lon=-1.5491), Waypoint(lat=53.7950, lon=-1.5600)],
        preset="road",
        exclude_locations=[Waypoint(lat=53.7970, lon=-1.5550)],
    )
    await client.route(request)

    sent = json.loads(route_mock.calls[0].request.content)
    assert sent["exclude_locations"] == [{"lat": 53.7970, "lon": -1.5550}]


@respx.mock
async def test_route_omits_exclude_locations_key_when_unset() -> None:
    route_mock = respx.post(f"{BASE}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    await client.route(make_request())

    sent = json.loads(route_mock.calls[0].request.content)
    assert "exclude_locations" not in sent


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


def make_avoid_request() -> RouteRequest:
    request = make_request()
    request.exclude_locations = [Waypoint(lat=53.7996, lon=-1.5491)]
    return request


# The coverage hint is the wrong advice when the rider set an avoid: both
# route() and route_alternates() must say so, since the alternates panel
# accepts avoids too and a rider hitting this there would otherwise be sent
# to investigate their map extract instead of removing the avoid.
@respx.mock
async def test_route_with_avoids_reports_the_avoid_not_the_map_extract() -> None:
    respx.post(f"{BASE}/route").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.route(make_avoid_request())
    assert exc.value.status_code == 422
    assert "avoided roads" in exc.value.detail
    assert "map extract" not in exc.value.detail


@respx.mock
async def test_route_alternates_with_avoids_reports_the_avoid_too() -> None:
    respx.post(f"{BASE}/route").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.route_alternates(make_avoid_request(), 2)
    assert exc.value.status_code == 422
    assert "avoided roads" in exc.value.detail
    assert "map extract" not in exc.value.detail


# Without avoids the hint is the useful one, on both paths.
@respx.mock
async def test_route_alternates_without_avoids_keeps_the_coverage_hint() -> None:
    respx.post(f"{BASE}/route").respond(
        status_code=400, json={"error": "No path could be found for input"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.route_alternates(make_request(), 2)
    assert exc.value.status_code == 422
    assert "map extract" in exc.value.detail


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
    result = await client.trace_attributes([SHAPE])

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
    result = await client.trace_attributes([SHAPE])

    assert result is not None
    expected = (boundary[0]["length"] + boundary[1]["length"]) * 1000.0
    assert result.total_m == pytest.approx(expected)


@respx.mock
async def test_trace_attributes_edge_walk_mismatch_returns_none() -> None:
    """A real edge_walk 400: an unmatched track fails this by design and must
    degrade silently rather than block saving the route."""
    respx.post(f"{BASE}/trace_attributes").respond(status_code=400, json=TRACE_ATTRS_FAIL)

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([SHAPE])

    assert result is None


@respx.mock
async def test_trace_attributes_unreachable_engine_returns_none() -> None:
    """The opposite of trace_route's own unreachable-engine test
    (test_an_unreachable_engine_still_fails_the_whole_trace): surface is
    decorative and never touches FIT or export, so there is nothing here
    worth retrying for."""
    respx.post(f"{BASE}/trace_attributes").mock(side_effect=httpx.ConnectError("refused"))

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([SHAPE])

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
    result = await client.trace_attributes([long_shape])

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
    result = await client.trace_attributes([long_shape])

    assert mock.call_count == 2
    chunks_sent = [json.loads(call.request.content)["shape"] for call in mock.calls]
    assert [len(c) for c in chunks_sent] == [1000, 1000]
    assert result is not None
    assert result.total_m == pytest.approx(100.0 * 2)


@respx.mock
async def test_trace_attributes_multi_leg_chunks_never_mix_legs_points() -> None:
    """Reproduces the via-waypoint finding: each leg is chunked on its own,
    so no request's shape ever straddles a leg boundary - the discontinuity
    that made edge_walk fail on the concatenated shape."""
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
    # Sized so each leg alone needs multiple chunks (1200 > TRACE_MAX_POINTS)
    # - the discontinuity at the shared via point sits between chunk 2 and
    # chunk 3 rather than at a leg boundary that happens to also be a chunk
    # boundary, which would hide a bug that only per-leg chunking avoids.
    # A distinct longitude keeps the two legs' coordinates from ever
    # coinciding, so the point-membership check below is meaningful.
    leg1 = [(53.7996 + i * 0.0001, -1.5491) for i in range(1200)]
    leg2 = [(53.7996 + i * 0.0001, -1.6491) for i in range(1200)]

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([leg1, leg2])

    assert mock.call_count == 4
    chunks_sent = [json.loads(call.request.content)["shape"] for call in mock.calls]
    leg1_points = {(p[0], p[1]) for p in leg1}
    leg2_points = {(p[0], p[1]) for p in leg2}
    for chunk in chunks_sent:
        chunk_points = {(p["lat"], p["lon"]) for p in chunk}
        # Every chunk's points come entirely from one leg or the other,
        # never both.
        assert chunk_points <= leg1_points or chunk_points <= leg2_points
    assert result is not None
    assert result.total_m == pytest.approx(100.0 * 4)


@respx.mock
async def test_trace_attributes_two_legs_both_succeeding_sums_both() -> None:
    edge_a = {
        "length": 0.5,
        "surface": "paved",
        "road_class": "residential",
        "use": "road",
        "cycle_lane": "lane",
    }
    edge_b = {
        "length": 0.3,
        "surface": "gravel",
        "road_class": "path",
        "use": "path",
        "cycle_lane": "none",
    }
    respx.post(f"{BASE}/trace_attributes").mock(
        side_effect=[
            httpx.Response(200, json={"units": "kilometers", "edges": [edge_a]}),
            httpx.Response(200, json={"units": "kilometers", "edges": [edge_b]}),
        ]
    )
    leg1 = [(53.7996, -1.5491), (53.8008, -1.5523)]
    leg2 = [(53.8008, -1.5523), (53.7950, -1.5600)]

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([leg1, leg2])

    assert result is not None
    assert result.total_m == pytest.approx(800.0)
    assert result.surface_m == {"paved": 500.0, "gravel": 300.0}


@respx.mock
async def test_trace_attributes_one_failing_leg_fails_the_whole_breakdown() -> None:
    """Failure semantics are unchanged by the multi-leg signature: ANY chunk
    failing degrades the whole breakdown to None, not just that leg's share
    of it - surface is all-or-nothing, not partial."""
    respx.post(f"{BASE}/trace_attributes").mock(
        side_effect=[
            httpx.Response(200, json={"units": "kilometers", "edges": []}),
            httpx.Response(400, json=TRACE_ATTRS_FAIL),
        ]
    )
    leg1 = [(53.7996, -1.5491), (53.8008, -1.5523)]
    leg2 = [(53.8008, -1.5523), (53.7950, -1.5600)]

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([leg1, leg2])

    assert result is None


async def test_trace_attributes_drops_legs_with_fewer_than_two_points() -> None:
    """A degenerate one-point leg cannot be traced; it is dropped rather than
    sent to Valhalla (which would reject it) or nulling the whole result."""
    client = ValhallaClient(base_url=BASE)
    with respx.mock:
        mock = respx.post(f"{BASE}/trace_attributes").respond(
            json={
                "units": "kilometers",
                "edges": [
                    {
                        "length": 0.1,
                        "surface": "paved",
                        "road_class": "residential",
                        "use": "road",
                        "cycle_lane": "none",
                    }
                ],
            }
        )
        leg1 = [(53.7996, -1.5491)]
        leg2 = [(53.8008, -1.5523), (53.7950, -1.5600)]
        result = await client.trace_attributes([leg1, leg2])

    assert mock.call_count == 1
    assert result is not None
    assert result.total_m == pytest.approx(100.0)


async def test_trace_attributes_all_legs_too_short_returns_none() -> None:
    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([[(53.7996, -1.5491)]])
    assert result is None


@respx.mock
async def test_trace_attributes_always_traces_with_the_neutral_road_bundle() -> None:
    """Costing is deliberately NOT threaded into tracing: edge_walk follows
    the shape's own edges, so costing cannot change which edges come back -
    but an extreme custom bundle (avoid_bad_surfaces=1.0) could make
    Valhalla reject a route's own real unpaved edges and null the whole
    breakdown. Proven live in review pass 2."""
    mock = respx.post(f"{BASE}/trace_attributes").respond(json=TRACE_ATTRS_REAL)

    client = ValhallaClient(base_url=BASE)
    result = await client.trace_attributes([SHAPE])

    assert result is not None
    sent = json.loads(mock.calls[0].request.content)
    assert sent["costing_options"]["bicycle"] == PRESETS["road"]


@respx.mock
async def test_match_ways_sums_length_per_way_id_from_a_real_response() -> None:
    respx.post(f"{BASE}/trace_attributes").respond(json=TRACE_ATTRS_MAP_SNAP_REAL)

    client = ValhallaClient(base_url=BASE)
    result = await client.match_ways(SHAPE)

    assert result is not None
    edges = TRACE_ATTRS_MAP_SNAP_REAL["edges"]
    expected: dict[int, float] = {}
    for edge in edges:
        expected[edge["way_id"]] = expected.get(edge["way_id"], 0.0) + edge["length"] * 1000.0
    assert result == pytest.approx(expected)
    # The real response covers 5 distinct ways and sums to the route's own
    # 1.335 km summary distance, to the metre.
    assert len(result) == 5
    assert sum(result.values()) == pytest.approx(1334.0, abs=1.0)


@respx.mock
async def test_match_ways_sends_map_snap_and_the_gravel_bundle_with_way_id_filters() -> None:
    mock = respx.post(f"{BASE}/trace_attributes").respond(json=TRACE_ATTRS_MAP_SNAP_REAL)

    client = ValhallaClient(base_url=BASE)
    await client.match_ways(SHAPE)

    sent = json.loads(mock.calls[0].request.content)
    assert sent["shape_match"] == "map_snap"
    assert sent["costing"] == "bicycle"
    # The most surface-tolerant named bundle, not "road" - a rider's own
    # recorded ride legitimately follows towpaths and bridleways more often
    # than a planned route would, and unlike edge_walk, map_snap actually
    # uses costing to decide what a trace can snap to.
    assert sent["costing_options"]["bicycle"] == PRESETS["gravel"]
    assert sent["filters"]["attributes"] == ["edge.way_id", "edge.length"]


async def test_match_ways_rejects_a_too_short_shape() -> None:
    client = ValhallaClient(base_url=BASE)
    result = await client.match_ways([(53.7996, -1.5491)])
    assert result is None


@respx.mock
async def test_match_ways_unmatchable_track_returns_none() -> None:
    """Unlike trace_route, a track that cannot be placed on the network
    degrades silently - coverage is an enhancement, and it must never block
    an activity import."""
    respx.post(f"{BASE}/trace_attributes").respond(
        status_code=400, json={"error": "No suitable edges near location"}
    )
    client = ValhallaClient(base_url=BASE)
    result = await client.match_ways(SHAPE)
    assert result is None


@respx.mock
async def test_match_ways_unreachable_engine_returns_none() -> None:
    """The same degrade-silently policy as trace_attributes' own unreachable-
    engine test, and the opposite of trace_route's: nothing here is worth
    retrying on the rider's behalf."""
    respx.post(f"{BASE}/trace_attributes").mock(side_effect=httpx.ConnectError("refused"))
    client = ValhallaClient(base_url=BASE)
    result = await client.match_ways(SHAPE)
    assert result is None


@respx.mock
async def test_match_ways_chunks_long_traces_without_overlap() -> None:
    edge = {"way_id": 42, "length": 0.1}
    mock = respx.post(f"{BASE}/trace_attributes").respond(
        json={"units": "kilometers", "edges": [edge]}
    )
    # Points 100m apart survive thinning, so this needs several requests.
    long_track = [(53.7996 + i * 0.001, -1.5491) for i in range(2500)]

    client = ValhallaClient(base_url=BASE)
    result = await client.match_ways(long_track)

    assert mock.call_count == 3
    chunks_sent = [json.loads(call.request.content)["shape"] for call in mock.calls]
    # No shared boundary point between consecutive chunks - only aggregate
    # metres per way are kept, the same reasoning trace_attributes' own
    # chunking test gives.
    assert chunks_sent[0][-1] != chunks_sent[1][0]
    assert result is not None
    assert result == {42: pytest.approx(300.0)}


ORIGIN = Waypoint(lat=51.7926, lon=-0.6606)


@respx.mock
async def test_isochrone_sends_time_contours_and_passes_features_through() -> None:
    mock = respx.post(f"{BASE}/isochrone").respond(json=ISOCHRONE_REAL)

    client = ValhallaClient(base_url=BASE)
    contours = [
        IsochroneContour(minutes=30, color="268bd2"),
        IsochroneContour(minutes=60, color="b58900"),
    ]
    result = await client.isochrone(
        ORIGIN, contours, PRESETS["road"], polygons=True, denoise=0.25, generalize=None
    )

    # Valhalla's own GeoJSON, untouched.
    assert result["type"] == "FeatureCollection"
    assert result["features"] == ISOCHRONE_REAL["features"]

    sent = json.loads(mock.calls[0].request.content)
    assert sent["locations"] == [{"lat": ORIGIN.lat, "lon": ORIGIN.lon}]
    assert sent["costing"] == "bicycle"
    assert sent["costing_options"]["bicycle"] == PRESETS["road"]
    assert sent["contours"] == [
        {"time": 30, "color": "268bd2"},
        {"time": 60, "color": "b58900"},
    ]
    assert sent["polygons"] is True
    assert sent["denoise"] == pytest.approx(0.25)
    assert "generalize" not in sent


@respx.mock
async def test_isochrone_sends_distance_contours_without_color() -> None:
    mock = respx.post(f"{BASE}/isochrone").respond(json=ISOCHRONE_REAL)

    client = ValhallaClient(base_url=BASE)
    contours = [IsochroneContour(km=15)]
    await client.isochrone(
        ORIGIN, contours, PRESETS["gravel"], polygons=False, denoise=1.0, generalize=50.0
    )

    sent = json.loads(mock.calls[0].request.content)
    assert sent["contours"] == [{"distance": 15}]
    assert sent["polygons"] is False
    assert sent["generalize"] == pytest.approx(50.0)


@respx.mock
async def test_isochrone_valhalla_error_maps_to_422() -> None:
    respx.post(f"{BASE}/isochrone").respond(
        status_code=400, json={"error": "No path could be found for input"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.isochrone(
            ORIGIN,
            [IsochroneContour(minutes=60)],
            PRESETS["road"],
            polygons=True,
            denoise=0.25,
            generalize=None,
        )
    assert exc.value.status_code == 422


def make_alternates_request() -> RouteRequest:
    return RouteRequest(
        waypoints=[Waypoint(lat=51.7926, lon=-0.6606), Waypoint(lat=51.7645, lon=-0.7442)],
        preset="road",
    )


@respx.mock
async def test_route_still_parses_identically_after_the_parse_trip_refactor() -> None:
    """route() delegates its RouteResponse construction to _parse_trip now,
    but its own behaviour must be byte-identical - this pins the same
    assertions test_route_success already makes."""
    route_mock = respx.post(f"{BASE}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    result = await client.route(make_request())

    assert result.distance_m == pytest.approx(1234.0)
    assert result.duration_s == pytest.approx(296.0)
    assert len(result.legs) == 1
    assert len(result.legs[0].maneuvers) == 2
    assert [p.elev_m for p in result.elevation] == [55.0, 60.5, 48.0]
    assert result.ascent_m == pytest.approx(5.5)
    assert result.descent_m == pytest.approx(12.5)

    sent = json.loads(route_mock.calls[0].request.content)
    assert "alternates" not in sent


@respx.mock
async def test_route_alternates_parses_primary_and_the_one_alternate_returned() -> None:
    route_mock = respx.post(f"{BASE}/route").respond(json=ROUTE_ALTERNATES_REAL)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    primary, alternates = await client.route_alternates(make_alternates_request(), count=2)

    assert primary.distance_m == pytest.approx(8237.0)
    assert len(primary.legs) == 1
    assert primary.legs[0].maneuvers

    # 2 were requested, Valhalla returned only 1 - reflected as-is, not
    # padded or treated as an error.
    assert len(alternates) == 1
    assert alternates[0].distance_m == pytest.approx(11785.0)
    assert alternates[0].legs[0].maneuvers

    sent = json.loads(route_mock.calls[0].request.content)
    assert sent["alternates"] == 2
    assert sent["costing"] == "bicycle"
    assert sent["costing_options"]["bicycle"] == PRESETS["road"]


@respx.mock
async def test_route_alternates_none_returned_is_an_empty_list() -> None:
    no_alternates = {"trip": ROUTE_ALTERNATES_REAL["trip"]}
    respx.post(f"{BASE}/route").respond(json=no_alternates)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    primary, alternates = await client.route_alternates(make_alternates_request(), count=3)

    assert primary.distance_m == pytest.approx(8237.0)
    assert alternates == []


@respx.mock
async def test_route_alternates_valhalla_error_maps_to_422() -> None:
    respx.post(f"{BASE}/route").respond(
        status_code=400, json={"error": "No path could be found for input"}
    )
    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await client.route_alternates(make_alternates_request(), count=2)
    assert exc.value.status_code == 422
