"""POIs along a route.

The line is seeded as a straight run of coordinates rather than a real
Valhalla polyline: what is under test is the spatial maths, and a straight
line makes the expected distances arithmetic rather than guesswork.
"""

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Poi
from app.schemas import MAX_POI_RESULTS, MAX_ROUTE_POINTS
from tests.conftest import register

# A 4 km line due east along the 51.8 parallel. At this latitude a degree of
# longitude is about 68.8 km, so 0.0580 -> 0.0000 spans roughly 4 km.
WEST = -0.0580
EAST = 0.0000
LAT = 51.8000
LINE = [[WEST, LAT], [EAST, LAT]]
# One degree of latitude is about 111.2 km, so this offset is ~200 m north.
NORTH_200M = 0.0018
NORTH_2KM = 0.0180


def poi(
    osm_id: int,
    category: str,
    lon: float,
    lat: float,
    name: str | None = None,
    tags: dict[str, str] | None = None,
) -> Poi:
    return Poi(
        osm_type="n",
        osm_id=osm_id,
        name=name,
        name_norm=name.casefold() if name else None,
        category=category,
        osm_tags=tags or {},
        geog=f"SRID=4326;POINT({lon} {lat})",
    )


async def seed(db: AsyncSession, *pois: Poi) -> None:
    for row in pois:
        db.add(row)
    await db.commit()


