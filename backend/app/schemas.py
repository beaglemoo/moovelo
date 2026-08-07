import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Preset = Literal["road", "gravel", "quiet"]


class AppConfig(BaseModel):
    """What the frontend needs to know before it renders anything."""

    tile_url_cyclosm: str | None = None
    # False until the place index has been built. The indexer is opt-in, so
    # a default install has no places, POIs or cycle routes - and the search
    # box, POI panel and network overlay all stay hidden rather than
    # offering features that would answer nothing.
    search_enabled: bool = False


class PlaceResult(BaseModel):
    id: int
    name: str
    # city / town / village / hamlet / suburb / locality / peak / station
    place_type: str
    lat: float
    lon: float
    # Straight-line metres from the `near` point, when one was given. The
    # frontend shows it because 21,848 of England's places share a name
    # with another, and distance is what tells two Newports apart.
    distance_m: float | None = None


class Waypoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteRequest(BaseModel):
    waypoints: list[Waypoint] = Field(min_length=2)
    preset: Preset = "road"


class ElevationPoint(BaseModel):
    dist_m: float
    elev_m: float


class RouteLeg(BaseModel):
    # Encoded polyline6 for this leg; maneuvers reference shape indices within it.
    geometry: str
    maneuvers: list[dict[str, Any]]


class RouteResponse(BaseModel):
    legs: list[RouteLeg]
    distance_m: float
    duration_s: float
    ascent_m: float
    descent_m: float
    elevation: list[ElevationPoint]


class RouteSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    waypoints: list[Waypoint] = Field(min_length=2)
    preset: Preset
    snapshot: RouteResponse


class RoutePatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=5000)
    is_favourite: bool | None = None
    waypoints: list[Waypoint] | None = Field(default=None, min_length=2)
    preset: Preset | None = None
    snapshot: RouteResponse | None = None


class WahooState(BaseModel):
    status: str = "none"
    error: str | None = None
    route_id: str | None = None
    pushed_at: datetime | None = None


class RouteSummary(BaseModel):
    id: uuid.UUID
    name: str
    preset: str
    source: str = "planned"
    tags: list[str] = []
    is_favourite: bool = False
    distance_m: float
    ascent_m: float
    updated_at: datetime
    wahoo: WahooState = WahooState()
    share_token: str | None = None

    @classmethod
    def from_route(cls, route: Any) -> "RouteSummary":
        return cls(
            id=route.id,
            name=route.name,
            preset=route.preset,
            source=route.source,
            tags=route.tags,
            is_favourite=route.is_favourite,
            distance_m=route.distance_m,
            ascent_m=route.ascent_m,
            updated_at=route.updated_at,
            wahoo=WahooState(
                status=route.wahoo_status,
                error=route.wahoo_error,
                route_id=route.wahoo_route_id,
                pushed_at=route.wahoo_pushed_at,
            ),
            share_token=route.share_token,
        )


class SavedRoute(RouteResponse):
    id: uuid.UUID
    name: str
    preset: str
    source: str = "planned"
    tags: list[str] = []
    notes: str | None = None
    is_favourite: bool = False
    waypoints: list[Waypoint]
    updated_at: datetime
    wahoo: WahooState = WahooState()
    share_token: str | None = None


class SharedRoute(RouteResponse):
    """Public view of a shared route - no ids, no owner information."""

    name: str
    preset: str
    updated_at: datetime
