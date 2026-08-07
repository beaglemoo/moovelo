"""Reverse geocoding: naming a point from the place index.

Seeded through the ORM like the search tests - the indexer writes these
tables, so there is no endpoint to create rows with.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import register
from tests.test_place_search import TRING, place, seed

# In the North Sea, well over 50 km from anywhere in the fixtures.
NORTH_SEA = (55.5000, 2.5000)
# A field between Tring and Aylesbury: nearer Tring, but in neither.
BETWEEN = (51.8100, -0.6400)
AYLESBURY = (51.8156, -0.8084)

# Wilstone village, a locality 2 km north of it, and a point in the field
# 500 m short of that locality - so the locality is much the nearer of the
# two, and much the worse answer.
WILSTONE = (51.8168, -0.6822)
FIELD_CORNER = (51.8348, -0.6822)
FIELD = (51.8303, -0.6822)
# 15 km east of Wilstone: inside the index scan's 50 km bound, far outside
# a hamlet's 3 km reach.
OUT_OF_REACH = (51.8168, -0.4642)


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


async def test_the_nearest_place_wins_between_equals(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(
        db,
        place(1, "Tring", "town", TRING, 0.65),
        place(2, "Aylesbury", "town", AYLESBURY, 0.65),
    )

    found = await reverse(client, BETWEEN)

    assert found is not None
    assert found["name"] == "Tring"


async def test_a_locality_does_not_beat_the_village_it_sits_in(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The whole reason reverse geocoding is not a nearest-neighbour query.

    Over a third of England's indexed places are place=locality - field
    names, bridges, trailheads - and they are almost always the nearest
    named thing. Ranked by distance alone, open farmland north of Tring
    comes back as "Dixon's Gap Bridge".
    """
    await register(client)
    await seed(
        db,
        place(1, "Dixon's Gap Bridge", "locality", FIELD_CORNER, 0.15),
        place(2, "Wilstone", "village", WILSTONE, 0.40),
    )

    found = await reverse(client, FIELD)

    assert found is not None
    assert found["name"] == "Wilstone"


async def test_a_locality_still_wins_when_you_are_standing_on_it(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Reach is a radius, not a veto: within about a kilometre the locality
    is the better answer, and a whitelist would have thrown it away."""
    await register(client)
    await seed(
        db,
        place(1, "Dixon's Gap Bridge", "locality", FIELD_CORNER, 0.15),
        place(2, "Wilstone", "village", WILSTONE, 0.40),
    )

    found = await reverse(client, FIELD_CORNER)

    assert found is not None
    assert found["name"] == "Dixon's Gap Bridge"


async def test_a_point_far_from_everything_returns_null(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Without the cap, a click out at sea would be named after whichever
    town happened to be least far away."""
    await register(client)
    await seed(db, place(1, "Tring", "town", TRING, 0.65))

    assert await reverse(client, NORTH_SEA) is None


async def test_a_place_out_of_its_own_reach_is_not_used(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A hamlet 15 km away is not where you are, even if it is the only
    thing in the index. A city at that distance would be."""
    await register(client)
    await seed(db, place(1, "Wilstone", "hamlet", WILSTONE, 0.25))

    assert await reverse(client, OUT_OF_REACH) is None


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
