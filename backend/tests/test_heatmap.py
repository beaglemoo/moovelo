"""The personal heatmap: /api/activities/heatmap/{z}/{x}/{y}.mvt.

Assertions are about tile contents rather than bytes, same as
test_cycle_network.py - the format is a protobuf. The one test that
matters most is the cross-user isolation check: a heatmap SQL query that
forgets its `user_id` filter is invisible in every other test here.
"""

import pytest
from httpx import AsyncClient, Response

from tests.conftest import register
from tests.test_activities_api import GPX_OLDER, GPX_RIDE, _import
from tests.test_cycle_network import tile_of

# Same Chilterns point GPX_RIDE traces through in test_activities_api.py.
LAT, LON = 51.7955, -0.6580
ZOOM = 14


def path(z: int = ZOOM, at: tuple[float, float] = (LAT, LON)) -> str:
    x, y = tile_of(at[0], at[1], z)
    return f"/api/activities/heatmap/{z}/{x}/{y}.mvt"


async def fetch(
    client: AsyncClient, z: int = ZOOM, at: tuple[float, float] = (LAT, LON)
) -> Response:
    response = await client.get(path(z, at))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    return response


async def test_tiles_need_a_session(client: AsyncClient) -> None:
    response = await client.get(path())

    assert response.status_code == 401


async def test_a_rider_with_no_activities_gets_an_empty_tile(client: AsyncClient) -> None:
    await register(client)

    assert (await fetch(client)).content == b""


async def test_an_imported_ride_produces_a_tile(client: AsyncClient) -> None:
    await register(client)
    await _import(client, GPX_RIDE)

    assert len((await fetch(client)).content) > 0


async def test_a_tile_elsewhere_is_empty(client: AsyncClient) -> None:
    """The bounding-box filter is what keeps this off a sequential scan, so
    a tile over the North Sea must come back with nothing in it."""
    await register(client)
    await _import(client, GPX_RIDE)

    assert (await fetch(client, ZOOM, (55.5, 2.5))).content == b""


async def test_one_riders_heatmap_never_shows_another_riders_rides(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst bug this feature could ship, so it is asserted rather than
    reviewed. See the PR description for proof this test actually bites -
    it was run once against SQL with the `user_id` filter removed."""
    monkeypatch.setattr("app.api.auth.settings.signups_enabled", True)
    await register(client, "one@example.com")
    await _import(client, GPX_RIDE)
    await client.post("/api/auth/logout")

    await register(client, "two@example.com")
    assert (await fetch(client)).content == b""


async def test_a_tile_outside_the_pyramid_is_a_404(client: AsyncClient) -> None:
    """At zoom 4 there are 16 tiles per axis, so 20 does not exist."""
    await register(client)

    response = await client.get("/api/activities/heatmap/4/20/3.mvt")

    assert response.status_code == 404


async def test_an_absurd_zoom_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.get("/api/activities/heatmap/28/0/0.mvt")

    assert response.status_code == 422


async def test_a_matching_if_none_match_is_a_304(client: AsyncClient) -> None:
    await register(client)
    await _import(client, GPX_RIDE)
    first = await fetch(client)
    etag = first.headers["etag"]

    second = await client.get(path(), headers={"if-none-match": etag})

    assert second.status_code == 304
    assert second.headers["etag"] == etag


async def test_a_stale_if_none_match_is_a_normal_tile(client: AsyncClient) -> None:
    await register(client)
    await _import(client, GPX_RIDE)

    response = await client.get(path(), headers={"if-none-match": '"not-the-real-one"'})

    assert response.status_code == 200


async def test_importing_another_ride_changes_the_etag(client: AsyncClient) -> None:
    """The ETag has to move the moment a ride is imported, or a stale tile
    would keep 304-ing forever - the opposite failure from the cycle
    network's day-long cache, and just as silent."""
    await register(client)
    await _import(client, GPX_RIDE)
    first_etag = (await fetch(client)).headers["etag"]

    await _import(client, GPX_OLDER)
    second_etag = (await fetch(client)).headers["etag"]

    assert first_etag != second_etag


async def test_each_tile_has_its_own_etag(client: AsyncClient) -> None:
    """An entity tag that does not vary with the entity is a trap.

    HTTP caches key on the URL, so one tag per rider would be safe today -
    but it hands a wrong 304, and so an empty body where a tile should be,
    to anything that revalidates by tag rather than by URL. The coordinates
    are in the fingerprint so that cannot happen.
    """
    await register(client)
    await _import(client, GPX_RIDE)

    here = (await fetch(client)).headers["etag"]
    zoomed = (await fetch(client, z=ZOOM + 1)).headers["etag"]
    elsewhere = (await fetch(client, at=(52.9, -1.5))).headers["etag"]

    assert len({here, zoomed, elsewhere}) == 3, "three tiles, three tags"


async def test_heatmap_available_reports_whether_the_rider_has_rides(
    client: AsyncClient,
) -> None:
    await register(client)

    assert (await client.get("/api/activities/heatmap-available")).json() == {"available": False}

    await _import(client, GPX_RIDE)

    assert (await client.get("/api/activities/heatmap-available")).json() == {"available": True}


async def test_heatmap_available_needs_a_session(client: AsyncClient) -> None:
    response = await client.get("/api/activities/heatmap-available")

    assert response.status_code == 401
