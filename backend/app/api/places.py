from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbDep, UserDep
from app.schemas import PlaceResult, PoiQuery, PoisAlongRoute
from app.services.places import (
    MAX_LIMIT,
    MIN_QUERY_LENGTH,
    pois_along_route,
    reverse_geocode,
    search_places,
)

router = APIRouter(prefix="/api/places")


@router.get("/search")
async def search(
    db: DbDep,
    _user: UserDep,
    q: Annotated[str, Query(min_length=MIN_QUERY_LENGTH, max_length=120)],
    near_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    near_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 8,
) -> list[PlaceResult]:
    """Find a place by name in the offline index.

    Returns an empty list rather than an error when the index has not been
    built - the frontend already hides search in that case, and a 404 here
    would turn a not-configured instance into an error state.
    """
    # Both or neither: half a coordinate cannot weight anything.
    near = (near_lat, near_lon) if near_lat is not None and near_lon is not None else None
    return await search_places(db, q, near=near, limit=limit)


@router.get("/reverse")
async def reverse(
    db: DbDep,
    _user: UserDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
) -> PlaceResult | None:
    """Name the nearest place to a point, for waypoint labels and route names.

    Null when nothing is within 50 km, which also covers an unbuilt index.
    """
    return await reverse_geocode(db, lat, lon)


@router.post("/pois-along-route")
async def pois(db: DbDep, _user: UserDep, query: PoiQuery) -> PoisAlongRoute:
    """Water, coffee, toilets and bike shops on a ride, ordered along it.

    A POST carrying the line rather than a GET on a route id, so the
    planner can ask about a route that has not been saved - which is when
    the question comes up.
    """
    return await pois_along_route(db, query.line, query.radius_m, query.categories)
