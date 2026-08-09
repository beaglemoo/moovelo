"""Loop generator: services/loop.py's binary search, scoring and dedup
maths, and the POST /api/route/loop endpoint that wraps them.

trace_attributes is monkeypatched at the ValhallaClient method level rather
than mocked with respx - the surface breakdown only matters to the scoring
tests below, and patching the method is simpler than a second respx route
for every test that does not care about it.
"""

import json

import httpx
import pytest
import respx
from fastapi import HTTPException
from httpx import AsyncClient

from app.main import app
from app.schemas import BicycleCostingOptions, LoopCandidate, RouteResponse, Waypoint
from app.services.geo import haversine
from app.services.loop import (
    LOOP_CANDIDATES,
    LOOP_DEDUP_KM,
    LOOP_TOLERANCE,
    _dedup_by_via,
    generate_loop_candidates,
    score_loop,
)
from app.services.valhalla import ValhallaClient
from tests.conftest import make_snapshot, register

BASE = "http://valhalla.test"
ORIGIN = Waypoint(lat=51.8, lon=-0.65)
HEIGHT_RESPONSE = {"range_height": []}

# Out-and-back distance as a plain multiple of the via's straight-line
# radius from the origin - independent of bearing, so every one of the
# 8 bearings the generator tries converges identically and deterministically.
# Chosen so the achievable range [0.4*r0, 2*r0] (with r0 = target /
# OUT_AND_BACK_RATIO) brackets the target: the converging radius sits at
# 0.625*r0, so the search must shrink from its over-target first guess.
DISTANCE_SCALE = 4.0


def _route_response(radius_m: float) -> dict:
    length_km = DISTANCE_SCALE * radius_m / 1000.0
    return {
        "trip": {
            "summary": {"length": length_km, "time": length_km * 150.0},
            "legs": [
                {
                    "shape": "",
                    "maneuvers": [
                        {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 0}
                    ],
                }
            ],
        }
    }


def _well_behaved_route_side_effect(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    via = payload["locations"][1]
    radius_m = haversine((ORIGIN.lat, ORIGIN.lon), (via["lat"], via["lon"]))
    return httpx.Response(200, json=_route_response(radius_m))


def _failing_bearing_zero_side_effect(request: httpx.Request) -> httpx.Response:
    """Bearing 0 (due north) leaves the via's longitude exactly unchanged -
    see test_geo.py's test_destination_point_due_north_keeps_longitude - so
    that is what identifies "the coastal bearing" here without needing to
    track which of the 8 requests belongs to which bearing."""
    payload = json.loads(request.content)
    via = payload["locations"][1]
    if via["lon"] == ORIGIN.lon:
        return httpx.Response(400, json={"error": "No path could be found for input"})
    return _well_behaved_route_side_effect(request)


@pytest.fixture(autouse=True)
def no_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(self: ValhallaClient, legs: list) -> None:
        return None

    monkeypatch.setattr(ValhallaClient, "trace_attributes", _none)


@respx.mock
async def test_binary_search_converges_within_tolerance() -> None:
    respx.post(f"{BASE}/route").mock(side_effect=_well_behaved_route_side_effect)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    candidates = await generate_loop_candidates(ORIGIN, 20.0, "road", None, client)

    assert 1 <= len(candidates) <= LOOP_CANDIDATES
    for candidate in candidates:
        assert candidate.distance_error_km / 20.0 <= LOOP_TOLERANCE + 1e-9


# A loop is adopted as the working route, so one generated through a road the
# rider had already excluded would land as a route that ignores an avoid the
# UI still shows as active.
@respx.mock
async def test_avoids_reach_every_route_call_the_search_makes() -> None:
    routed = respx.post(f"{BASE}/route").mock(side_effect=_well_behaved_route_side_effect)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)
    avoid = Waypoint(lat=53.81, lon=-1.56)

    client = ValhallaClient(base_url=BASE)
    await generate_loop_candidates(ORIGIN, 20.0, "road", None, client, [avoid])

    assert routed.call_count > 0
    for call in routed.calls:
        payload = json.loads(call.request.content)
        assert payload["exclude_locations"] == [{"lat": avoid.lat, "lon": avoid.lon}]


