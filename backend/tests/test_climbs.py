"""Climb detection: services/climbs.py's pure segmentation algorithm, and
the /api/route round trip that carries its output on every RouteResponse.

Elevation profiles are built from constant-grade ramps glued end to end
(`_profile`), starting at (0 m, 100 m elevation) with no flat lead-in or
lead-out unless a test says otherwise - a ramp that fills the whole profile
keeps the numbers below easy to reason about, since the 150 m smoothing
window then only ever softens the two true ends of the climb rather than a
kink against a flat approach as well.
"""

import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.schemas import ElevationPoint, RouteResponse
from app.services.climbs import CATEGORY_THRESHOLDS, detect_climbs
from app.services.polyline import encode_polyline6
from app.services.valhalla import ValhallaClient
from tests.conftest import register
from tests.test_auth_routes import WAYPOINTS, save_body


def _ramp(
    points: list[tuple[float, float]],
    start_dist: float,
    start_elev: float,
    length_m: float,
    grade_pct: float,
    step_m: float = 25.0,
) -> tuple[float, float]:
    """Append a constant-grade ramp onto `points`, returning where it ends."""
    steps = int(length_m // step_m)
    dist, elev = start_dist, start_elev
    for i in range(1, steps + 1):
        dist = start_dist + i * step_m
        elev = start_elev + (dist - start_dist) * grade_pct / 100.0
        points.append((dist, elev))
    return dist, elev


def _profile(segments: list[tuple[float, float]]) -> list[ElevationPoint]:
    """Glue constant-grade (length_m, grade_pct) ramps end to end."""
    points: list[tuple[float, float]] = [(0.0, 100.0)]
    dist, elev = 0.0, 100.0
    for length_m, grade_pct in segments:
        dist, elev = _ramp(points, dist, elev, length_m, grade_pct)
    return [ElevationPoint(dist_m=d, elev_m=e) for d, e in points]


def test_no_elevation_finds_no_climbs() -> None:
    assert detect_climbs([]) == []


def test_a_single_point_finds_no_climbs() -> None:
    assert detect_climbs([ElevationPoint(dist_m=0, elev_m=100)]) == []


def test_flat_route_has_no_climbs() -> None:
    assert detect_climbs(_profile([(2000, 0.0)])) == []


def test_a_clean_climb_is_detected_end_to_end() -> None:
    """5 km at 6% is the roadmap's own mid-table calibration example:
    length_m * avg_grade_pct**2 / 1000 ~= 180 -> category "2". The 150 m
    smoothing window softens the very start and end of the climb (there is
    no flat lead-in here for it to blend with instead), which is why the
    detected gain and average grade land a little under the nominal 300 m /
    6.0% rather than exactly on them."""
    climbs = detect_climbs(_profile([(5000, 6.0)]))

    assert len(climbs) == 1
    climb = climbs[0]
    assert climb.start_dist_m == pytest.approx(0.0, abs=1.0)
    assert climb.end_dist_m == pytest.approx(5000.0, abs=1.0)
    assert climb.length_m == pytest.approx(5000.0, abs=1.0)
    assert climb.gain_m == pytest.approx(300.0, rel=0.05)
    assert climb.avg_grade_pct == pytest.approx(6.0, rel=0.05)
    assert climb.max_grade_pct >= climb.avg_grade_pct
    # The scoring formula itself, checked against whatever length/grade the
    # segmentation actually produced, rather than pinning both independently.
    assert climb.score == pytest.approx(climb.length_m * climb.avg_grade_pct**2 / 1000)
    assert climb.category == "2"


def test_a_short_ramp_is_dropped_for_length() -> None:
    """400 m even at a punchy 8% never reaches the 500 m minimum length."""
    assert detect_climbs(_profile([(400, 8.0)])) == []


def test_a_short_climb_clearing_the_score_floor_is_kept() -> None:
    """500 m at 8% clears the length/gain minimums and the score floor
    (nominally 500 * 8**2 / 1000 = 32, safely over category 4's 20)."""
    climbs = detect_climbs(_profile([(500, 8.0)]))

    assert len(climbs) == 1
    assert climbs[0].length_m == pytest.approx(500.0, abs=1.0)
    assert climbs[0].gain_m >= 20.0
    assert climbs[0].category == "4"


def test_passing_the_length_and_gain_minimums_is_not_enough_on_its_own() -> None:
    """800 m at 3.75% nominally gains 30 m over 800 m, clearing both raw
    minimums (500 m length, 20 m gain) - but its score never reaches
    category 4's floor of 20, so it is discarded rather than kept. This is
    exactly the case the algorithm spec calls out: those two minimums alone
    do not guarantee a climb scores anything - even a climb sitting right on
    both of them (500 m at exactly 20 m gain) only scores 8."""
    assert detect_climbs(_profile([(800, 3.75)])) == []


def test_a_short_shallow_dip_is_swallowed_into_one_climb() -> None:
    """A 100 m dip losing 5 m - under both the 500 m gap and 15 m loss merge
    tolerances - is a roller, not the end of the climb."""
    climbs = detect_climbs(_profile([(950, 6.0), (100, -5.0), (950, 6.0)]))

    assert len(climbs) == 1
    assert climbs[0].start_dist_m == pytest.approx(0.0, abs=1.0)
    assert climbs[0].end_dist_m == pytest.approx(2000.0, abs=1.0)


def test_a_deep_descent_splits_the_climb_in_two() -> None:
    """A 300 m descent losing 25 m is under the gap-length tolerance (500 m)
    but over the loss tolerance (15 m), so - unlike the shallow dip above -
    it is not swallowed: two separate climbs either side of it."""
    climbs = detect_climbs(_profile([(800, 6.0), (300, -8.3333), (800, 6.0)]))

    assert len(climbs) == 2
    assert climbs[0].end_dist_m < climbs[1].start_dist_m
    for climb in climbs:
        assert climb.length_m == pytest.approx(750.0, abs=1.0)


def test_climbs_are_returned_in_route_order() -> None:
    climbs = detect_climbs(_profile([(800, 6.0), (300, -8.3333), (800, 6.0)]))
    assert [c.start_dist_m for c in climbs] == sorted(c.start_dist_m for c in climbs)


def test_max_grade_exceeds_average_on_a_steepening_ramp() -> None:
    """4% for the first half, 10% for the second: the steepest 100 m window
    sits in the steeper half and comfortably exceeds the whole climb's
    average grade."""
    climbs = detect_climbs(_profile([(1000, 4.0), (1000, 10.0)]))

    assert len(climbs) == 1
    assert climbs[0].max_grade_pct == pytest.approx(10.0, rel=0.05)
    assert climbs[0].max_grade_pct > climbs[0].avg_grade_pct + 1.0


def test_category_thresholds_are_ordered_highest_first() -> None:
    assert [name for _, name in CATEGORY_THRESHOLDS] == ["HC", "1", "2", "3", "4"]
    assert [threshold for threshold, _ in CATEGORY_THRESHOLDS] == sorted(
        (threshold for threshold, _ in CATEGORY_THRESHOLDS), reverse=True
    )


# --- API round trip -----------------------------------------------------

VALHALLA = "http://valhalla.test"

TRIP_RESPONSE = {
    "trip": {
        "summary": {"length": 5.0, "time": 900.0},
        "legs": [
            {
                "shape": encode_polyline6([(53.7996, -1.5491), (53.7950, -1.5600)]),
                "maneuvers": [{"type": 1, "instruction": "Ride west.", "begin_shape_index": 0}],
            }
        ],
    }
}


def _hilly_climb() -> list[ElevationPoint]:
    """The same 5 km @ 6% climb used above, for the API-level tests."""
    return _profile([(5000, 6.0)])


def _hilly_height_response() -> dict[str, object]:
    """Sent back as raw /height range_height pairs."""
    return {"range_height": [[p.dist_m, p.elev_m] for p in _hilly_climb()]}


@pytest.fixture(autouse=True)
def valhalla_client() -> None:
    """The endpoint reads the client off app.state, which only the lifespan
    sets up - and the test transport does not run it."""
    app.state.valhalla = ValhallaClient(base_url=VALHALLA)


@respx.mock
async def test_plan_route_carries_climbs(client: AsyncClient) -> None:
    respx.post(f"{VALHALLA}/route").respond(json=TRIP_RESPONSE)
    respx.post(f"{VALHALLA}/height").respond(json=_hilly_height_response())
    await register(client)

    response = await client.post("/api/route", json={"waypoints": WAYPOINTS, "preset": "road"})

    assert response.status_code == 200, response.text
    climbs = response.json()["climbs"]
    assert len(climbs) == 1
    assert climbs[0]["category"] == "2"


async def test_saving_a_route_with_climbs_round_trips(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    with_climb = snapshot.model_copy(update={"climbs": detect_climbs(_hilly_climb())})
    await register(client)

    created = (await client.post("/api/routes", json=save_body(with_climb))).json()
    assert len(created["climbs"]) == 1
    assert created["climbs"][0]["category"] == "2"

    fetched = (await client.get(f"/api/routes/{created['id']}")).json()
    assert len(fetched["climbs"]) == 1
    assert fetched["climbs"][0]["category"] == "2"


async def test_saving_a_route_without_climbs_reads_back_as_an_empty_list(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    """A pre-Phase-7 snapshot carries no climbs; unlike surface (nullable,
    "match failed" is a real state) the column is NOT NULL DEFAULT '[]', so
    this reads back as an empty list rather than null."""
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    assert created["climbs"] == []

    fetched = (await client.get(f"/api/routes/{created['id']}")).json()
    assert fetched["climbs"] == []


async def test_share_carries_the_climb_list(client: AsyncClient, snapshot: RouteResponse) -> None:
    with_climb = snapshot.model_copy(update={"climbs": detect_climbs(_hilly_climb())})
    await register(client)
    created = (await client.post("/api/routes", json=save_body(with_climb))).json()
    token = (await client.post(f"/api/routes/{created['id']}/share")).json()["share_token"]

    await client.post("/api/auth/logout")
    shared = (await client.get(f"/api/shared/{token}")).json()

    assert len(shared["climbs"]) == 1
    assert shared["climbs"][0]["category"] == "2"
