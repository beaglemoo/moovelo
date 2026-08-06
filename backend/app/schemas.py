import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Preset = Literal["road", "gravel", "quiet"]


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
    waypoints: list[Waypoint]
    updated_at: datetime
    wahoo: WahooState = WahooState()
    share_token: str | None = None


class SharedRoute(RouteResponse):
    """Public view of a shared route - no ids, no owner information."""

    name: str
    preset: str
    updated_at: datetime