@respx.mock
async def test_a_bearing_that_always_fails_is_skipped_and_others_survive() -> None:
    respx.post(f"{BASE}/route").mock(side_effect=_failing_bearing_zero_side_effect)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)

    client = ValhallaClient(base_url=BASE)
    candidates = await generate_loop_candidates(ORIGIN, 20.0, "road", None, client)

    assert 1 <= len(candidates) <= LOOP_CANDIDATES
    # None of the survivors is the always-failing due-north bearing.
    for candidate in candidates:
        via = candidate.waypoints[1]
        assert via.lon != ORIGIN.lon


@respx.mock
async def test_all_bearings_failing_raises_422() -> None:
    respx.post(f"{BASE}/route").respond(
        status_code=400, json={"error": "No path could be found for input"}
    )

    client = ValhallaClient(base_url=BASE)
    with pytest.raises(HTTPException) as exc:
        await generate_loop_candidates(ORIGIN, 20.0, "road", None, client)
    assert exc.value.status_code == 422
    assert "No rideable loop" in exc.value.detail


def test_score_prefers_the_near_exact_paved_candidate_under_road() -> None:
    """Hand-computed: exact distance but 60% unpaved loses to a 2.5% miss at
    5% unpaved, under the road preset (which penalises unpaved surfaces)."""
    unpaved_heavy = score_loop(
        actual_km=20.0,
        target_km=20.0,
        ascent_m=0.0,
        unpaved_fraction=0.6,
        preset="road",
        costing_options=None,
    )
    near_exact_paved = score_loop(
        actual_km=20.5,
        target_km=20.0,
        ascent_m=0.0,
        unpaved_fraction=0.05,
        preset="road",
        costing_options=None,
    )
    assert unpaved_heavy == pytest.approx(0.5 * 0.6)  # 0.3
    assert near_exact_paved == pytest.approx(0.025 + 0.5 * 0.05)  # 0.05
    assert near_exact_paved < unpaved_heavy


def test_score_prefers_the_unpaved_candidate_under_gravel() -> None:
    """The same two candidates as above, but under gravel - which rewards
    unpaved distance instead of penalising it - flip which one wins."""
    unpaved_heavy = score_loop(
        actual_km=20.0,
        target_km=20.0,
        ascent_m=0.0,
        unpaved_fraction=0.6,
        preset="gravel",
        costing_options=None,
    )
    near_exact_paved = score_loop(
        actual_km=20.5,
        target_km=20.0,
        ascent_m=0.0,
        unpaved_fraction=0.05,
        preset="gravel",
        costing_options=None,
    )
    assert unpaved_heavy == pytest.approx(-0.5 * 0.6)  # -0.3
    assert near_exact_paved == pytest.approx(0.025 - 0.5 * 0.05)  # 0.0
    assert unpaved_heavy < near_exact_paved


def test_score_treats_custom_costing_like_road_by_default() -> None:
    custom = BicycleCostingOptions(
        bicycle_type="Road",
        cycling_speed=25,
        use_roads=0.6,
        use_hills=0.5,
        avoid_bad_surfaces=0.9,
    )
    score = score_loop(20.0, 20.0, 0.0, 0.5, preset="road", costing_options=custom)
    assert score == pytest.approx(0.5 * 0.5)  # unpaved penalised, same sign as "road"


def test_score_treats_low_avoid_bad_surfaces_custom_costing_like_gravel() -> None:
    custom = BicycleCostingOptions(
        bicycle_type="Cross",
        cycling_speed=20,
        use_roads=0.3,
        use_hills=0.5,
        avoid_bad_surfaces=0.2,
    )
    score = score_loop(20.0, 20.0, 0.0, 0.5, preset="road", costing_options=custom)
    assert score == pytest.approx(-0.5 * 0.5)  # unpaved rewarded, same sign as "gravel"


