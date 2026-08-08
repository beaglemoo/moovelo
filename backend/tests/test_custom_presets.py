"""CRUD for saved costing-slider presets: ownership, name uniqueness, cap."""

import pytest
from httpx import AsyncClient

from tests.conftest import register

OPTIONS = {
    "bicycle_type": "Cross",
    "cycling_speed": 20,
    "use_roads": 0.3,
    "use_hills": 0.5,
    "avoid_bad_surfaces": 0.0,
}


def body(name: str = "My gravel mix") -> dict[str, object]:
    return {"name": name, "options": OPTIONS}


async def test_create_then_list_returns_it(client: AsyncClient) -> None:
    await register(client)
    created = await client.post("/api/custom-presets", json=body())
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["name"] == "My gravel mix"
    assert payload["options"] == OPTIONS

    listed = (await client.get("/api/custom-presets")).json()
    assert len(listed) == 1
    assert listed[0]["id"] == payload["id"]


async def test_list_is_ordered_by_name(client: AsyncClient) -> None:
    await register(client)
    await client.post("/api/custom-presets", json=body("Zebra"))
    await client.post("/api/custom-presets", json=body("Alpha"))

    listed = (await client.get("/api/custom-presets")).json()
    assert [p["name"] for p in listed] == ["Alpha", "Zebra"]


async def test_name_is_stripped(client: AsyncClient) -> None:
    await register(client)
    created = await client.post("/api/custom-presets", json=body("  Padded  "))
    assert created.json()["name"] == "Padded"


async def test_patch_renames_and_replaces_options(client: AsyncClient) -> None:
    await register(client)
    created = (await client.post("/api/custom-presets", json=body())).json()

    new_options = {**OPTIONS, "cycling_speed": 25}
    patched = await client.patch(
        f"/api/custom-presets/{created['id']}", json={"name": "Renamed", "options": new_options}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["options"] == new_options


async def test_patch_omitting_a_field_leaves_it_unchanged(client: AsyncClient) -> None:
    await register(client)
    created = (await client.post("/api/custom-presets", json=body())).json()

    patched = await client.patch(f"/api/custom-presets/{created['id']}", json={"name": "Renamed"})
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["options"] == OPTIONS


async def test_delete_removes_it(client: AsyncClient) -> None:
    await register(client)
    created = (await client.post("/api/custom-presets", json=body())).json()

    deleted = await client.delete(f"/api/custom-presets/{created['id']}")
    assert deleted.status_code == 204
    assert (await client.get("/api/custom-presets")).json() == []


async def test_duplicate_name_on_create_is_rejected(client: AsyncClient) -> None:
    await register(client)
    await client.post("/api/custom-presets", json=body("Gravel mix"))
    second = await client.post("/api/custom-presets", json=body("Gravel mix"))
    assert second.status_code == 409


async def test_duplicate_name_on_rename_is_rejected(client: AsyncClient) -> None:
    await register(client)
    await client.post("/api/custom-presets", json=body("Existing"))
    other = (await client.post("/api/custom-presets", json=body("Other"))).json()

    renamed = await client.patch(f"/api/custom-presets/{other['id']}", json={"name": "Existing"})
    assert renamed.status_code == 409


async def test_the_twenty_first_preset_is_rejected(client: AsyncClient) -> None:
    await register(client)
    for i in range(20):
        created = await client.post("/api/custom-presets", json=body(f"Preset {i}"))
        assert created.status_code == 201

    over_cap = await client.post("/api/custom-presets", json=body("One too many"))
    assert over_cap.status_code == 422


async def test_bad_bicycle_type_is_rejected(client: AsyncClient) -> None:
    await register(client)
    bad_options = {**OPTIONS, "bicycle_type": "Unicycle"}
    bad = await client.post("/api/custom-presets", json={"name": "Bad", "options": bad_options})
    assert bad.status_code == 422


@pytest.mark.parametrize("method,path_suffix", [("PATCH", ""), ("DELETE", "")])
async def test_another_users_preset_404s(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, path_suffix: str
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    created = (await client.post("/api/custom-presets", json=body())).json()
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    response = await client.request(
        method, f"/api/custom-presets/{created['id']}{path_suffix}", json=body("Renamed")
    )
    assert response.status_code == 404
    assert (await client.get("/api/custom-presets")).json() == []


async def test_another_users_preset_is_not_listed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "owner@example.com")
    await client.post("/api/custom-presets", json=body())
    await client.post("/api/auth/logout")

    await register(client, "stranger@example.com")
    assert (await client.get("/api/custom-presets")).json() == []
