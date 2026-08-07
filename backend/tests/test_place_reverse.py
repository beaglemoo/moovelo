"""Reverse geocoding: the nearest named place to a point.

Seeded through the ORM like the search tests - the indexer writes these
tables, so there is no endpoint to create rows with.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import register
from tests.test_place_search import BIRMINGHAM, IVINGHOE, TRING, place, seed

# In the North Sea, well over 50 km from anywhere in the fixtures.
NORTH_SEA = (55.5000, 2.5000)
# A field between Tring and Ivinghoe Beacon: nearer Tring, but not on it.
BETWEEN = (51.8100, -0.6400)


async def reverse(client: AsyncClient, at: tuple[float, float]) -> dict[str, object] | None:
    response = await client.get("/api/places/reverse", params={"lat": at[0], "lon": at[1]})
    assert response.status_code == 200, response.text
    result: dict[str, object] | None = response.json()
    return result


async def test_reverse_needs_a_session(client: AsyncClient) -> None:
    response = await client.get("/api/places/reverse", params={"lat": TRING[0], "lon": TRING[1]})

    assert response.status_code == 401


async def test_an_unbuilt_index_returns_null_rather_than_failing(client: AsyncClient) -> None:
    await register(client)

    assert await reverse(client, TRING) is None


async def test_the_nearest_place_wins(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(
        db,
        place(1, "Tring", "town", TRING, 0.65),
        place(2, "Ivinghoe Beacon", "peak", IVINGHOE, 0.30),
    )

    found = await reverse(client, BETWEEN)

    assert found is not None
    assert found["name"] == "Tring"


async def test_nearest_beats_more_important(client: AsyncClient, db: AsyncSession) -> None:
    """Deliberate: naming a point after the hamlet you are standing in is
    more useful than after the city an hour away, even though search ranks
    them the other way round."""
    await register(client)
    await seed(
        db,
        place(1, "Birmingham", "city", BIRMINGHAM, 1.0),
        place(2, "Ivinghoe Beacon", "peak", IVINGHOE, 0.30),
    )

    found = await reverse(client, IVINGHOE)

    assert found is not None
    assert found["name"] == "Ivinghoe Beacon"


async def test_a_point_far_from_everything_returns_null(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Without the 50 km cap, a click out at sea would be named after
    whichever town happened to be least far away."""
    await register(client)
    await seed(db, place(1, "Tring", "town", TRING, 0.65))

    assert await reverse(client, NORTH_SEA) is None


async def test_the_distance_is_reported_in_metres(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(db, place(1, "Tring", "town", TRING, 0.65))

    found = await reverse(client, TRING)

    assert found is not None
    distance = found["distance_m"]
    assert isinstance(distance, float)
    assert distance < 1.0


async def test_an_out_of_range_coordinate_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.get("/api/places/reverse", params={"lat": 91, "lon": 0})

    assert response.status_code == 422


async def test_both_coordinates_are_required(client: AsyncClient) -> None:
    await register(client)

    response = await client.get("/api/places/reverse", params={"lat": TRING[0]})

    assert response.status_code == 422
