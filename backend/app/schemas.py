import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Preset = Literal["road", "gravel", "quiet"]

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class AppConfig(BaseModel):
    """What the frontend needs to know before it renders anything."""

    tile_url_cyclosm: str | None = None
    # False until the place index has been built. The indexer is opt-in, so
    # a default install has no places, POIs or cycle routes - and the search
    # box, POI panel and network overlay all stay hidden rather than
    # offering features that would answer nothing.
    search_enabled: bool = False
    # When the index was last built, as epoch seconds. Goes on the cycle
    # network tile URL so a re-index changes the URL: the tiles carry a long
    # max-age, and without this a browser would keep serving yesterday's
    # network for a day after the indexer reran. None when unbuilt.
    search_index_version: str | None = None


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


# A real 50 km road route decodes to about 1,800 points, so this leaves
# room for a very long one while keeping the request body bounded.
MAX_ROUTE_POINTS = 20_000
# Further than this and "along the route" stops meaning anything.
MAX_POI_RADIUS_M = 5_000.0
MAX_POI_RESULTS = 300


class PoiQuery(BaseModel):
    """POIs near a route line.

    The line is sent rather than a route id so the planner can ask before
    anything is saved - which is when "where is water on this ride" is
    actually asked.
    """

    # [lon, lat], matching GeoJSON and the decoded polyline the frontend
    # already holds. Bounded like Waypoint is: PostGIS accepts a latitude of
    # 500 without complaint and returns a distance computed from it, so
    # nonsense in comes back as plausible nonsense out rather than a 422.
    line: list[tuple[Longitude, Latitude]] = Field(min_length=2, max_length=MAX_ROUTE_POINTS)
    radius_m: float = Field(default=250.0, gt=0, le=MAX_POI_RADIUS_M)
    # Empty means every category. Unknown names simply match nothing.
    categories: list[str] = Field(default_factory=list, max_length=32)


class PoiResult(BaseModel):
    id: int
    # Null for the unnamed majority - 232 of the 722 POIs within 250 m of a
    # Tring to Oxford ride are unnamed bike parking - so the UI must render
    # the category instead, never an empty row.
    name: str | None
    category: str
    lat: float
    lon: float
    dist_from_route_m: float
    # Metres from the start, measured along the route. What turns a list of
    # nearby POIs into a plan for the ride.
    dist_along_m: float
    # opening_hours, website and the like, straight from OSM. Untrusted
    # text: render it, never interpret it.
    tags: dict[str, str]


class PoisAlongRoute(BaseModel):
    pois: list[PoiResult]
    # True when MAX_POI_RESULTS cut the list short, so the UI can say so
    # rather than quietly presenting a partial answer as a complete one.
    truncated: bool


DEFAULT_WEIGHT_KG = 78.0
DEFAULT_FLAT_SPEED_KMH = 22.0


class UserSettingsResponse(BaseModel):
    weight_kg: float
    flat_speed_kmh: float
    ftp_watts: float | None = None


class UserSettingsPatch(BaseModel):
    weight_kg: float | None = Field(default=None, ge=30, le=200)
    flat_speed_kmh: float | None = Field(default=None, ge=5, le=60)
    # Nullable *and* optional: omitting the field leaves ftp_watts untouched,
    # sending it as null clears a previously set value. exclude_unset is
    # what tells these two apart.
    ftp_watts: float | None = Field(default=None, ge=0, le=2000)


class SurfaceBreakdown(BaseModel):
    """Aggregated Valhalla /trace_attributes edges over a route's own shape.

    Metres, not fractions - summable across chunks, and the frontend derives
    percentages from `total_m`. Keys are Valhalla's own enum strings
    (including ones absent from its documented enum, e.g. "service_road" and
    "parking_aisle"), so these are plain dicts rather than a Literal/enum.
    """

    total_m: float
    surface_m: dict[str, float] = {}
    road_class_m: dict[str, float] = {}
    use_m: dict[str, float] = {}
    # Metres where cycle_lane is present and not "none". Kept separate from
    # use_m: surface mix and marked cycling infrastructure measure different
    # things, and conflating them would misrepresent both.
    cycle_lane_m: float = 0.0


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
    # None default is what keeps snapshots stored before Phase 7 - and any
    # route whose edge_walk match failed - parsing without a migration of
    # their own.
    surface: SurfaceBreakdown | None = None


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