async def find(
    client: AsyncClient,
    line: list[list[float]] | None = None,
    **body: Any,
) -> dict[str, Any]:
    response = await client.post(
        "/api/places/pois-along-route",
        json={"line": line if line is not None else LINE, **body},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def names(client: AsyncClient, **body: Any) -> list[str | None]:
    return [p["name"] for p in (await find(client, **body))["pois"]]


async def test_pois_need_a_session(client: AsyncClient) -> None:
    response = await client.post("/api/places/pois-along-route", json={"line": LINE})

    assert response.status_code == 401


async def test_an_unbuilt_index_returns_nothing_rather_than_failing(client: AsyncClient) -> None:
    await register(client)

    assert await names(client) == []


async def test_results_are_ordered_along_the_route_not_by_distance_from_it(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The ordering is the whole feature: a list of nearby POIs is not a
    plan for a ride, a list in the order you will pass them is."""
    await register(client)
    await seed(
        db,
        # Seeded back to front, and the last one is nearest to the line.
        poi(1, "cafe", -0.0100, LAT + NORTH_200M, name="Third"),
        poi(2, "cafe", -0.0400, LAT + NORTH_200M, name="Second"),
        poi(3, "cafe", -0.0550, LAT, name="First"),
    )

    assert await names(client) == ["First", "Second", "Third"]


async def test_distance_along_the_route_is_reported_in_metres(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    # Halfway along a line of roughly 4 km.
    await seed(db, poi(1, "water", (WEST + EAST) / 2, LAT))

    found = (await find(client))["pois"][0]

    assert 1800 < found["dist_along_m"] < 2200
    assert found["dist_from_route_m"] < 1.0


async def test_a_poi_beyond_the_radius_is_left_out(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(
        db,
        poi(1, "cafe", -0.0300, LAT + NORTH_200M, name="Near"),
        poi(2, "cafe", -0.0300, LAT + NORTH_2KM, name="Far"),
    )

    assert await names(client) == ["Near"]
    assert await names(client, radius_m=3000) == ["Near", "Far"]


async def test_categories_filter_the_list(client: AsyncClient, db: AsyncSession) -> None:
    """232 of the 722 POIs within 250 m of a real 50 km ride are unnamed
    bike parking. Without a filter the useful ones are unfindable."""
    await register(client)
    await seed(
        db,
        poi(1, "bike_parking", -0.0500, LAT),
        poi(2, "water", -0.0400, LAT),
        poi(3, "cafe", -0.0300, LAT, name="The Cafe"),
    )

    everything = (await find(client))["pois"]
    assert len(everything) == 3

    picked = await find(client, categories=["water", "cafe"])
    assert [p["category"] for p in picked["pois"]] == ["water", "cafe"]


async def test_an_unknown_category_matches_nothing_rather_than_everything(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    await seed(db, poi(1, "cafe", -0.0300, LAT, name="The Cafe"))

    assert await names(client, categories=["brewery"]) == []


async def test_unnamed_pois_are_returned(client: AsyncClient, db: AsyncSession) -> None:
    """A drinking fountain rarely has a name, and is exactly what you want
    to find. The UI renders the category for these."""
    await register(client)
    await seed(db, poi(1, "water", -0.0300, LAT))

    found = (await find(client))["pois"]

    assert len(found) == 1
    assert found[0]["name"] is None
    assert found[0]["category"] == "water"


async def test_opening_hours_come_back_with_the_poi(client: AsyncClient, db: AsyncSession) -> None:
    await register(client)
    await seed(
        db,
        poi(1, "cafe", -0.0300, LAT, name="The Cafe", tags={"opening_hours": "Mo-Su 08:00-17:00"}),
    )

    assert (await find(client))["pois"][0]["tags"]["opening_hours"] == "Mo-Su 08:00-17:00"


async def test_a_dense_start_does_not_eat_the_whole_answer(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The cap must be spread along the ride, not taken from the front.

    Real POI density is urban-clustered: a route out of Oxford had 498 POIs
    within 500 m, and the first 301 by distance-along all fell inside the
    opening 233 m - so the answer described 0.6% of a 38 km ride while
    reporting only "truncated". The test that existed seeded POIs evenly
    spaced along the line, a distribution that can never expose this.
    """
    await register(client)
    town = [
        poi(i, "cafe", WEST + (i % 40) * 0.000_02, LAT, name=f"Town cafe {i}")
        for i in range(MAX_POI_RESULTS + 100)
    ]
    # Three lonely ones spread over the rest of the 4 km line.
    country = [
        poi(90_001, "water", -0.0300, LAT, name="Halfway tap"),
        poi(90_002, "water", -0.0150, LAT, name="Three quarter tap"),
        poi(90_003, "water", -0.0010, LAT, name="Last tap"),
    ]
    await seed(db, *town, *country)

    found = await find(client)
    names_found = {p["name"] for p in found["pois"]}

    assert {"Halfway tap", "Three quarter tap", "Last tap"} <= names_found
    assert found["truncated"] is True


async def test_a_clustered_route_still_returns_everything_that_fits(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The correction to the correction.

    Spreading the cap along the route was first done with a flat quota per
    bucket, which was worse than the problem it fixed: real POIs cluster
    into a handful of buckets, so a ride out of Oxford with 275 candidates -
    comfortably under the 300 cap - returned 24 of them and still reported
    truncation, because the quota in the 43 empty buckets was never
    redistributed. Nothing under the cap should ever be dropped.
    """
    await register(client)
    # 250 POIs packed into a short stretch, well under MAX_POI_RESULTS.
    await seed(
        db,
        *(
            poi(i, "cafe", WEST + (i % 25) * 0.000_02, LAT, name=f"Clustered {i}")
            for i in range(250)
        ),
    )

    found = await find(client)

    assert len(found["pois"]) == 250
    assert found["truncated"] is False


async def test_distance_along_is_not_measured_in_degrees(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An L-shaped route: 20 km east, then 20 km north.

    ST_LineLocatePoint on unprojected lon/lat treats a degree of longitude
    and a degree of latitude as the same unit, and at this latitude the
    former is about 63% of the latter. That put a POI at the corner 22% too
    far along. The only test that existed used a purely east-west line - the
    one geometry where the distortion vanishes.
    """
    await register(client)
    corner = (-0.71, 51.0)
    await seed(db, poi(1, "water", corner[0], corner[1], name="At the corner"))

    found = await find(client, line=[[-1.0, 51.0], list(corner), [-0.71, 51.18]])

    # True geodesic distance to the corner is about 20.4 km of a 40.4 km
    # route; the degree-space answer was 24.9 km.
    along = found["pois"][0]["dist_along_m"]
    assert 20_000 < along < 20_800, along


async def test_a_long_list_is_cut_short_and_says_so(client: AsyncClient, db: AsyncSession) -> None:
    """No silent caps: a truncated answer must not look like a complete one."""
    await register(client)
    step = (EAST - WEST) / (MAX_POI_RESULTS + 20)
    await seed(
        db,
        *(
            poi(i, "cafe", WEST + i * step, LAT, name=f"Cafe {i}")
            for i in range(MAX_POI_RESULTS + 5)
        ),
    )

    found = await find(client)

    # At most the cap, and fewer here because the per-bucket limit is what
    # binds: the cap is a ceiling on a spread, not a page taken from the
    # front.
    assert 0 < len(found["pois"]) <= MAX_POI_RESULTS
    assert found["truncated"] is True
    # Whatever survives still covers the ride end to end.
    along = [p["dist_along_m"] for p in found["pois"]]
    assert min(along) < 500
    assert max(along) > 3000


async def test_a_short_list_is_not_flagged_as_truncated(
    client: AsyncClient, db: AsyncSession
) -> None:
    await register(client)
    await seed(db, poi(1, "cafe", -0.0300, LAT, name="The Cafe"))

    assert (await find(client))["truncated"] is False


async def test_a_line_of_one_point_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.post("/api/places/pois-along-route", json={"line": [[WEST, LAT]]})

    assert response.status_code == 422


async def test_an_absurdly_long_line_is_rejected(client: AsyncClient) -> None:
    """A real 50 km route is about 1,800 points; the cap keeps the request
    body bounded without getting in a genuine ride's way."""
    await register(client)
    line = [[WEST, LAT]] * (MAX_ROUTE_POINTS + 1)

    response = await client.post("/api/places/pois-along-route", json={"line": line})

    assert response.status_code == 422


async def test_an_absurd_radius_is_rejected(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        "/api/places/pois-along-route", json={"line": LINE, "radius_m": 500_000}
    )

    assert response.status_code == 422


async def test_a_line_with_impossible_coordinates_is_rejected(client: AsyncClient) -> None:
    """PostGIS takes a latitude of 500 without complaint and returns a
    distance computed from it, so nonsense would come back looking like a
    real answer. Bounded like Waypoint has always been."""
    await register(client)

    response = await client.post(
        "/api/places/pois-along-route", json={"line": [[WEST, LAT], [WEST, 500.0]]}
    )

    assert response.status_code == 422
