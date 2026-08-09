"""APP_VERSION resolution and its authenticated exposure."""

import tomllib
from pathlib import Path

from httpx import AsyncClient

from app.version import APP_VERSION
from tests.conftest import register


def test_app_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert declared == APP_VERSION
    assert APP_VERSION != "unknown"


async def test_admin_overview_exposes_version(client: AsyncClient) -> None:
    await register(client)  # first user, admin
    overview = (await client.get("/api/admin/overview")).json()
    assert overview["config"]["version"] == APP_VERSION


async def test_settings_exposes_version(client: AsyncClient) -> None:
    await register(client)
    body = (await client.get("/api/settings")).json()
    assert body["version"] == APP_VERSION
