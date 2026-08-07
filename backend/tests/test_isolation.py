"""Another user's routes must be invisible to every new endpoint."""

import pytest
from httpx import AsyncClient

from app.schemas import RouteResponse
from tests.conftest import register
from tests.test_auth_routes import save_body


async def test_new_endpoints_do_not_leak_another_users_routes(
    client: AsyncClient, snapshot: RouteResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    mine = (await client.post("/api/routes", json=save_body(snapshot))).json()
    await client.patch(f"/api/routes/{mine['id']}", json={"tags": ["secret-tag"]})
    await client.post("/api/auth/logout")

    # Second user, fresh account.
    await register(client, "stranger@example.com")

    assert (await client.get(f"/api/routes/{mine['id']}")).status_code == 404
    assert (await client.post(f"/api/routes/{mine['id']}/duplicate")).status_code == 404
    assert (await client.post(f"/api/routes/{mine['id']}/reverse")).status_code == 404
    assert (await client.get(f"/api/routes/{mine['id']}/export.gpx")).status_code == 404
    assert (await client.get(f"/api/routes/{mine['id']}/export.fit")).status_code == 404
    assert (
        await client.patch(f"/api/routes/{mine['id']}", json={"name": "hijacked"})
    ).status_code == 404
    assert (await client.delete(f"/api/routes/{mine['id']}")).status_code == 404
    # Listing, searching and the tag vocabulary are all scoped to the owner.
    assert (await client.get("/api/routes")).json() == []
    assert (await client.get("/api/routes", params={"q": "Canal"})).json() == []
    assert (await client.get("/api/routes", params={"tag": "secret-tag"})).json() == []
    assert (await client.get("/api/routes/tags")).json() == []
