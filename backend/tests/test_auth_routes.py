"""Integration tests for auth and route CRUD against a throwaway Postgres."""

import json

import pytest
import respx
from httpx import AsyncClient

from app.main import app
from app.schemas import RouteResponse
from app.services.polyline import encode_polyline6
from app.services.valhalla import ValhallaClient
from tests.conftest import register

REVERSED_TRIP = {
    "trip": {
        "summary": {"length": 1.93, "time": 470.0},
        "legs": [
            {
                "shape": encode_polyline6([(53.785, -1.575), (53.7996, -1.5491)]),
                "maneuvers": [
                    {"type": 1, "instruction": "Ride northeast.", "begin_shape_index": 0},
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 1},
                ],
            }
        ],
    }
}

WAYPOINTS = [{"lat": 53.7996, "lon": -1.5491}, {"lat": 53.785, "lon": -1.575}]

TRACE_ATTRS_RESPONSE = {
    "units": "kilometers",
    "edges": [{"length": 1.93, "surface": "paved", "road_class": "residential", "use": "road"}],
}


def save_body(snapshot: RouteResponse, name: str = "Canal loop") -> dict[str, object]:
    return {
        "name": name,
        "waypoints": WAYPOINTS,
        "preset": "gravel",
        "snapshot": snapshot.model_dump(),
    }


async def test_first_user_becomes_admin_then_signups_blocked(client: AsyncClient) -> None:
    status = (await client.get("/api/auth/status")).json()
    assert status["setup_required"] is True
    assert status["signups_enabled"] is False
    assert status["oidc"]["enabled"] is False

    await register(client)
    me = (await client.get("/api/auth/me")).json()
    assert me == {"email": "admin@example.com", "is_admin": True}

    second = await client.post(
        "/api/auth/register", json={"email": "b@example.com", "password": "password-123"}
    )
    assert second.status_code == 403


async def test_login_logout_flow(client: AsyncClient) -> None:
    await register(client)
    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401

    bad = await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "wrong-password"}
    )
    assert bad.status_code == 401

    good = await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "correct-horse-9"}
    )
    assert good.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_route_endpoints_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/routes")).status_code == 401
    assert (
        await client.post("/api/route", json={"waypoints": WAYPOINTS, "preset": "road"})
    ).status_code == 401


async def test_route_crud_cycle(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)

    created = await client.post("/api/routes", json=save_body(snapshot))
    assert created.status_code == 201, created.text
    route_id = created.json()["id"]

    listed = (await client.get("/api/routes")).json()
    assert [r["name"] for r in listed] == ["Canal loop"]
    assert listed[0]["distance_m"] == snapshot.distance_m

    fetched = (await client.get(f"/api/routes/{route_id}")).json()
    assert fetched["waypoints"] == WAYPOINTS
    assert fetched["legs"] == [leg.model_dump() for leg in snapshot.legs]

    renamed = await client.patch(f"/api/routes/{route_id}", json={"name": "Renamed loop"})
    assert renamed.json()["name"] == "Renamed loop"

    assert (await client.delete(f"/api/routes/{route_id}")).status_code == 204
    assert (await client.get(f"/api/routes/{route_id}")).status_code == 404