def test_score_ascent_term_calibration() -> None:
    """The scoring docstring's own claim: even a mountainous 30 m/km ride
    adds only ~0.15 on its own (no distance error, no surface term) - less
    than a 20% distance miss, so ascent can break ties but never overrule
    the distance the rider actually asked for."""
    score = score_loop(
        20.0, 20.0, ascent_m=600.0, unpaved_fraction=0.0, preset="road", costing_options=None
    )
    assert score == pytest.approx(0.15)
    assert score < 0.20


def _candidate(via: Waypoint, score: float, snapshot: RouteResponse) -> LoopCandidate:
    return LoopCandidate(
        waypoints=[ORIGIN, via, ORIGIN],
        snapshot=snapshot,
        distance_error_km=0.0,
        score=score,
    )


def test_dedup_keeps_only_the_better_of_two_close_candidates() -> None:
    snapshot = make_snapshot()
    close_via_a = Waypoint(lat=ORIGIN.lat + 0.01, lon=ORIGIN.lon)  # ~1.1 km north
    close_via_b = Waypoint(lat=ORIGIN.lat + 0.012, lon=ORIGIN.lon)  # ~1.3 km north
    assert haversine((close_via_a.lat, close_via_a.lon), (close_via_b.lat, close_via_b.lon)) < (
        LOOP_DEDUP_KM * 1000.0
    )
    far_via = Waypoint(lat=ORIGIN.lat, lon=ORIGIN.lon + 0.2)  # well over 3 km away

    # Already sorted by score, worst first, matching what the caller passes in.
    candidates = [
        _candidate(far_via, score=0.5, snapshot=snapshot),
        _candidate(close_via_b, score=0.3, snapshot=snapshot),  # the better of the pair
        _candidate(close_via_a, score=0.2, snapshot=snapshot),  # the best overall
    ]
    candidates.sort(key=lambda c: c.score)

    kept = _dedup_by_via(candidates, limit=3, min_sep_m=LOOP_DEDUP_KM * 1000.0)

    assert len(kept) == 2
    assert kept[0].score == pytest.approx(0.2)
    assert kept[0].waypoints[1] is close_via_a
    assert kept[1].waypoints[1] is far_via


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    app.state.valhalla = ValhallaClient(base_url=BASE)


async def test_route_loop_needs_a_session(client: AsyncClient) -> None:
    response = await client.post(
        "/api/route/loop", json={"origin": {"lat": ORIGIN.lat, "lon": ORIGIN.lon}, "target_km": 20}
    )
    assert response.status_code == 401


@respx.mock
async def test_route_loop_happy_path_returns_full_snapshots(client: AsyncClient) -> None:
    respx.post(f"{BASE}/route").mock(side_effect=_well_behaved_route_side_effect)
    respx.post(f"{BASE}/height").respond(json=HEIGHT_RESPONSE)
    await register(client)

    response = await client.post(
        "/api/route/loop",
        json={"origin": {"lat": ORIGIN.lat, "lon": ORIGIN.lon}, "target_km": 20, "preset": "road"},
    )

    assert response.status_code == 200, response.text
    candidates = response.json()["candidates"]
    assert 1 <= len(candidates) <= LOOP_CANDIDATES
    for candidate in candidates:
        assert len(candidate["waypoints"]) == 3
        assert candidate["snapshot"]["distance_m"] > 0
        assert "legs" in candidate["snapshot"]


async def test_route_loop_rejects_zero_target(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/route/loop", json={"origin": {"lat": ORIGIN.lat, "lon": ORIGIN.lon}, "target_km": 0}
    )
    assert response.status_code == 422


async def test_route_loop_rejects_target_over_200km(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/route/loop", json={"origin": {"lat": ORIGIN.lat, "lon": ORIGIN.lon}, "target_km": 201}
    )
    assert response.status_code == 422
