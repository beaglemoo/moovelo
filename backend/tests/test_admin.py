"""Admin endpoints and SSO-only login mode."""

import pytest
from httpx import AsyncClient

from app.config import settings
from tests.conftest import register


@pytest.fixture
def sso_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", "https://id.test")
    monkeypatch.setattr(settings, "oidc_client_id", "c")
    monkeypatch.setattr(settings, "oidc_client_secret", "s")
    monkeypatch.setattr(settings, "password_auth_enabled", False)


async def test_password_login_disabled(client: AsyncClient, sso_only: None) -> None:
    status = (await client.get("/api/auth/status")).json()
    assert status["password_login"] is False
    creds = {"email": "a@example.com", "password": "password-123"}
    assert (await client.post("/api/auth/register", json=creds)).status_code == 403
    assert (await client.post("/api/auth/login", json=creds)).status_code == 403


async def test_password_disable_ignored_without_oidc(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "password_auth_enabled", False)
    status = (await client.get("/api/auth/status")).json()
    assert status["password_login"] is True  # no OIDC -> flag ignored, no lockout


async def test_admin_overview_and_delete(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(client)  # first user, admin

    overview = (await client.get("/api/admin/overview")).json()
    assert overview["stats"]["user_count"] == 1
    assert overview["users"][0]["is_admin"] is True
    assert overview["config"]["password_login"] is True

    admin_id = overview["users"][0]["id"]
    self_delete = await client.delete(f"/api/admin/users/{admin_id}")
    assert self_delete.status_code == 400

    monkeypatch.setattr(settings, "signups_enabled", True)
    await client.post("/api/auth/logout")
    await register(client, "second@example.com")

    # Second user is not admin.
    assert (await client.get("/api/admin/overview")).status_code == 403

    # Back as admin, delete the second user.
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "correct-horse-9"}
    )
    overview = (await client.get("/api/admin/overview")).json()
    second = next(u for u in overview["users"] if u["email"] == "second@example.com")
    assert (await client.delete(f"/api/admin/users/{second['id']}")).status_code == 204
    overview = (await client.get("/api/admin/overview")).json()
    assert overview["stats"]["user_count"] == 1
