"""Wahoo service and endpoint tests with a mocked Wahoo Cloud API."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import AsyncClient

from app.config import settings
from app.models import Route, WahooAccount
from app.schemas import RouteResponse
from app.services import wahoo
from app.services.wahoo import WahooError
from tests.conftest import register

WAHOO = "https://api.wahooligan.com"


@pytest.fixture
def wahoo_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "wahoo_client_id", "wahoo-client")
    monkeypatch.setattr(settings, "wahoo_client_secret", "wahoo-secret")
    yield


def make_route(snapshot: RouteResponse, wahoo_route_id: str | None = None) -> Route:
    return Route(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Canal loop",
        preset="gravel",
        waypoints=[],
        legs=[leg.model_dump() for leg in snapshot.legs],
        elevation=[p.model_dump() for p in snapshot.elevation],
        distance_m=snapshot.distance_m,
        duration_s=snapshot.duration_s,
        ascent_m=snapshot.ascent_m,
        descent_m=snapshot.descent_m,
        wahoo_route_id=wahoo_route_id,
        updated_at=datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    )


def make_account() -> WahooAccount:
    return WahooAccount(
        user_id=uuid.uuid4(),
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        athlete={},
    )


@respx.mock
async def test_push_creates_route(wahoo_settings: None, snapshot: RouteResponse) -> None:
    create = respx.post(f"{WAHOO}/v1/routes").respond(json={"id": 4242})
    route = make_route(snapshot)

    wahoo_id = await wahoo.push_route(route, make_account())

    assert wahoo_id == "4242"
    raw = create.calls[0].request.content

    def field(name: str) -> bytes:
        return raw.split(f'name="{name}"\r\n\r\n'.encode(), 1)[1].split(b"\r\n", 1)[0]

    assert field("route[external_id]") == str(route.id).encode()
    assert field("route[name]") == b"Canal loop"
    assert field("route[workout_type_family_id]") == b"0"
    assert float(field("route[start_lat]")) == pytest.approx(53.7996)
    assert float(field("route[start_lng]")) == pytest.approx(-1.5491)
    # FIT file rides along as a multipart attachment named route.fit.
    assert b'name="route[file]"; filename="route.fit"' in raw
    fit_bytes = raw.split(b'filename="route.fit"', 1)[1]
    assert b".FIT" in fit_bytes[:200]


@respx.mock
async def test_push_updates_existing_route(wahoo_settings: None, snapshot: RouteResponse) -> None:
    update = respx.put(f"{WAHOO}/v1/routes/4242").respond(json={"id": 4242})
    route = make_route(snapshot, wahoo_route_id="4242")

    assert await wahoo.push_route(route, make_account()) == "4242"
    assert update.called


@respx.mock
async def test_push_retries_on_429(wahoo_settings: None, snapshot: RouteResponse) -> None:
    calls = respx.post(f"{WAHOO}/v1/routes")
    calls.side_effect = [
        respx.MockResponse(429, headers={"Retry-After": "0"}),
        respx.MockResponse(201, json={"id": 7}),
    ]

    assert await wahoo.push_route(make_route(snapshot), make_account()) == "7"
    assert calls.call_count == 2


@respx.mock
async def test_push_refreshes_on_401(wahoo_settings: None, snapshot: RouteResponse) -> None:
    calls = respx.post(f"{WAHOO}/v1/routes")
    calls.side_effect = [
        respx.MockResponse(401),
        respx.MockResponse(201, json={"id": 9}),
    ]
    respx.post(f"{WAHOO}/oauth/token").respond(
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 7200}
    )
    account = make_account()

    assert await wahoo.push_route(make_route(snapshot), account) == "9"
    assert account.access_token == "access-2"
    assert account.refresh_token == "refresh-2"


@respx.mock
async def test_push_terminal_error(wahoo_settings: None, snapshot: RouteResponse) -> None:
    respx.post(f"{WAHOO}/v1/routes").respond(422, json={"error": "bad file"})
    with pytest.raises(WahooError, match="HTTP 422"):
        await wahoo.push_route(make_route(snapshot), make_account())


@respx.mock
async def test_exchange_code_fetches_athlete(wahoo_settings: None) -> None:
    respx.post(f"{WAHOO}/oauth/token").respond(
        json={"access_token": "a", "refresh_token": "r", "expires_in": 7200}
    )
    respx.get(f"{WAHOO}/v1/user").respond(json={"first": "James", "last": "B"})

    tokens = await wahoo.exchange_code("code-1")
    assert tokens["athlete"] == {"first": "James", "last": "B"}


async def test_endpoints_404_when_unconfigured(client: AsyncClient) -> None:
    await register(client)
    assert (await client.get("/api/wahoo/connect")).status_code == 404
    status = (await client.get("/api/wahoo/status")).json()
    assert status == {"configured": False, "connected": False, "athlete": None}


async def test_push_requires_connected_account(
    client: AsyncClient, snapshot: RouteResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "wahoo_client_id", "x")
    monkeypatch.setattr(settings, "wahoo_client_secret", "y")
    await register(client)
    created = await client.post(
        "/api/routes",
        json={
            "name": "R",
            "waypoints": [{"lat": 53.0, "lon": -1.0}, {"lat": 53.1, "lon": -1.1}],
            "preset": "road",
            "snapshot": snapshot.model_dump(),
        },
    )
    route_id = created.json()["id"]
    response = await client.post(f"/api/wahoo/push/{route_id}")
    assert response.status_code == 400


@respx.mock
async def test_connect_and_push_flow(
    client: AsyncClient, snapshot: RouteResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "wahoo_client_id", "x")
    monkeypatch.setattr(settings, "wahoo_client_secret", "y")
    respx.route(host="test").pass_through()
    respx.post(f"{WAHOO}/oauth/token").respond(
        json={"access_token": "a", "refresh_token": "r", "expires_in": 7200}
    )
    respx.get(f"{WAHOO}/v1/user").respond(json={"first": "James", "last": "B"})

    await register(client)
    connect = await client.get("/api/wahoo/connect")
    assert connect.status_code == 302
    state = connect.headers["location"].split("state=")[1].split("&")[0]

    callback = await client.get(f"/api/wahoo/callback?code=c&state={state}")
    assert callback.status_code == 302

    status = (await client.get("/api/wahoo/status")).json()
    assert status["connected"] is True
    assert status["athlete"] == {"name": "James B"}

    created = await client.post(
        "/api/routes",
        json={
            "name": "R",
            "waypoints": [{"lat": 53.0, "lon": -1.0}, {"lat": 53.1, "lon": -1.1}],
            "preset": "road",
            "snapshot": snapshot.model_dump(),
        },
    )
    route_id = created.json()["id"]
    pushed = await client.post(f"/api/wahoo/push/{route_id}")
    assert pushed.status_code == 200
    assert pushed.json()["wahoo"]["status"] == "queued"

    again = await client.post(f"/api/wahoo/push/{route_id}")
    assert again.status_code == 409

    # Drive the real worker against the DB row: the queued -> pushing commit
    # expires server-onupdate columns, which push_route must survive.
    respx.post(f"{WAHOO}/v1/routes").respond(json={"id": 4242})
    from app.services.wahoo_queue import WahooQueue

    await WahooQueue()._push_one(uuid.UUID(route_id))

    detail = (await client.get(f"/api/routes/{route_id}")).json()
    assert detail["wahoo"]["status"] == "synced"
    assert detail["wahoo"]["route_id"] == "4242"
