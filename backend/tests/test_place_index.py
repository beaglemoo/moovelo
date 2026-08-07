"""The place index schema, and the config flag that gates it.

The tables here are written only by the indexer sidecar, so these tests
seed them directly rather than through an endpoint. They deliberately
assert on the properties the search queries in later phases depend on -
that pg_trgm is present, that geography distances come back in meters -
because each of those failing would otherwise surface much later as a
confusing wrong answer rather than a missing extension.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CycleWay, Place, Poi, SearchIndexMeta

# Tring and its neighbours, roughly to scale: Tring Station is about 2 km
# from the town, Ivinghoe Beacon about 5 km further on.
TRING = (51.7955, -0.6580)
TRING_STATION = (51.8010, -0.6340)
IVINGHOE = (51.8410, -0.6030)


def point(lat: float, lon: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


def make_place(osm_id: int, name: str, place_type: str, at: tuple[float, float]) -> Place:
    return Place(
        osm_type="n",
        osm_id=osm_id,
        name=name,
        name_norm=name.lower(),
        place_type=place_type,
        importance=0.75 if place_type == "town" else 0.3,
        geog=point(*at),
    )


async def test_config_reports_no_index_before_the_indexer_runs(client: AsyncClient) -> None:
    response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["search_enabled"] is False


async def test_config_reports_the_index_once_metadata_exists(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(
        SearchIndexMeta(
            built_at=datetime.now(UTC),
            source_files=["england-latest.osm.pbf"],
            place_count=2,
            poi_count=1,
            cycle_way_count=1,
        )
    )
    await db.commit()

    response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["search_enabled"] is True


async def test_config_needs_no_session(client: AsyncClient) -> None:
    """The login page reads it before anyone has authenticated."""
    response = await client.get("/api/config")

    assert response.status_code == 200


async def test_only_one_metadata_row_can_exist(db: AsyncSession) -> None:
    def row() -> SearchIndexMeta:
        return SearchIndexMeta(
            built_at=datetime.now(UTC),
            source_files=["england-latest.osm.pbf"],
            place_count=0,
            poi_count=0,
            cycle_way_count=0,
        )

    db.add(row())
    await db.commit()
    db.add(row())

    try:
        await db.commit()
    except Exception:
        await db.rollback()
    else:  # pragma: no cover - only reached if the constraint is dropped
        raise AssertionError("a second metadata row was accepted")

    count = await db.scalar(text("SELECT count(*) FROM search_index_meta"))
    assert count == 1


async def test_trigram_matching_is_available_on_places(db: AsyncSession) -> None:
    """A missing pg_trgm would fail here rather than deep inside search."""
    db.add(make_place(1, "Tring", "town", TRING))
    db.add(make_place(2, "Tring Station", "hamlet", TRING_STATION))
    db.add(make_place(3, "Ivinghoe Beacon", "peak", IVINGHOE))
    await db.commit()

    rows = await db.execute(
        text(
            "SELECT name FROM places "
            "WHERE name_norm LIKE :prefix OR name_norm % :term ORDER BY name"
        ),
        {"prefix": "trin%", "term": "trin"},
    )

    assert [name for (name,) in rows] == ["Tring", "Tring Station"]


async def test_geography_distances_come_back_in_meters(db: AsyncSession) -> None:
    """The column type, not the query, is what makes this true.

    A geometry column would answer in degrees here, and every radius and
    every "how far along the route" figure downstream would be wrong by
    five orders of magnitude while still looking like a plausible number.
    """
    db.add(make_place(1, "Tring", "town", TRING))
    db.add(make_place(2, "Ivinghoe Beacon", "peak", IVINGHOE))
    await db.commit()

    metres = await db.scalar(
        text(
            "SELECT ST_Distance(a.geog, b.geog) FROM places a, places b "
            "WHERE a.name = 'Tring' AND b.name = 'Ivinghoe Beacon'"
        )
    )

    assert metres is not None
    assert 5000 < metres < 7000


async def test_a_poi_needs_no_name(db: AsyncSession) -> None:
    """Drinking fountains and repair stands are usually unnamed, and they
    are exactly the POIs worth finding."""
    db.add(
        Poi(
            osm_type="n",
            osm_id=10,
            category="drinking_water",
            osm_tags={},
            geog=point(*TRING),
        )
    )
    await db.commit()

    name = await db.scalar(text("SELECT name FROM pois WHERE osm_id = 10"))
    assert name is None


async def test_a_cycle_route_is_stored_per_relation(db: AsyncSession) -> None:
    """Keyed on the OSM relation so the overlay can label a line "NCN 6"."""
    db.add(
        CycleWay(
            id=123456,
            ref="6",
            name="National Route 6",
            network="ncn",
            geom="SRID=4326;MULTILINESTRING((-0.658 51.7955, -0.634 51.8010))",
        )
    )
    await db.commit()

    row = await db.execute(text("SELECT ref, network, ST_NumGeometries(geom) FROM cycle_ways"))
    assert row.one() == ("6", "ncn", 1)