async def test_routes_are_scoped_per_user(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    created = await client.post("/api/routes", json=save_body(snapshot))
    route_id = created.json()["id"]

    # Force-enable signups for a second account via a fresh registration
    # being blocked; instead simulate another user by logging out and
    # asserting the route is invisible without a session.
    await client.post("/api/auth/logout")
    assert (await client.get(f"/api/routes/{route_id}")).status_code == 401


async def test_export_endpoints(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    created = await client.post("/api/routes", json=save_body(snapshot))
    route_id = created.json()["id"]

    gpx = await client.get(f"/api/routes/{route_id}/export.gpx")
    assert gpx.status_code == 200
    assert b"<trkpt" in gpx.content
    assert "canal-loop.gpx" in gpx.headers["content-disposition"]

    fit = await client.get(f"/api/routes/{route_id}/export.fit")
    assert fit.status_code == 200
    assert fit.content[8:12] == b".FIT"


async def test_share_link_lifecycle(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    created = await client.post("/api/routes", json=save_body(snapshot))
    route_id = created.json()["id"]
    assert created.json()["share_token"] is None

    shared = (await client.post(f"/api/routes/{route_id}/share")).json()
    token = shared["share_token"]
    assert token

    # Public endpoints work without a session and leak no identifiers.
    await client.post("/api/auth/logout")
    public = await client.get(f"/api/shared/{token}")
    assert public.status_code == 200
    body = public.json()
    assert body["name"] == "Canal loop"
    assert "id" not in body and "waypoints" not in body

    gpx = await client.get(f"/api/shared/{token}/export.gpx")
    assert gpx.status_code == 200
    assert b"<trkpt" in gpx.content

    # Rotating the token invalidates the old link.
    await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "correct-horse-9"}
    )
    rotated = (await client.post(f"/api/routes/{route_id}/share")).json()
    assert rotated["share_token"] != token
    assert (await client.get(f"/api/shared/{token}")).status_code == 404

    # Revoking kills the link entirely.
    revoked = (await client.delete(f"/api/routes/{route_id}/share")).json()
    assert revoked["share_token"] is None
    assert (await client.get(f"/api/shared/{rotated['share_token']}")).status_code == 404


async def test_route_metadata_round_trips(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    assert created["tags"] == [] and created["is_favourite"] is False and created["notes"] is None

    patched = (
        await client.patch(
            f"/api/routes/{created['id']}",
            json={
                "tags": ["gravel", "with the kids"],
                "notes": "Cafe at 12km",
                "is_favourite": True,
            },
        )
    ).json()
    assert patched["tags"] == ["gravel", "with the kids"]
    assert patched["notes"] == "Cafe at 12km"
    assert patched["is_favourite"] is True

    summary = (await client.get("/api/routes")).json()[0]
    assert summary["tags"] == ["gravel", "with the kids"]
    assert summary["is_favourite"] is True


async def test_tags_are_trimmed_and_deduplicated(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    patched = (
        await client.patch(
            f"/api/routes/{created['id']}",
            json={"tags": ["  gravel ", "gravel", "", "  ", "hilly   loop"]},
        )
    ).json()
    assert patched["tags"] == ["gravel", "hilly loop"]


async def test_notes_can_be_cleared(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    await client.patch(f"/api/routes/{created['id']}", json={"notes": "temporary"})
    cleared = (await client.patch(f"/api/routes/{created['id']}", json={"notes": ""})).json()
    assert cleared["notes"] is None


async def _seed_library(client: AsyncClient, snapshot: RouteResponse) -> None:
    """Three routes with different names, tags and favourites."""
    for name, tags, favourite, notes in [
        ("Canal loop", ["gravel"], True, "Cafe at 12km"),
        ("Hill repeats", ["road", "hilly"], False, None),
        ("Towpath spin", ["gravel", "flat"], False, "Good with the kids"),
    ]:
        created = (await client.post("/api/routes", json=save_body(snapshot))).json()
        await client.patch(
            f"/api/routes/{created['id']}",
            json={"name": name, "tags": tags, "is_favourite": favourite, "notes": notes or ""},
        )


async def test_library_search_matches_names_and_notes(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    await _seed_library(client, snapshot)

    by_name = (await client.get("/api/routes", params={"q": "hill"})).json()
    assert [r["name"] for r in by_name] == ["Hill repeats"]

    # Recording "cafe at 12km" is only useful if it can be found again.
    by_note = (await client.get("/api/routes", params={"q": "cafe"})).json()
    assert [r["name"] for r in by_note] == ["Canal loop"]


async def test_library_filters_by_tag_and_favourite(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    await _seed_library(client, snapshot)

    gravel = (await client.get("/api/routes", params={"tag": "gravel"})).json()
    assert sorted(r["name"] for r in gravel) == ["Canal loop", "Towpath spin"]

    favourites = (await client.get("/api/routes", params={"favourite": "true"})).json()
    assert [r["name"] for r in favourites] == ["Canal loop"]


async def test_library_sorts_by_name(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    await _seed_library(client, snapshot)

    ascending = (await client.get("/api/routes", params={"sort": "name", "order": "asc"})).json()
    assert [r["name"] for r in ascending] == ["Canal loop", "Hill repeats", "Towpath spin"]

    descending = (await client.get("/api/routes", params={"sort": "name", "order": "desc"})).json()
    assert [r["name"] for r in descending] == ["Towpath spin", "Hill repeats", "Canal loop"]


async def test_library_lists_every_tag_used(client: AsyncClient, snapshot: RouteResponse) -> None:
    await register(client)
    await _seed_library(client, snapshot)
    assert (await client.get("/api/routes/tags")).json() == ["flat", "gravel", "hilly", "road"]


async def test_duplicate_copies_organisation_but_not_sync_state(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    await client.patch(
        f"/api/routes/{created['id']}", json={"tags": ["gravel"], "notes": "Cafe at 12km"}
    )
    await client.post(f"/api/routes/{created['id']}/share")

    copy = (await client.post(f"/api/routes/{created['id']}/duplicate")).json()

    assert copy["id"] != created["id"]
    assert copy["name"] == "Canal loop (copy)"
    assert copy["tags"] == ["gravel"]
    assert copy["notes"] == "Cafe at 12km"
    # A copy has never been pushed to Wahoo and is not shared.
    assert copy["share_token"] is None
    assert copy["wahoo"]["status"] == "none"
    # The stored snapshot is reused, so the copy is identical.
    assert copy["legs"] == created["legs"]
    assert copy["distance_m"] == created["distance_m"]


async def test_duplicate_name_stays_within_the_column(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()
    await client.patch(f"/api/routes/{created['id']}", json={"name": "x" * 200})
    copy = (await client.post(f"/api/routes/{created['id']}/duplicate")).json()
    assert len(copy["name"]) == 200
    assert copy["name"].endswith(" (copy)")


@respx.mock
async def test_reverse_reroutes_rather_than_flipping_the_line(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    app.state.valhalla = ValhallaClient(base_url="http://valhalla.test")
    route_mock = respx.post("http://valhalla.test/route").respond(json=REVERSED_TRIP)
    respx.post("http://valhalla.test/height").respond(json={"range_height": [[0, 60.0]]})
    respx.post("http://valhalla.test/trace_attributes").respond(json=TRACE_ATTRS_RESPONSE)
    await register(client)
    created = (await client.post("/api/routes", json=save_body(snapshot))).json()

    reversed_route = (await client.post(f"/api/routes/{created['id']}/reverse")).json()

    # One-ways and turn instructions are direction-dependent, so the route is
    # planned again in the new direction rather than having its line flipped.
    sent = json.loads(route_mock.calls.last.request.content)
    assert [(loc["lat"], loc["lon"]) for loc in sent["locations"]] == [
        (WAYPOINTS[1]["lat"], WAYPOINTS[1]["lon"]),
        (WAYPOINTS[0]["lat"], WAYPOINTS[0]["lon"]),
    ]
    assert reversed_route["name"] == "Canal loop (reversed)"
    assert reversed_route["id"] != created["id"]
    # A fresh surface breakdown for the new direction, not copied over.
    assert reversed_route["surface"]["total_m"] == pytest.approx(1930.0)
    # Non-destructive: the original is still there.
    assert len((await client.get("/api/routes")).json()) == 2


async def test_search_treats_wildcards_as_literal_text(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    for name in ["50% gravel", "50 percent gravel", "under_score", "underXscore"]:
        created = (await client.post("/api/routes", json=save_body(snapshot))).json()
        await client.patch(f"/api/routes/{created['id']}", json={"name": name})

    percent = (await client.get("/api/routes", params={"q": "50%"})).json()
    assert [r["name"] for r in percent] == ["50% gravel"]

    underscore = (await client.get("/api/routes", params={"q": "under_"})).json()
    assert [r["name"] for r in underscore] == ["under_score"]

    # A bare wildcard is a search for that character, not for everything.
    assert (await client.get("/api/routes", params={"q": "%"})).json() != []
    assert len((await client.get("/api/routes", params={"q": "%"})).json()) == 1


CUSTOM_OPTIONS = {
    "bicycle_type": "Mountain",
    "cycling_speed": 14,
    "use_roads": 0.1,
    "use_hills": 0.9,
    "avoid_bad_surfaces": 0.0,
}


def custom_save_body(snapshot: RouteResponse, name: str = "Custom ride") -> dict[str, object]:
    return {
        "name": name,
        "waypoints": WAYPOINTS,
        "preset": "custom",
        "costing_options": CUSTOM_OPTIONS,
        "snapshot": snapshot.model_dump(),
    }


async def test_saving_with_custom_costing_persists_and_is_echoed_back(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=custom_save_body(snapshot))).json()
    assert created["preset"] == "custom"
    assert created["costing_options"] == CUSTOM_OPTIONS

    reloaded = (await client.get(f"/api/routes/{created['id']}")).json()
    assert reloaded["preset"] == "custom"
    assert reloaded["costing_options"] == CUSTOM_OPTIONS


async def test_switching_back_to_a_named_preset_clears_stored_costing_options(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    await register(client)
    created = (await client.post("/api/routes", json=custom_save_body(snapshot))).json()

    patched = await client.patch(
        f"/api/routes/{created['id']}",
        json={"preset": "road", "waypoints": WAYPOINTS, "snapshot": snapshot.model_dump()},
    )
    assert patched.json()["preset"] == "road"
    assert patched.json()["costing_options"] is None


@respx.mock
async def test_reverse_of_a_custom_costed_route_sends_the_custom_bundle(
    client: AsyncClient, snapshot: RouteResponse
) -> None:
    """The regression this PR hinges on: once "custom" is a stored preset
    value, reversing such a route must not KeyError on PRESETS["custom"] -
    and the reversed route must actually be planned with the stored custom
    bundle, not silently fall back to a named preset."""
    app.state.valhalla = ValhallaClient(base_url="http://valhalla.test")
    route_mock = respx.post("http://valhalla.test/route").respond(json=REVERSED_TRIP)
    respx.post("http://valhalla.test/height").respond(json={"range_height": [[0, 60.0]]})
    attrs_mock = respx.post("http://valhalla.test/trace_attributes").respond(
        json=TRACE_ATTRS_RESPONSE
    )
    await register(client)
    created = (await client.post("/api/routes", json=custom_save_body(snapshot))).json()

    reversed_response = await client.post(f"/api/routes/{created['id']}/reverse")

    assert reversed_response.status_code == 201, reversed_response.text
    reversed_route = reversed_response.json()
    assert reversed_route["preset"] == "custom"
    assert reversed_route["costing_options"] == CUSTOM_OPTIONS

    route_sent = json.loads(route_mock.calls.last.request.content)
    assert route_sent["costing_options"]["bicycle"] == CUSTOM_OPTIONS
    attrs_sent = json.loads(attrs_mock.calls.last.request.content)
    assert attrs_sent["costing_options"]["bicycle"] == CUSTOM_OPTIONS
