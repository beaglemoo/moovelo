"""Place search ranking.

Seeded directly, because the place index has no write endpoint - the
indexer sidecar fills it. The fixtures mirror what the real England index
actually contains, including the awkward parts: several places sharing a
name, and a railway station named exactly like the town it serves.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Place
from app.services.places import normalise
from tests.conftest import register

TRING = (51.7955, -0.6580)
TRING_STATION = (51.8010, -0.6340)
IVINGHOE = (51.8410, -0.6030)
BIRMINGHAM = (52.4800, -1.9000)
# Far enough that proximity is decisive rather than marginal.
ISLE_OF_WIGHT = (50.7000, -1.2900)


def place(
    osm_id: int,
    name: str,
    place_type: str,
    at: tuple[float, float],
    importance: float,
) -> Place:
    return Place(
        osm_type="n",
        osm_id=osm_id,
        name=name,
        name_norm=normalise(name),
        place_type=place_type,
        importance=importance,
        geog=f"SRID=4326;POINT({at[1]} {at[0]})",
    )


async def seed(db: AsyncSession, *places: Place) -> None:
    for row in places:
        db.add(row)
    await db.commit()


async def search(client: AsyncClient, q: str, **params: float | int) -> list[dict[str, object]]:
    response = await client.get("/api/places/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    results: list[dict[str, object]] = response.json()
    return results


async def names(client: AsyncClient, q: str, **params: float | int) -> list[str]:
    return [str(r["name"]) for r in await search(client, q, **params)]


async def test_search_needs_a_session(client: AsyncClient) -> None:
    response = await client.get("/api/places/search", params={"q": "tring"})

    assert response.status_code == 401


async def test_an_unbuilt_index_returns_nothing_rather_than_failing(client: AsyncClient) -> None:
    """The indexer is opt-in, so an empty index is a normal state, not an
    error - the frontend hides search entirely in that case."""
    await register(client)

    assert await names(client, "tring") == []


async def test_a_prefix_finds_the_place(client: AsyncClient, db: AsyncSession) -> None:
    """Typing "trin" must find Tring before the word is finished."""
    await register(client)
    await seed(db, place(1, "Tring", "town", TRING, 0.65))

    assert "Tring" in await names(client, "trin")


async def test_an_exact_match_outranks_a_longer_prefix_match(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    await seed(
        db,
        place(1, "Tring Station", "village", TRING_STATION, 0.40),
        place(2, "Tring", "town", TRING, 0.65),
    )

    assert (await names(client, "tring"))[0] == "Tring"


async def test_importance_separates_places_with_the_same_name(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Real data has a railway station called exactly "Tring", two miles
    from the town called exactly "Tring"."""
    await register(client)
    await seed(
        db,
        place(1, "Tring", "station", TRING_STATION, 0.25),
        place(2, "Tring", "town", TRING, 0.65),
    )

    first = (await search(client, "tring"))[0]
    assert first["place_type"] == "town"


async def test_a_typo_still_finds_the_city(client: AsyncClient, db: AsyncSession) -> None:
    """Trigram matching, and the reason the % operator is used rather than
    a similarity() comparison: only the operator uses the index."""
    await register(client)
    await seed(db, place(1, "Birmingham", "city", BIRMINGHAM, 1.0))

    assert "Birmingham" in await names(client, "birmingam")


async def test_the_nearer_of_two_identical_places_wins(
    client: AsyncClient, db: AsyncSession
) -> None:
    """21,848 English places share a name with another. Where importance
    and text score tie, the map centre is the only thing left."""
    await register(client)
    await seed(
        db,
        place(1, "Newport", "town", ISLE_OF_WIGHT, 0.65),
        place(2, "Newport", "town", TRING, 0.65),
    )

    near_wight = await search(client, "newport", near_lat=50.70, near_lon=-1.29)
    near_tring = await search(client, "newport", near_lat=51.795, near_lon=-0.658)

    assert near_wight[0]["lat"] == ISLE_OF_WIGHT[0]
    assert near_tring[0]["lat"] == TRING[0]


async def test_distance_is_reported_only_when_a_centre_is_given(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    await seed(db, place(1, "Ivinghoe Beacon", "peak", IVINGHOE, 0.30))

    with_centre = (await search(client, "ivinghoe", near_lat=51.7955, near_lon=-0.6580))[0]
    without = (await search(client, "ivinghoe"))[0]

    assert without["distance_m"] is None
    distance = with_centre["distance_m"]
    assert isinstance(distance, float)
    # Ivinghoe Beacon is about 5 km from Tring.
    assert 4000 < distance < 7000


async def test_accents_are_folded_both_when_indexing_and_when_searching(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The indexer writes name_norm; this asserts the search side folds the
    query the same way. If the two ever diverge, accented names silently
    stop matching."""
    await register(client)
    await seed(db, place(1, "Ynys Môn", "village", TRING, 0.40))

    assert await names(client, "ynys mon") == ["Ynys Môn"]
    assert await names(client, "Ynys Môn") == ["Ynys Môn"]


async def test_a_wildcard_in_the_query_matches_nothing(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Unescaped, "t%" would prefix-match a large slice of the index.

    Note the trigram branch still applies: pg_trgm ignores punctuation, so
    "tr%" is fuzzy-similar to "tring" and finds it. That is wanted - what
    is not wanted is the wildcard reaching LIKE and dragging in everything
    else, which is what this pins.
    """
    await register(client)
    await seed(
        db,
        place(1, "Tring", "town", TRING, 0.65),
        place(2, "Birmingham", "city", BIRMINGHAM, 1.0),
        place(3, "Ivinghoe", "village", IVINGHOE, 0.40),
    )

    assert await names(client, "t%") == []
    assert "Birmingham" not in await names(client, "tr%")
    assert "Ivinghoe" not in await names(client, "tr_")


async def test_a_one_character_query_is_rejected(client: AsyncClient) -> None:
    """Trigrams are meaningless below three characters and a single letter
    would scan the index; the frontend does not send them either."""
    await register(client)

    response = await client.get("/api/places/search", params={"q": "t"})

    assert response.status_code == 422


async def test_the_limit_is_capped(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)

    response = await client.get("/api/places/search", params={"q": "tring", "limit": 500})

    assert response.status_code == 422


async def test_half_a_coordinate_is_ignored_rather_than_failing(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    await seed(db, place(1, "Tring", "town", TRING, 0.65))

    results = await search(client, "tring", near_lat=51.795)

    assert results[0]["distance_m"] is None
