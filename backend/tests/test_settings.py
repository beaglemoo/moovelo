"""Per-user rider settings: get-or-default, partial patch, bounds, isolation."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserSettings
from tests.conftest import register


async def test_get_before_any_patch_returns_defaults_and_creates_no_row(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)

    response = await client.get("/api/settings")
    assert response.status_code == 200
    assert response.json() == {"weight_kg": 78.0, "flat_speed_kmh": 22.0, "ftp_watts": None}

    rows = (await db.execute(select(UserSettings))).scalars().all()
    assert rows == []


async def test_first_patch_creates_the_row(client: AsyncClient) -> None:
    await register(client)

    response = await client.patch("/api/settings", json={"weight_kg": 80.0})
    assert response.status_code == 200
    body = response.json()
    assert body["weight_kg"] == 80.0
    assert body["flat_speed_kmh"] == 22.0
    assert body["ftp_watts"] is None


async def test_partial_patch_preserves_other_fields(client: AsyncClient) -> None:
    await register(client)
    await client.patch("/api/settings", json={"weight_kg": 90.0, "ftp_watts": 250.0})

    response = await client.patch("/api/settings", json={"flat_speed_kmh": 25.0})
    assert response.status_code == 200
    body = response.json()
    assert body["weight_kg"] == 90.0
    assert body["flat_speed_kmh"] == 25.0
    assert body["ftp_watts"] == 250.0


async def test_ftp_watts_null_clears_it_while_omitting_leaves_it_set(
    client: AsyncClient,
) -> None:
    await register(client)
    set_ftp = await client.patch("/api/settings", json={"ftp_watts": 200.0})
    assert set_ftp.json()["ftp_watts"] == 200.0

    # Omitting the field entirely must not touch it.
    unrelated = await client.patch("/api/settings", json={"weight_kg": 82.0})
    assert unrelated.json()["ftp_watts"] == 200.0

    # Explicitly sending null must clear it.
    cleared = await client.patch("/api/settings", json={"ftp_watts": None})
    assert cleared.json()["ftp_watts"] is None
    assert cleared.json()["weight_kg"] == 82.0


async def test_weight_out_of_bounds_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await client.patch("/api/settings", json={"weight_kg": 500.0})
    assert response.status_code == 422


async def test_unauthenticated_get_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/api/settings")).status_code == 401


async def test_settings_are_scoped_per_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    await client.patch("/api/settings", json={"weight_kg": 65.0, "ftp_watts": 180.0})
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    default = (await client.get("/api/settings")).json()
    assert default == {"weight_kg": 78.0, "flat_speed_kmh": 22.0, "ftp_watts": None}

    await client.patch("/api/settings", json={"weight_kg": 95.0})
    stranger = (await client.get("/api/settings")).json()
    assert stranger["weight_kg"] == 95.0

    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse-9"}
    )
    owner = (await client.get("/api/settings")).json()
    assert owner["weight_kg"] == 65.0
    assert owner["ftp_watts"] == 180.0
