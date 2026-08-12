"""GET /api/coverage/roads and osm_ways' own provenance (migration 0016).

A rebuild with INDEX_ROADS off leaves the live osm_ways table untouched but
always overwrites search_index_meta.source_files with the run that just
finished (indexer/db.py:publish) - so an install that indexed one extract
with roads on and later reruns against a *different* extract with roads off
ends up with source_files describing the new extract while osm_ways still
describes the old one. osm_ways_source_files is the indexer's own record of
which extract actually built the road table, carried forward on a run that
left it alone - this file is the backend side of that fix: what
/api/coverage/roads does with the two columns once they disagree.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OsmWay, SearchIndexMeta
from tests.conftest import register

LAT, LON = 51.8000, -0.6500
BBOX = {"min_lat": LAT - 0.05, "min_lon": LON - 0.05, "max_lat": LAT + 0.05, "max_lon": LON + 0.05}


def _meta(
    *, source_files: list[str], osm_ways_source_files: list[str] | None, way_count: int | None = 1
) -> SearchIndexMeta:
    return SearchIndexMeta(
        built_at=datetime.now(UTC),
        source_files=source_files,
        place_count=0,
        poi_count=0,
        cycle_way_count=0,
        cycle_way_member_count=None,
        osm_way_count=way_count,
        osm_ways_source_files=osm_ways_source_files,
    )


def _way(way_id: int) -> OsmWay:
    return OsmWay(
        way_id=way_id,
        highway="residential",
        name=None,
        length_m=1000.0,
        geom=f"SRID=4326;LINESTRING({LON} {LAT},{LON} {LAT + 0.01})",
    )


async def test_osm_ways_built_from_a_different_extract_than_the_rest_of_the_index_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The exact scenario the brief describes, reproduced at the API layer
    a level up from indexer/tests/test_db.py's own coverage of publish()
    itself: England indexed with roads on, then a Wales rebuild with roads
    off. osm_ways still holds England's roads and osm_way_count is real, but
    osm_ways_source_files (carried forward, unchanged by the roads-off run)
    no longer matches source_files (overwritten every run) - and that
    mismatch, not merely osm_way_count being set, is what must gate the
    endpoint."""
    db.add_all(
        [
            _way(100),
            _meta(
                source_files=["wales-latest.osm.pbf"],
                osm_ways_source_files=["england-latest.osm.pbf"],
            ),
        ]
    )
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/roads", params=BBOX)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "different extract" in body["reason"]
    assert body["highways"] == []


async def test_matching_provenance_is_available(client: AsyncClient, db: AsyncSession) -> None:
    """The healthy case: osm_ways_source_files and source_files agree
    because the same run built both (or a roads-on rebuild refreshed osm_ways
    to match the rest of the index) - coverage must not be refused just
    because the column exists."""
    db.add_all(
        [
            _way(100),
            _meta(
                source_files=["england-latest.osm.pbf"],
                osm_ways_source_files=["england-latest.osm.pbf"],
            ),
        ]
    )
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/roads", params=BBOX)

    body = response.json()
    assert body["available"] is True


async def test_null_provenance_is_treated_as_unknown_not_stale(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The transitional case: an install that built osm_ways before this
    column existed has osm_way_count set but osm_ways_source_files still
    NULL, until its own next roads-on rebuild fills it in. Refusing coverage
    here would regress every existing install's coverage the moment this
    migration lands, to guard against a mismatch nothing has actually
    observed - so NULL reads as "not yet tracked", not as a second reason to
    refuse alongside a real mismatch."""
    db.add_all(
        [
            _way(100),
            _meta(source_files=["england-latest.osm.pbf"], osm_ways_source_files=None),
        ]
    )
    await db.commit()
    await register(client)

    response = await client.get("/api/coverage/roads", params=BBOX)

    body = response.json()
    assert body["available"] is True
