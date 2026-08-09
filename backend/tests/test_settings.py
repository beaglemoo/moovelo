"""Per-user rider settings: get-or-default, partial patch, bounds, isolation."""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.settings import update_settings
from app.models import User, UserSettings
from app.schemas import UserSettingsPatch
from app.version import APP_VERSION
from tests.conftest import register


async def test_get_before_any_patch_returns_defaults_and_creates_no_row(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)

    response = await client.get("/api/settings")
    assert response.status_code == 200
    assert response.json() == {
        "weight_kg": 78.0,
        "flat_speed_kmh": 22.0,
        "ftp_watts": None,
        "version": APP_VERSION,
    }

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
    assert default == {
        "weight_kg": 78.0,
        "flat_speed_kmh": 22.0,
        "ftp_watts": None,
        "version": APP_VERSION,
    }

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


async def test_ftp_watts_zero_is_rejected(client: AsyncClient) -> None:
    """0 would cube down to a zero speed in effective_flat_speed; that
    meaning already belongs to omitting the field or sending null."""
    await register(client)
    response = await client.patch("/api/settings", json={"ftp_watts": 0})
    assert response.status_code == 422


async def test_explicit_null_weight_is_rejected_with_a_clear_message(
    client: AsyncClient,
) -> None:
    await register(client)
    await client.patch("/api/settings", json={"weight_kg": 82.0})

    response = await client.patch("/api/settings", json={"weight_kg": None})

    assert response.status_code == 422
    assert "weight_kg cannot be null" in response.text
    # Unaffected: the row still has its last real value.
    assert (await client.get("/api/settings")).json()["weight_kg"] == 82.0


async def test_explicit_null_flat_speed_is_rejected_with_a_clear_message(
    client: AsyncClient,
) -> None:
    await register(client)
    response = await client.patch("/api/settings", json={"flat_speed_kmh": None})
    assert response.status_code == 422
    assert "flat_speed_kmh cannot be null" in response.text


async def test_omitting_weight_still_leaves_it_unchanged(client: AsyncClient) -> None:
    """The validator that rejects an explicit null must not also fire on the
    field simply being absent - that is what keeps a partial patch partial."""
    await register(client)
    await client.patch("/api/settings", json={"weight_kg": 82.0})

    response = await client.patch("/api/settings", json={"flat_speed_kmh": 25.0})

    assert response.status_code == 200
    assert response.json()["weight_kg"] == 82.0
    assert response.json()["flat_speed_kmh"] == 25.0


async def test_concurrent_first_patches_recover_from_the_insert_race(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent first PATCHes for the same user both pass the
    "does a row exist" select and both try to insert the same user_id
    primary key. Reproduced for real, not mocked: a barrier forces both
    requests' selects to land before either commits, so the loser hits a
    genuine IntegrityError from Postgres and must recover rather than 500.
    """
    await register(client)
    async with db_factory() as lookup:
        user = (await lookup.execute(select(User))).scalar_one()

    barrier = asyncio.Barrier(2)
    orig_commit = AsyncSession.commit
    synced: set[int] = set()

    async def synced_commit(self: AsyncSession) -> None:
        # Only the first commit of each session waits - the recovery path's
        # own follow-up commit (after a rollback) must proceed immediately,
        # or the two tasks would deadlock waiting on each other a second time.
        if id(self) not in synced:
            synced.add(id(self))
            await barrier.wait()
        await orig_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", synced_commit)

    async with db_factory() as session_a, db_factory() as session_b:
        results = await asyncio.gather(
            update_settings(UserSettingsPatch(weight_kg=85.0), session_a, user),
            update_settings(UserSettingsPatch(weight_kg=90.0), session_b, user),
        )

    # Both requests succeeded with their own values - neither 500ed.
    assert {r.weight_kg for r in results} == {85.0, 90.0}

    async with db_factory() as check:
        rows = (await check.execute(select(UserSettings))).scalars().all()
    # The PK constraint held: exactly one row survives the race.
    assert len(rows) == 1
