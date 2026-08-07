import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models import Base
from app.schemas import RouteResponse
from app.services.polyline import encode_polyline6

TEST_DB_HOST = "127.0.0.1:5433"
ADMIN_URL = f"postgresql+asyncpg://bikegps:bikegps@{TEST_DB_HOST}/bikegps"

SHAPE_LEG1 = [(53.7996, -1.5491), (53.8008, -1.5523), (53.7950, -1.5600)]
SHAPE_LEG2 = [(53.7950, -1.5600), (53.7900, -1.5700), (53.7850, -1.5750)]


def make_snapshot() -> RouteResponse:
    """A deterministic two-leg route snapshot with maneuvers and elevation."""
    return RouteResponse(
        legs=[
            {
                "geometry": encode_polyline6(SHAPE_LEG1),
                "maneuvers": [
                    {"type": 1, "instruction": "Ride southwest.", "begin_shape_index": 0},
                    {
                        "type": 15,
                        "instruction": "Turn left onto Canal Towpath.",
                        "begin_shape_index": 1,
                    },
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 2},
                ],
            },
            {
                "geometry": encode_polyline6(SHAPE_LEG2),
                "maneuvers": [
                    {"type": 8, "instruction": "Continue.", "begin_shape_index": 0},
                    {
                        "type": 10,
                        "instruction": "Turn right onto Mill Lane.",
                        "begin_shape_index": 1,
                    },
                    {"type": 4, "instruction": "You have arrived.", "begin_shape_index": 2},
                ],
            },
        ],
        distance_m=1930.0,
        duration_s=463.0,
        ascent_m=18.0,
        descent_m=7.0,
        elevation=[
            {"dist_m": 0, "elev_m": 55.0},
            {"dist_m": 500, "elev_m": 63.0},
            {"dist_m": 1200, "elev_m": 70.0},
            {"dist_m": 1930, "elev_m": 66.0},
        ],
    )


@pytest.fixture
def snapshot() -> RouteResponse:
    return make_snapshot()


@pytest.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A throwaway Postgres database for one test (skips if absent)."""
    admin_engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    dbname = f"bikegps_test_{uuid.uuid4().hex[:8]}"
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    except Exception:
        await admin_engine.dispose()
        pytest.skip("test postgres not reachable on 127.0.0.1:5433 (run the dev compose stack)")

    engine = create_async_engine(f"postgresql+asyncpg://bikegps:bikegps@{TEST_DB_HOST}/{dbname}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        # Mirrors migration 0006. The schema here is built by create_all
        # rather than by Alembic, so extensions the migrations enable have
        # to be repeated - and a missing pg_trgm surfaces as an unrelated
        # -looking "operator does not exist: text % text" on the place
        # index tables.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    await admin_engine.dispose()


@pytest.fixture
async def db(db_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session on the same throwaway database the client fixture uses.

    Request one alongside `client` to seed rows the API has no endpoint
    for - the place index tables are written only by the indexer sidecar.
    """
    async with db_factory() as session:
        yield session


@pytest.fixture
async def client(
    db_factory: async_sessionmaker[AsyncSession],
    snapshot: RouteResponse,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """App client backed by a throwaway Postgres database (skips if absent)."""

    async def override_get_db() -> AsyncIterator[Any]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # The Wahoo queue worker opens its own sessions outside the request
    # dependency; point it at the same throwaway database.
    monkeypatch.setattr("app.services.wahoo_queue.session_factory", db_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    app.dependency_overrides.clear()


async def register(http: AsyncClient, email: str = "admin@example.com") -> None:
    response = await http.post(
        "/api/auth/register", json={"email": email, "password": "correct-horse-9"}
    )
    assert response.status_code == 200, response.text
