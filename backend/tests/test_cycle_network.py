"""Cycle-network vector tiles.

The assertions are about tile contents rather than bytes: an MVT is a
protobuf, so these check that a tile is non-empty when a route crosses it,
empty when none does, and that the zoom tiers drop the right networks.
"""

import math

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CycleWay
from tests.conftest import register

# Two points on the Chilterns, and a tile that contains them at any zoom
# computed below.
LAT = 51.8000
LON = -0.6500


def tile_of(lat: float, lon: float, z: int) -> tuple[int, int]:
    """Web mercator tile containing a point, the standard slippy-map maths."""
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int(
        (1.0 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi)
        / 2.0
        * n
    )
    return x, y


def route(relation_id: int, network: str, ref: str, at: tuple[float, float]) -> CycleWay:
    """A short two-segment route near `at`, as the indexer would store it."""
    lat, lon = at
    return CycleWay(
        id=relation_id,
        ref=ref,
        name=f"{network.upper()} {ref}",
        network=network,
        operator="Sustrans",
        geom=(
            f"SRID=4326;MULTILINESTRING(({lon} {lat},{lon + 0.01} {lat + 0.01}),"
            f"({lon + 0.01} {lat + 0.01},{lon + 0.02} {lat + 0.02}))"
        ),
    )


async def seed(db: AsyncSession, *routes: CycleWay) -> None:
    for row in routes:
        db.add(row)
    await db.commit()


async def fetch(client: AsyncClient, z: int, at: tuple[float, float] = (LAT, LON)) -> bytes:
    x, y = tile_of(at[0], at[1], z)
    response = await client.get(f"/api/places/cycle-network/{z}/{x}/{y}.mvt")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    return response.content


async def test_tiles_need_a_session(client: AsyncClient) -> None:
    response = await client.get("/api/places/cycle-network/12/2036/1352.mvt")

    assert response.status_code == 401


async def test_an_unbuilt_index_returns_an_empty_tile_rather_than_failing(
    client: AsyncClient,
) -> None:
    """The indexer is opt-in, so no cycle routes is a normal state. MapLibre
    treats a zero-length body as an empty tile, which is what we want - a
    404 would show up in the console on every pan."""
    await register(client)

    assert await fetch(client, 12) == b""


async def test_a_route_in_the_tile_produces_a_tile(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(db, route(1, "ncn", "6", (LAT, LON)))

    assert len(await fetch(client, 12)) > 0


async def test_a_tile_elsewhere_is_empty(client: AsyncClient, db: AsyncSession) -> None:
    """The bounding-box filter is what keeps this off a sequential scan, so
    a tile over the North Sea must come back with nothing in it."""
    await register(client)
    await seed(db, route(1, "ncn", "6", (LAT, LON)))

    assert await fetch(client, 12, (55.5, 2.5)) == b""


async def test_local_routes_are_dropped_when_zoomed_out(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A county-wide view of every local route is unreadable, so the tiers
    drop detail the way a paper map does."""
    await register(client)
    await seed(db, route(1, "lcn", "12", (LAT, LON)))

    assert len(await fetch(client, 11)) > 0
    assert await fetch(client, 10) == b""


async def test_regional_routes_are_dropped_at_country_scale(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    await seed(db, route(1, "rcn", "30", (LAT, LON)))

    assert len(await fetch(client, 8)) > 0
    assert await fetch(client, 7) == b""


async def test_national_routes_survive_every_zoom(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(db, route(1, "ncn", "6", (LAT, LON)))

    for zoom in (5, 8, 11, 14):
        assert len(await fetch(client, zoom)) > 0, f"empty at z{zoom}"


async def test_a_tile_outside_the_pyramid_is_a_404(client: AsyncClient) -> None:
    """At zoom 4 there are 16 tiles per axis, so 20 does not exist. Silently
    returning an empty tile would hide a client bug."""
    await register(client)

    response = await client.get("/api/places/cycle-network/4/20/3.mvt")

    assert response.status_code == 404


async def test_an_absurd_zoom_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.get("/api/places/cycle-network/28/0/0.mvt")

    assert response.status_code == 422
