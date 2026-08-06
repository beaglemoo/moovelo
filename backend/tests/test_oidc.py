"""OIDC login flow tests with a mocked provider."""

from collections.abc import Iterator

import pytest
import respx
from httpx import AsyncClient

from app.config import settings
from app.services import oidc

ISSUER = "https://id.test"


@pytest.fixture
def oidc_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", "bikegps-client")
    monkeypatch.setattr(settings, "oidc_client_secret", "shhh")
    monkeypatch.setattr(settings, "oidc_provider_name", "Pocket ID")
    oidc._discovery_cache.clear()
    yield
    oidc._discovery_cache.clear()


@pytest.fixture
def provider(oidc_settings: None) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="test").pass_through()
        router.get(f"{ISSUER}/.well-known/openid-configuration").respond(
            json={
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/api/oidc/token",
                "userinfo_endpoint": f"{ISSUER}/api/oidc/userinfo",
            }
        )
        router.post(f"{ISSUER}/api/oidc/token").respond(json={"access_token": "at-123"})
        router.get(f"{ISSUER}/api/oidc/userinfo").respond(
            json={"email": "Rider@Example.com", "email_verified": True}
        )
        yield router


async def test_status_reports_oidc(client: AsyncClient, provider: respx.MockRouter) -> None:
    status = (await client.get("/api/auth/status")).json()
    assert status["oidc"] == {"enabled": True, "name": "Pocket ID"}


async def test_login_redirects_with_state(client: AsyncClient, provider: respx.MockRouter) -> None:
    response = await client.get("/api/auth/oidc/login")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize?")
    assert "client_id=bikegps-client" in location
    assert "bikegps_oidc_state" in response.headers.get("set-cookie", "")


async def test_callback_provisions_first_user_as_admin(
    client: AsyncClient, provider: respx.MockRouter
) -> None:
    login = await client.get("/api/auth/oidc/login")
    state = login.headers["location"].split("state=")[1].split("&")[0]

    callback = await client.get(f"/api/auth/oidc/callback?code=abc&state={state}")
    assert callback.status_code == 302
    assert callback.headers["location"] == "/"

    me = (await client.get("/api/auth/me")).json()
    assert me == {"email": "rider@example.com", "is_admin": True}


async def test_callback_rejects_state_mismatch(
    client: AsyncClient, provider: respx.MockRouter
) -> None:
    await client.get("/api/auth/oidc/login")
    response = await client.get("/api/auth/oidc/callback?code=abc&state=wrong")
    assert response.status_code == 400


async def test_callback_rejects_unknown_identity_when_signups_disabled(
    client: AsyncClient, provider: respx.MockRouter
) -> None:
    from tests.conftest import register

    await register(client, "someone.else@example.com")
    await client.post("/api/auth/logout")

    login = await client.get("/api/auth/oidc/login")
    state = login.headers["location"].split("state=")[1].split("&")[0]
    response = await client.get(f"/api/auth/oidc/callback?code=abc&state={state}")
    assert response.status_code == 403


async def test_login_404_when_not_configured(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/oidc/login")).status_code == 404
